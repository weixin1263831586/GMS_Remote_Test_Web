"""SCP firmware transport with websocket progress reporting."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import tempfile
import threading
import time

import scp

from foundation.error_model import ApiError

from . import runtime


logger = logging.getLogger(__name__)

_DEFAULT_TRANSFER_TIMEOUT_SECONDS = 3600.0


def transfer_timeout_seconds() -> float:
    """Configured SCP transfer deadline (monotonic-clock, >= 1s)."""
    try:
        return max(float(runtime.firmware_transfer_timeout or 0), 1.0)
    except (TypeError, ValueError):
        return _DEFAULT_TRANSFER_TIMEOUT_SECONDS


def _abort_transport(ssh) -> None:
    """Close the SSH transport so a blocked SCP worker thread aborts.

    paramiko offers no cooperative cancel; closing the transport makes the
    blocked read/write raise inside the worker thread.
    """
    with contextlib.suppress(Exception):
        ssh.get_transport().close()


async def scp_put_with_deadline(ssh, local_path: str, remote_path: str) -> None:
    """Run a bare ``SCPClient.put`` bounded by the configured transfer timeout.

    On timeout the SSH transport is closed to abort the blocked worker
    thread and the caller sees ``ApiError.dependency_timeout`` (504); a
    transport-level failure maps to ``upstream_failure`` (502) instead of
    the generic 500.
    """
    timeout_seconds = transfer_timeout_seconds()
    client = scp.SCPClient(ssh.get_transport())
    loop = asyncio.get_running_loop()
    try:
        await asyncio.wait_for(
            loop.run_in_executor(None, client.put, local_path, remote_path),
            timeout=timeout_seconds,
        )
    except asyncio.TimeoutError as exc:
        _abort_transport(ssh)
        raise ApiError.dependency_timeout(
            f'SCP transfer timed out after {int(timeout_seconds)}s: {local_path}'
        ) from exc
    except asyncio.CancelledError:
        _abort_transport(ssh)
        raise
    except Exception as exc:
        # 传输失败属远端基础设施故障（502）：与下方 upload_error 的映射
        # 一致；异常细节只进服务端日志，客户端消息保留失败上下文。
        logger.error('SCP transfer failed: %s', exc)
        raise ApiError.upstream_failure(
            f'SCP transfer failed while uploading {local_path}', service='scp'
        ) from exc
    finally:
        with contextlib.suppress(Exception):
            client.close()


async def upload_firmware_to_test_host(
    ssh,
    client_id: str,
    source,
    remote_path: str,
    filename: str,
    file_size: int,
    upload_id: str = "",
) -> None:
    progress = {'current_percentage': 0.0, 'last_lock_update': 0.0}
    complete = threading.Event()
    upload_error = [None]

    def update_global(percentage: float, sent: int) -> None:
        if percentage != 0 and percentage - progress['last_lock_update'] < 10:
            return
        with runtime.global_state.firmware_upload_progress_lock:
            current = runtime.global_state.firmware_upload_progress.get(client_id) or {}
            if upload_id and current.get('upload_id') not in {None, '', upload_id}:
                return
            runtime.global_state.firmware_upload_progress[client_id] = {
                'progress': percentage,
                'filename': filename,
                'uploaded_size': sent,
                'total_size': file_size,
                'timestamp': time.time(),
                'stage': 'uploading_to_server',
                'upload_id': upload_id,
            }
        progress['last_lock_update'] = percentage

    def upload_progress(_filename, size, sent) -> None:
        percentage = (sent / size) * 100 if size > 0 else 0.0
        progress['current_percentage'] = percentage
        try:
            update_global(percentage, sent)
        except Exception:
            logger.exception('Failed to update firmware upload progress')

    def upload_worker() -> None:
        client = None
        try:
            client = scp.SCPClient(ssh.get_transport(), progress=upload_progress)
            if hasattr(source, 'read'):
                with contextlib.suppress(Exception):
                    source.seek(0)
                client.putfo(source, remote_path, size=file_size)
            else:
                client.put(source, remote_path)
        except Exception as exc:
            logger.error('Firmware upload error: %s', exc)
            upload_error[0] = str(exc)
        finally:
            if client:
                with contextlib.suppress(Exception):
                    client.close()
            complete.set()

    try:
        update_global(0.0, 0)
        await _send_progress(client_id, filename, 0, file_size, 0)
        thread = threading.Thread(target=upload_worker, daemon=True)
        thread.start()

        last_percentage = 0.0
        last_update_time = time.time()
        # 单调时钟 deadline：卡死的 SCP 传输按 504 收口（time.time() 会被
        # NTP 跳变破坏，deadline 必须用 time.monotonic()）。
        deadline = time.monotonic() + transfer_timeout_seconds()
        while not complete.is_set():
            if time.monotonic() >= deadline:
                _abort_transport(ssh)
                raise ApiError.dependency_timeout(
                    'Firmware upload to test host timed out'
                )
            await asyncio.sleep(1.0)
            percentage = progress['current_percentage']
            now = time.time()
            if abs(percentage - last_percentage) > 1.0 and now - last_update_time > 2.0:
                await _send_progress(
                    client_id,
                    filename,
                    round(percentage, 2),
                    file_size,
                    int((percentage / 100) * file_size),
                )
                last_percentage = percentage
                last_update_time = now

        thread.join(timeout=300)
        if thread.is_alive():
            # SCP 卡死属依赖超时（504），不能落进通用 RuntimeError→500。
            raise ApiError.dependency_timeout('Firmware upload to test host timed out')
        if upload_error[0]:
            # SCP 传输失败属远端基础设施故障（502）。
            raise ApiError.upstream_failure(f'Upload failed: {upload_error[0]}', service='scp')
        await _send_progress(client_id, filename, 100, file_size, file_size)
        await runtime.safe_websocket_send(client_id, {
            'type': 'log_update',
            'log': 'Firmware file upload complete',
            'log_type': 'success',
        })
    except asyncio.CancelledError:
        if not complete.is_set():
            _abort_transport(ssh)
        raise
    finally:
        with runtime.global_state.firmware_upload_progress_lock:
            current = runtime.global_state.firmware_upload_progress.get(client_id) or {}
            if not upload_id or current.get('upload_id') == upload_id:
                runtime.global_state.firmware_upload_progress.pop(client_id, None)


async def download_remote_firmware(ssh, remote_path: str, local_path: str) -> None:
    """Copy a test-host firmware file onto the Controller filesystem."""

    def _get() -> None:
        sftp = ssh.open_sftp()
        try:
            with open(local_path, "r+b") as destination:
                sftp.getfo(remote_path, destination)
        finally:
            sftp.close()

    timeout_seconds = transfer_timeout_seconds()
    try:
        await asyncio.wait_for(
            asyncio.to_thread(_get),
            timeout=timeout_seconds,
        )
    except asyncio.TimeoutError as exc:
        _abort_transport(ssh)
        raise ApiError.dependency_timeout("从测试主机读取固件超时") from exc
    except asyncio.CancelledError:
        _abort_transport(ssh)
        raise
    except Exception as exc:
        logger.error("firmware download from test host failed: %s", type(exc).__name__)
        raise ApiError.upstream_failure(
            "从测试主机读取固件失败", service="sftp",
        ) from exc


async def controller_firmware_for_source_flash(
    ssh,
    remote_firmware: str,
    local_firmware_path: str | None,
) -> tuple[str, str | None]:
    """Return a Controller-local image path and an optional temp to delete.

    ``remote_firmware`` is on the test host. Passing it to Source Agent
    ``SFTP.put`` would open a same-named Controller file, or fail after USB
    ownership was already returned.
    """
    if local_firmware_path and os.path.isfile(local_firmware_path):
        return local_firmware_path, None
    descriptor, local_path = tempfile.mkstemp(
        prefix="gms-source-firmware-", suffix=".img",
    )
    os.close(descriptor)
    try:
        await download_remote_firmware(ssh, remote_firmware, local_path)
    except (Exception, asyncio.CancelledError):
        with contextlib.suppress(OSError):
            os.remove(local_path)
        raise
    return local_path, local_path


async def _send_progress(
    client_id: str,
    filename: str,
    percentage: float,
    total_size: int,
    uploaded_size: int,
) -> None:
    await runtime.safe_websocket_send(client_id, {
        'type': 'file_upload_progress',
        'filename': filename,
        'percentage': percentage,
        'total_size': total_size,
        'uploaded_size': uploaded_size,
    })
