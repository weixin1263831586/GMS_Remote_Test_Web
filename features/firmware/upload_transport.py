"""SCP firmware transport with websocket progress reporting."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import time

import scp

from . import runtime


logger = logging.getLogger(__name__)


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
        while not complete.is_set():
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
            raise RuntimeError('Upload timed out')
        if upload_error[0]:
            raise RuntimeError(f'Upload failed: {upload_error[0]}')
        await _send_progress(client_id, filename, 100, file_size, file_size)
        await runtime.safe_websocket_send(client_id, {
            'type': 'log_update',
            'log': 'Firmware file upload complete',
            'log_type': 'success',
        })
    finally:
        with runtime.global_state.firmware_upload_progress_lock:
            current = runtime.global_state.firmware_upload_progress.get(client_id) or {}
            if not upload_id or current.get('upload_id') == upload_id:
                runtime.global_state.firmware_upload_progress.pop(client_id, None)


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
