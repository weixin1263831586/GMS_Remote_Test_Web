import asyncio
import contextlib
import logging
import os
import re
import shlex
import time

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from features.auth import require_elevated_admin_when_auth_required
from features.devices import DeviceUtils, parse_adb_device_states
from features.test_execution import get_default_suites_path
from features.users import get_client_username_from_request
from foundation.responses import error_response, success_response
from foundation.security import sanitize_device_ids
from foundation.uploads import upload_temp_root

from . import chunk_uploads, runtime
from .firmware_validation import (
    FirmwareValidationResult,
    validate_local_update_image,
    validate_remote_update_image,
)
from .gsi_diagnostics import diagnose_gsi_burn_failure
from .gsi_transport import prepare_gsi_command, upload_gsi_assets
from .models import SNBurnRequest
from .upload_transport import upload_firmware_to_test_host as _upload_firmware_to_test_host


logger = logging.getLogger(__name__)

router = APIRouter()

UPLOAD_PROGRESS_EXPIRATION = 24 * 60 * 60
_REMOTE_FILE_FOUND_MARKER = "__GMS_REMOTE_FILE_FOUND__"
_REMOTE_FILE_MISSING_MARKER = "__GMS_REMOTE_FILE_MISSING__"
_FASTBOOT_OKAY_RE = re.compile(r"\s+OKAY\s+\[\s*[\d.]+s\]$")
_ANSI_ESCAPE_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
_FIRMWARE_CHUNK_ROOT = upload_temp_root("gms_firmware_uploads")
MAX_FIRMWARE_CHUNKS = chunk_uploads.MAX_FIRMWARE_CHUNKS


def strip_ansi_codes(text: str) -> str:
    return _ANSI_ESCAPE_RE.sub("", text or "")


def _adb_proxy_devices(devices: list[str]) -> list[str]:
    try:
        from worker_agent.adb_proxy import imported_device_for_serial

        return [
            device_id
            for device_id in devices
            if imported_device_for_serial(device_id) is not None
        ]
    except (ImportError, OSError, RuntimeError, ValueError):
        return []


def _remote_file_exists(ssh, path: str) -> bool:
    check_cmd = (
        f"test -f {shlex.quote(path)} "
        f"&& echo {_REMOTE_FILE_FOUND_MARKER} "
        f"|| echo {_REMOTE_FILE_MISSING_MARKER}"
    )
    output, _, _ = runtime.ssh_manager.execute_command(ssh, check_cmd, timeout=5)
    lines = {line.strip() for line in output.splitlines()}
    return _REMOTE_FILE_FOUND_MARKER in lines


def _safe_upload_token(value: str) -> str:
    return chunk_uploads.safe_upload_token(value)


def _remote_join(base_dir: str, path: str) -> str:
    return f"{base_dir.rstrip('/')}/{path.lstrip('/')}"


def _normalize_firmware_devices(values: list[str]) -> tuple[list[str], list[str]]:
    requested = list(dict.fromkeys(
        str(value or "").strip()
        for value in values
        if str(value or "").strip()
    ))
    devices = sanitize_device_ids(requested)
    invalid = [value for value in requested if value not in devices]
    return devices, invalid


def _resolve_gsi_remote_image(ssh, gms_suite_dir: str, image_path: str, label: str) -> tuple[str | None, str | None]:
    image_path = str(image_path or "").strip()
    if not image_path:
        return "", None

    if image_path.startswith("/") or image_path.startswith("./"):
        if _remote_file_exists(ssh, image_path):
            return image_path, None
        return None, f"{label} not found: {image_path}"

    remote_candidate = _remote_join(gms_suite_dir, image_path)
    if _remote_file_exists(ssh, remote_candidate):
        return remote_candidate, None
    return None, f"{label} not found: {remote_candidate}"


def _firmware_upload_session_dir(client_id: str, upload_id: str) -> str:
    return chunk_uploads.upload_session_dir(_FIRMWARE_CHUNK_ROOT, client_id, upload_id)


def _cleanup_expired_upload_sessions(client_id: str) -> None:
    chunk_uploads.cleanup_expired_upload_sessions(
        _FIRMWARE_CHUNK_ROOT,
        client_id,
        UPLOAD_PROGRESS_EXPIRATION,
    )


async def _handle_firmware_chunk_upload(form, client_id: str):
    return await chunk_uploads.handle_chunk_upload(
        form,
        client_id,
        _FIRMWARE_CHUNK_ROOT,
        runtime.global_state,
        UPLOAD_PROGRESS_EXPIRATION,
    )


async def _lock_devices(request: Request, client_id: str, devices: list, error_prefix="Devices occupied"):
    username = get_client_username_from_request(request)
    return await runtime.lock_firmware_devices(
        client_id=client_id,
        username=username,
        devices=devices,
        error_prefix=error_prefix,
    )


async def _reject_invalid_firmware(
    client_id: str,
    devices: list[str],
    firmware_name: str,
    validation: FirmwareValidationResult,
):
    if client_id in runtime.global_state.websocket_connections:
        with contextlib.suppress(Exception):
            await runtime.safe_websocket_send(client_id, {
                "type": "log_update",
                "log": validation.message,
                "log_type": "error",
            })
    if runtime.store_notification:
        runtime.store_notification(
            client_id,
            "Firmware validation failed",
            validation.message[:300],
            "error",
            "firmware",
            {"devices": devices, "firmware": firmware_name, "stage": "preflight"},
        )
    return error_response(validation.message, status_code=422)


def _partition_devices_by_flash_state(
    ssh,
    devices: list[str],
) -> tuple[list[str], list[str]]:
    """允许 GSI 从 ADB、bootloader Fastboot 或 Fastbootd 状态开始。"""
    adb_output, _error, _code = runtime.ssh_manager.execute_command(
        ssh,
        "adb devices",
        timeout=8,
    )
    fastboot_output, fastboot_error, _code = runtime.ssh_manager.execute_command(
        ssh,
        "fastboot devices",
        timeout=8,
    )
    adb_states = parse_adb_device_states(adb_output)
    fastboot_devices = set(
        DeviceUtils.parse_fastboot_devices(fastboot_output or fastboot_error)
    )
    ready, unavailable = [], []
    for serial in devices:
        if adb_states.get(serial) == "device" or serial in fastboot_devices:
            ready.append(serial)
        else:
            unavailable.append(serial)
    return ready, unavailable


async def _notify_skip(client_id: str, offline: list[str]) -> None:
    """通过 websocket 告知用户哪些离线设备被跳过。"""
    if not offline:
        return
    if client_id in runtime.global_state.websocket_connections:
        with contextlib.suppress(Exception):
            await runtime.safe_websocket_send(
                client_id,
                {
                    "type": "log_update",
                    "log": f"跳过不可烧写设备（未在 ADB/Fastboot 中或状态异常）: {', '.join(offline)}",
                    "log_type": "warning",
                },
            )





@router.get("/api/burn/upload-progress")
async def get_firmware_upload_progress(request: Request):
    """Query firmware upload progress."""
    client_id = runtime.get_client_id_from_request(request)

    with runtime.global_state.firmware_upload_progress_lock:
        current_time = time.time()
        expired_clients = [
            cid for cid, data in runtime.global_state.firmware_upload_progress.items()
            if current_time - data["timestamp"] > UPLOAD_PROGRESS_EXPIRATION
        ]
        for cid in expired_clients:
            del runtime.global_state.firmware_upload_progress[cid]

        if client_id in runtime.global_state.firmware_upload_progress:
            progress_data = runtime.global_state.firmware_upload_progress[client_id]
            return JSONResponse(content={
                "in_progress": True,
                "progress": progress_data["progress"],
                "filename": progress_data["filename"],
                "uploaded_size": progress_data["uploaded_size"],
                "total_size": progress_data["total_size"],
                "stage": progress_data.get("stage", "uploading_to_server"),
                "upload_id": progress_data.get("upload_id", ""),
            })
        else:
            return JSONResponse(content={"in_progress": False})



@router.post("/api/burn/firmware")
async def burn_firmware(
    request: Request,
    h: str | None = Query(None),
    help: bool = Query(False),
    _admin=Depends(require_elevated_admin_when_auth_required),
):
    """Firmware burning - supports file upload."""
    resp = runtime.generate_help_or_continue(help, "POST", "/api/burn/firmware")
    if resp:
        return resp

    client_id = ""
    merged_firmware = None
    burn_lock_path = None
    burn_succeeded = False
    locked_devices: list[str] = []
    try:
        client_id = runtime.get_client_id_from_request(request)

        form = await request.form()
        finalize_upload = str(form.get("finalize_upload") or "").strip().lower() in {
            "1", "true", "yes"
        }
        if finalize_upload:
            upload_id = str(form.get("upload_id") or "").strip()
            merged_firmware, staged_error = chunk_uploads.load_staged_upload(
                _FIRMWARE_CHUNK_ROOT,
                client_id,
                upload_id,
            )
            if not merged_firmware:
                return error_response(staged_error or "Firmware upload is not staged", 409)
            burn_lock_path = chunk_uploads.acquire_burn_lock(merged_firmware)
            if not burn_lock_path:
                return error_response("Firmware burn is already being finalized", 409)
        elif form.get("chunk_index") is not None or form.get("check_chunks") is not None:
            chunk_response, merged_firmware = await _handle_firmware_chunk_upload(form, client_id)
            if chunk_response is not None:
                return chunk_response

        devices_param = request.query_params.get("devices")
        if devices_param:
            raw_devices = devices_param.split(",")
        else:
            devices_str = form.get("devices")
            raw_devices = devices_str.split(",") if devices_str else []

        devices, invalid_devices = _normalize_firmware_devices(raw_devices)
        if invalid_devices:
            return error_response("Invalid device serial", status_code=400)

        if not devices:
            return error_response("No devices selected")
        proxy_devices = _adb_proxy_devices(devices)
        if proxy_devices:
            return error_response(
                "ADB Proxy远程设备不支持固件烧写，请在设备来源主机操作: "
                + ", ".join(proxy_devices),
                status_code=409,
            )

        locked_devices, lock_err = await _lock_devices(request, client_id, devices, "The following devices are occupied")
        if lock_err:
            return lock_err

        firmware_file = form.get("firmware_file")
        firmware_path = form.get("firmware_path", "").strip()
        if merged_firmware:
            firmware_file = None
            firmware_path = merged_firmware["path"]

        if not firmware_file and not firmware_path:
            return error_response("Please upload a firmware file or provide a firmware path")

        local_tool = os.path.join(runtime.project_root, "tools", "upgrade_tool")
        if not os.path.exists(local_tool):
            return error_response(f"upgrade_tool not found: {local_tool}")

        # Staged browser uploads are local files, so reject malformed Rockchip
        # update images before spending time on SCP or rebooting a device.
        if merged_firmware:
            local_validation = await asyncio.to_thread(
                validate_local_update_image,
                local_tool,
                firmware_path,
            )
            if not local_validation.valid:
                return await _reject_invalid_firmware(
                    client_id,
                    devices,
                    merged_firmware["name"],
                    local_validation,
                )

        config = runtime.config_manager.load_config()
        async with runtime.ssh_manager.async_optional_connection(config) as ssh:
            if not ssh:
                return error_response("SSH connection failed")

            try:
                gms_suite_dir = get_default_suites_path(config)
                if firmware_file:
                    firmware_name = os.path.basename(firmware_file.filename or "").strip()
                    if not firmware_name:
                        return error_response("Invalid firmware filename")

                    firmware_stream = firmware_file.file
                    try:
                        firmware_stream.seek(0, os.SEEK_END)
                        firmware_size = firmware_stream.tell()
                        firmware_stream.seek(0)
                    except Exception as e:
                        return error_response(f"Failed to inspect firmware size: {e}")

                    if firmware_size <= 0:
                        return error_response("Uploaded firmware file is empty")

                    remote_firmware = os.path.join(gms_suite_dir, firmware_name)
                    await _upload_firmware_to_test_host(ssh, client_id, firmware_stream, remote_firmware, firmware_name, firmware_size)
                else:
                    firmware_name = (
                        merged_firmware["name"]
                        if merged_firmware
                        else os.path.basename(firmware_path.rstrip("/"))
                    )
                    remote_firmware = os.path.join(gms_suite_dir, firmware_name)
                    local_firmware_path = None

                    if firmware_path.startswith("/") or firmware_path.startswith("./"):
                        if await asyncio.to_thread(_remote_file_exists, ssh, firmware_path):
                            remote_firmware = firmware_path
                        elif os.path.exists(firmware_path):
                            local_firmware_path = firmware_path
                        else:
                            return error_response(f"Firmware not found: {firmware_path}")
                    elif os.path.exists(firmware_path):
                        local_firmware_path = firmware_path
                    else:
                        remote_candidate = os.path.join(gms_suite_dir, firmware_path)
                        if await asyncio.to_thread(_remote_file_exists, ssh, remote_candidate):
                            remote_firmware = remote_candidate
                        else:
                            return error_response(f"Firmware not found: {firmware_path}")

                    if local_firmware_path:
                        file_size = os.path.getsize(local_firmware_path)
                        if file_size <= 0:
                            return error_response("Firmware file is empty")
                        if not merged_firmware:
                            local_validation = await asyncio.to_thread(
                                validate_local_update_image,
                                local_tool,
                                local_firmware_path,
                            )
                            if not local_validation.valid:
                                return await _reject_invalid_firmware(
                                    client_id,
                                    devices,
                                    firmware_name,
                                    local_validation,
                                )
                        await _upload_firmware_to_test_host(
                            ssh,
                            client_id,
                            local_firmware_path,
                            remote_firmware,
                            firmware_name,
                            file_size,
                            upload_id=merged_firmware.get("upload_id", "") if merged_firmware else "",
                        )

                # Upload upgrade_tool only after the firmware source has been
                # validated. Missing paths must not reboot devices into loader.
                logger.info("[Firmware Burn] Uploading upgrade_tool...")
                # Use a private path so the platform does not overwrite a
                # potentially newer operator-managed upgrade_tool.
                remote_tool = os.path.join(gms_suite_dir, ".gms_upgrade_tool")

                import scp
                scp_client = scp.SCPClient(ssh.get_transport())
                scp_client.put(local_tool, remote_tool)
                scp_client.close()

                # Validate the exact remote bytes that will be burned. This
                # catches both invalid loader packages and transport damage.
                remote_validation = await asyncio.to_thread(
                    validate_remote_update_image,
                    runtime.ssh_manager,
                    ssh,
                    remote_tool,
                    remote_firmware,
                )
                if not remote_validation.valid:
                    return await _reject_invalid_firmware(
                        client_id,
                        devices,
                        firmware_name,
                        remote_validation,
                    )

                # Enter Loader mode
                for device in devices:
                    cmd = f"adb -s {shlex.quote(device)} reboot loader"
                    await asyncio.to_thread(runtime.ssh_manager.execute_command, ssh, cmd, timeout=5)

                await asyncio.sleep(8)

                # Check Loader devices
                quoted_suite_dir = shlex.quote(gms_suite_dir)
                quoted_remote_tool = shlex.quote(remote_tool)
                check_cmd = f"cd {quoted_suite_dir} && {quoted_remote_tool} ld"
                output, _, _ = await asyncio.to_thread(runtime.ssh_manager.execute_command, ssh, check_cmd, timeout=5)

                if "List of rockusb connected(0)" in output or "List of rockusb connected" not in output:
                    return error_response(f"No Loader devices detected. Output:\n{output}", status_code=409)

                # Burn firmware
                burn_cmd = f"cd {quoted_suite_dir} && {quoted_remote_tool} uf {shlex.quote(remote_firmware)}"

                if client_id in runtime.global_state.websocket_connections:
                    with contextlib.suppress(Exception):
                        await runtime.safe_websocket_send(client_id, {"type": "log_update", "log": "Starting firmware burn...", "log_type": "info"})

                _stdin, stdout, stderr = await asyncio.to_thread(
                    lambda: ssh.exec_command(burn_cmd, get_pty=True, timeout=300)
                )
                output_buffer = []

                firmware_burn_start = False
                current_progress = 0
                last_progress_time = 0

                while not stdout.channel.exit_status_ready():
                    current_time = asyncio.get_event_loop().time()

                    if stdout.channel.recv_ready():
                        chunk = (await asyncio.to_thread(stdout.channel.recv, 1024)).decode("utf-8", errors="ignore")
                        output_buffer.append(chunk)
                        clean_chunk = strip_ansi_codes(chunk)

                        if "Download Firmware Start" in clean_chunk and not firmware_burn_start:
                            firmware_burn_start = True
                            current_progress = 0
                            last_progress_time = current_time

                        if client_id in runtime.global_state.websocket_connections:
                            try:
                                for line in clean_chunk.split("\n"):
                                    line = line.strip()
                                    if line:
                                        if firmware_burn_start:
                                            if any(kw in line.lower() for kw in ["error", "failed", "fail"]):
                                                await runtime.safe_websocket_send(client_id, {"type": "log_update", "log": line, "log_type": "error"})
                                            continue
                                        await runtime.safe_websocket_send(client_id, {"type": "log_update", "log": line, "log_type": "info"})
                            except Exception as e:
                                logger.error(f"[Firmware Burn] Log send failed: {e}")

                    if firmware_burn_start and (current_time - last_progress_time > runtime.gsi_progress_poll_interval):
                        current_progress = min(current_progress + runtime.gsi_progress_increment, runtime.gsi_progress_max)
                        last_progress_time = current_time

                        if client_id in runtime.global_state.websocket_connections:
                            with contextlib.suppress(Exception):
                                await runtime.safe_websocket_send(client_id, {"type": "firmware_progress", "percentage": current_progress})

                    await asyncio.sleep(0.1)

                final_output = "".join(output_buffer)
                exit_status = stdout.channel.recv_exit_status()

                if exit_status == 0:
                    if client_id in runtime.global_state.websocket_connections:
                        with contextlib.suppress(Exception):
                            await runtime.safe_websocket_send(client_id, {"type": "firmware_progress", "percentage": 100})
                        with contextlib.suppress(Exception):
                            await runtime.safe_websocket_send(client_id, {"type": "log_update", "log": "Firmware burn complete!", "log_type": "success"})

                    runtime.store_notification(client_id, "Firmware burn complete", f"Devices: {', '.join(devices)}", "success", "firmware", {"devices": devices, "firmware": firmware_name})
                    # 通知前端刷新 ADB 设备状态；设备锁由 finally 统一释放。
                    if client_id in runtime.global_state.websocket_connections:
                        with contextlib.suppress(Exception):
                            await runtime.safe_websocket_send(client_id, {"type": "firmware_burn_complete", "devices": devices, "success": True})
                    burn_succeeded = True
                    return success_response(message="Firmware burn completed successfully")
                else:
                    error_output = final_output or stderr.read().decode("utf-8", errors="ignore")

                    if client_id in runtime.global_state.websocket_connections:
                        try:
                            await runtime.safe_websocket_send(client_id, {"type": "log_update", "log": f"Firmware burn failed (exit code: {exit_status})", "log_type": "error"})
                            if error_output and len(error_output) < 500:
                                await runtime.safe_websocket_send(client_id, {"type": "log_update", "log": f"Error: {error_output[:200]}", "log_type": "error"})
                        except Exception:
                            pass

                    runtime.store_notification(client_id, "Firmware burn failed", (error_output or "Burn failed")[:300], "error", "firmware", {"devices": devices, "firmware": firmware_name, "exit_status": exit_status})
                    clean_output = strip_ansi_codes(error_output).strip()
                    burn_error_msg = f"Firmware burn failed (exit code: {exit_status})"
                    if clean_output:
                        burn_error_msg += f": {clean_output[:300]}"
                    return error_response(burn_error_msg, status_code=422)

            except Exception as e:
                runtime.store_notification(client_id, "Firmware burn error", str(e)[:300], "error", "firmware", {"devices": devices, "firmware": firmware_name if 'firmware_name' in dir() else ""})
                return error_response(str(e))

    except Exception as e:
        import traceback
        logger.error(f"Error in burn_firmware: {e}")
        logger.error(f"Traceback: {traceback.format_exc()}")
        return error_response(str(e), 500)
    finally:
        if merged_firmware and client_id:
            chunk_uploads.clear_upload_progress(
                runtime.global_state,
                client_id,
                merged_firmware.get("upload_id", ""),
            )
        chunk_uploads.release_burn_lock(burn_lock_path)
        if burn_succeeded and merged_firmware:
            chunk_uploads.remove_staged_upload(merged_firmware)
        if locked_devices and client_id:
            with contextlib.suppress(Exception):
                await runtime.release_firmware_devices(client_id, locked_devices)

@router.post("/api/burn/gsi")
async def burn_gsi(
    request: Request,
    _admin=Depends(require_elevated_admin_when_auth_required),
):
    """GSI burning using run_GSI_Burn.sh script."""
    try:
        client_id = runtime.get_client_id_from_request(request)
        req_data = await request.json()
        devices = req_data.get("devices", [])
        script_path = req_data.get("script_path", "").strip()
        system_img = req_data.get("system_img", "").strip()
        vendor_img = req_data.get("vendor_img", "").strip()

        if not devices:
            return error_response("No devices selected")
        proxy_devices = _adb_proxy_devices(devices)
        if proxy_devices:
            return error_response(
                "ADB Proxy远程设备不支持GSI烧写，请在设备来源主机操作: "
                + ", ".join(proxy_devices),
                status_code=409,
            )
        if not script_path:
            return error_response("Script path is required")
        if not system_img and not vendor_img:
            return error_response("At least one of system image or vendor boot image is required")

        config = runtime.config_manager.load_config()
        async with runtime.ssh_manager.async_optional_connection(config) as ssh:
            if not ssh:
                return error_response("SSH connection failed")

            # 烧写前预检：允许设备从 ADB、bootloader Fastboot 或 Fastbootd
            # 开始；其他离线/未授权状态仍提前剔除。
            online_devices, offline_devices = await asyncio.to_thread(
                _partition_devices_by_flash_state, ssh, devices
            )
            if not online_devices:
                return error_response(
                    f"没有可烧写的 ADB/Fastboot 设备，离线/状态异常: {', '.join(offline_devices)}"
                )
            await _notify_skip(client_id, offline_devices)

            locked_devices, lock_err = await _lock_devices(request, client_id, online_devices)
            if lock_err:
                return lock_err

            try:
                gms_suite_dir = get_default_suites_path(config)
                remote_script, resolved_misc, asset_error = await asyncio.to_thread(
                    upload_gsi_assets,
                    ssh=ssh,
                    ssh_manager=runtime.ssh_manager,
                    project_root=str(runtime.project_root),
                    suite_dir=gms_suite_dir,
                )
                if asset_error:
                    await runtime.release_firmware_devices(client_id, locked_devices)
                    return error_response(asset_error)

                resolved_system = ""
                if system_img:
                    resolved_system, system_error = await asyncio.to_thread(
                        _resolve_gsi_remote_image,
                        ssh,
                        gms_suite_dir,
                        system_img,
                        "System image",
                    )
                    if system_error:
                        await runtime.release_firmware_devices(client_id, locked_devices)
                        return error_response(system_error)

                remote_vendor = ""
                if vendor_img:
                    resolved_vendor, vendor_error = await asyncio.to_thread(
                        _resolve_gsi_remote_image,
                        ssh,
                        gms_suite_dir,
                        vendor_img,
                        "Vendor boot image",
                    )
                    if vendor_error:
                        await runtime.release_firmware_devices(client_id, locked_devices)
                        return error_response(vendor_error)
                    remote_vendor = resolved_vendor or ""

                results = []

                if client_id in runtime.global_state.websocket_connections:
                    with contextlib.suppress(Exception):
                        await runtime.safe_websocket_send(client_id, {"type": "log_update", "log": f"Starting GSI burn for {len(online_devices)} devices...", "log_type": "info"})

                for device in online_devices:
                    if client_id in runtime.global_state.websocket_connections:
                        with contextlib.suppress(Exception):
                            await runtime.safe_websocket_send(client_id, {"type": "log_update", "log": f"Burning device: {device}", "log_type": "info"})

                    try:
                        burn_cmd = await asyncio.to_thread(
                            prepare_gsi_command,
                            ssh=ssh,
                            ssh_manager=runtime.ssh_manager,
                            remote_script=remote_script,
                            device=device,
                            system_img=resolved_system,
                            misc_img=resolved_misc,
                            vendor_img=remote_vendor,
                        )
                    except Exception as prep_error:
                        error_msg = f"Fastboot preparation failed: {prep_error}"
                        results.append({
                            "device": device,
                            "success": False,
                            "error": error_msg,
                            "output": error_msg,
                        })
                        if client_id in runtime.global_state.websocket_connections:
                            with contextlib.suppress(Exception):
                                await runtime.safe_websocket_send(client_id, {"type": "log_update", "log": f"Device {device} GSI burn failed: {error_msg}", "log_type": "error"})
                        continue

                    _stdin, stdout, stderr = await asyncio.to_thread(
                        ssh.exec_command,
                        burn_cmd,
                        get_pty=True,
                        timeout=600,
                    )
                    output_buffer = []

                    while not stdout.channel.exit_status_ready():
                        if stdout.channel.recv_ready():
                            chunk = (await asyncio.to_thread(stdout.channel.recv, 1024)).decode("utf-8", errors="ignore")
                            output_buffer.append(chunk)
                            clean_chunk = strip_ansi_codes(chunk)

                            if client_id in runtime.global_state.websocket_connections:
                                try:
                                    for line in clean_chunk.split("\n"):
                                        line = line.strip()
                                        if not line:
                                            continue
                                        # 过滤 fastboot 冗余输出
                                        if (line.startswith("OKAY") or
                                            line.startswith("Writing '") or
                                            line.startswith("Finished.") or
                                            line.startswith("< waiting for")):
                                            continue
                                        # 保留操作名，去掉尾部的 OKAY [x.xxxs]
                                        cleaned = _FASTBOOT_OKAY_RE.sub("", line)
                                        await runtime.safe_websocket_send(client_id, {"type": "log_update", "log": cleaned, "log_type": "info"})
                                except Exception:
                                    pass
                        else:
                            await asyncio.sleep(0.5)

                    while stdout.channel.recv_ready():
                        chunk = await asyncio.to_thread(stdout.channel.recv, 1024)
                        output_buffer.append(chunk.decode("utf-8", errors="ignore"))
                    final_output = "".join(output_buffer)
                    exit_status = stdout.channel.recv_exit_status()
                    error_output = (await asyncio.to_thread(stderr.read)).decode("utf-8", errors="ignore")

                    if exit_status == 0:
                        results.append({"device": device, "success": True, "output": final_output})
                        if client_id in runtime.global_state.websocket_connections:
                            with contextlib.suppress(Exception):
                                await runtime.safe_websocket_send(client_id, {"type": "log_update", "log": f"Device {device} GSI burn complete", "log_type": "success"})
                    else:
                        combined_output = "\n".join(
                            part for part in (final_output, error_output) if part
                        )
                        error_msg = diagnose_gsi_burn_failure(combined_output)
                        results.append({"device": device, "success": False, "error": error_msg, "output": combined_output})
                        if client_id in runtime.global_state.websocket_connections:
                            try:
                                await runtime.safe_websocket_send(client_id, {"type": "log_update", "log": f"Device {device} GSI burn failed: {error_msg}", "log_type": "error"})
                            except Exception:
                                pass

                try:
                    await runtime.release_firmware_devices(client_id, locked_devices)
                except Exception as release_error:
                    logger.warning("[GSI Burn] Failed to release device locks: %s", release_error)

                all_success = all(r["success"] for r in results)
                if all_success:
                    try:
                        runtime.store_notification(client_id, "GSI burn complete", f"Devices: {', '.join(online_devices)}", "success", "firmware", {"devices": online_devices, "results": results})
                    except Exception as notify_error:
                        logger.warning("[GSI Burn] Failed to store success notification: %s", notify_error)
                    # 设备锁已释放，通知前端刷新 ADB 设备状态
                    if client_id in runtime.global_state.websocket_connections:
                        with contextlib.suppress(Exception):
                            await runtime.safe_websocket_send(client_id, {"type": "firmware_burn_complete", "devices": online_devices, "success": True})
                    return JSONResponse(content={"success": True, "message": "GSI burn completed successfully", "results": results})
                else:
                    failed_results = [r for r in results if not r.get("success")]
                    failure_summary = "; ".join(
                        f"{result.get('device')}: {result.get('error')}"
                        for result in failed_results
                    )
                    try:
                        runtime.store_notification(client_id, "GSI burn failed", failure_summary[:300], "error", "firmware", {"devices": online_devices, "results": results})
                    except Exception as notify_error:
                        logger.warning("[GSI Burn] Failed to store failure notification: %s", notify_error)
                    return error_response(f"部分设备烧写失败: {failure_summary}", results=results)

            except Exception as e:
                try:
                    runtime.store_notification(client_id, "GSI burn error", str(e)[:300], "error", "firmware", {"devices": online_devices})
                except Exception as notify_error:
                    logger.warning("[GSI Burn] Failed to store error notification: %s", notify_error)
                try:
                    await runtime.release_firmware_devices(client_id, locked_devices)
                except Exception as release_error:
                    logger.warning("[GSI Burn] Failed to release device locks after error: %s", release_error)
                return error_response(str(e))

    except Exception as e:
        logger.error(f"Error in burn_gsi: {e}")
        return error_response(str(e), 500)



@router.post("/api/burn/serial")
async def burn_sn(
    req: SNBurnRequest,
    _admin=Depends(require_elevated_admin_when_auth_required),
):
    """SN burning - burn serial number to selected devices."""
    try:
        devices = req.devices
        sn_code = req.sn_code

        if not devices:
            return error_response("No devices selected", 400)
        if not sn_code:
            return error_response("SN code is required", 400)

        config = runtime.config_manager.load_config()
        async with runtime.ssh_manager.async_optional_connection(config) as ssh:
            if not ssh:
                return error_response("SSH connection failed", 500)

            results = [
                {
                    "device": device_id,
                    "success": False,
                    "error": "SN burning requires device in loader mode. Feature needs specific tool support.",
                }
                for device_id in devices
            ]

            return JSONResponse(content={"success": True, "results": results})
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error burning SN: {e}")
        return error_response(str(e), status_code=500)
