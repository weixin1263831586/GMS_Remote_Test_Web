import asyncio
import contextlib
import json
import logging
import os
import re
import shlex
import tempfile
import threading
import time

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from features.test_execution import get_default_suites_path
from features.users import get_client_username_from_request
from foundation.responses import error_response, success_response
from foundation.uploads import merge_files_to_path, safe_upload_target_path, save_upload_to_path

from . import runtime
from .models import SNBurnRequest


logger = logging.getLogger(__name__)

router = APIRouter()

UPLOAD_PROGRESS_EXPIRATION = 24 * 60 * 60
_REMOTE_FILE_FOUND_MARKER = "__GMS_REMOTE_FILE_FOUND__"
_REMOTE_FILE_MISSING_MARKER = "__GMS_REMOTE_FILE_MISSING__"
_FASTBOOT_OKAY_RE = re.compile(r"\s+OKAY\s+\[\s*[\d.]+s\]$")
_ANSI_ESCAPE_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
_FIRMWARE_CHUNK_ROOT = os.path.join(tempfile.gettempdir(), "gms_firmware_uploads")


def strip_ansi_codes(text: str) -> str:
    return _ANSI_ESCAPE_RE.sub("", text or "")


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
    return re.sub(r"[^A-Za-z0-9_.-]", "_", str(value or "").strip())[:120] or "default"


def _remote_join(base_dir: str, path: str) -> str:
    return f"{base_dir.rstrip('/')}/{path.lstrip('/')}"


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
    return os.path.join(
        _FIRMWARE_CHUNK_ROOT,
        _safe_upload_token(client_id),
        _safe_upload_token(upload_id),
    )


def _read_uploaded_chunks(session_dir: str) -> set[int]:
    chunks_file = os.path.join(session_dir, "uploaded_chunks.json")
    if not os.path.exists(chunks_file):
        return set()
    try:
        with open(chunks_file, encoding="utf-8") as handle:
            return {int(item) for item in json.load(handle)}
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return set()


def _write_uploaded_chunks(session_dir: str, uploaded_chunks: set[int]) -> None:
    with open(os.path.join(session_dir, "uploaded_chunks.json"), "w", encoding="utf-8") as handle:
        json.dump(sorted(uploaded_chunks), handle)


def _firmware_chunk_paths(session_dir: str, total_chunks: int) -> list[str]:
    return [os.path.join(session_dir, f"chunk_{idx:05d}") for idx in range(total_chunks)]


async def _handle_firmware_chunk_upload(form, client_id: str):
    upload_id = str(form.get("upload_id") or "").strip()
    file_name = str(form.get("file_name") or "").strip()
    if not upload_id or not file_name:
        return error_response("upload_id and file_name are required for chunk upload", 400), None

    session_dir = _firmware_upload_session_dir(client_id, upload_id)
    os.makedirs(session_dir, exist_ok=True)

    if str(form.get("check_chunks") or "").strip() in {"1", "true", "yes"}:
        uploaded_chunks = _read_uploaded_chunks(session_dir)
        try:
            total_chunks = int(form.get("total_chunks") or 0)
            file_size = int(form.get("file_size") or 0)
        except (TypeError, ValueError):
            total_chunks = 0
            file_size = 0
        uploaded_size = 0
        if total_chunks > 0:
            for path in _firmware_chunk_paths(session_dir, total_chunks):
                if os.path.exists(path):
                    uploaded_size += os.path.getsize(path)
        return JSONResponse(content={
            "success": True,
            "uploaded_chunks": sorted(uploaded_chunks),
            "chunks_uploaded": len(uploaded_chunks),
            "total_chunks": total_chunks,
            "progress": round((len(uploaded_chunks) / total_chunks) * 100, 2) if total_chunks else 0,
            "uploaded_size": uploaded_size,
            "total_size": file_size,
            "upload_id": upload_id,
        }), None

    try:
        chunk_index = int(form.get("chunk_index"))
        total_chunks = int(form.get("total_chunks"))
        file_size = int(form.get("file_size") or 0)
    except (TypeError, ValueError):
        return error_response("Invalid chunk metadata", 400), None

    if chunk_index < 0 or total_chunks <= 0 or chunk_index >= total_chunks:
        return error_response("Invalid chunk index", 400), None

    upload_file = form.get("file") or form.get("firmware_file")
    if upload_file is None:
        return error_response("No chunk file provided", 400), None

    chunk_path = os.path.join(session_dir, f"chunk_{chunk_index:05d}")
    await save_upload_to_path(upload_file, chunk_path)

    uploaded_chunks = _read_uploaded_chunks(session_dir)
    uploaded_chunks.add(chunk_index)
    chunk_paths = _firmware_chunk_paths(session_dir, total_chunks)
    # Reconcile with the filesystem in case chunks arrived out of order / via resume.
    present_paths = {idx: path for idx, path in enumerate(chunk_paths) if os.path.exists(path)}
    uploaded_chunks.update(present_paths.keys())
    _write_uploaded_chunks(session_dir, uploaded_chunks)

    uploaded_size = sum(os.path.getsize(path) for path in present_paths.values())
    with runtime.global_state.firmware_upload_progress_lock:
        runtime.global_state.firmware_upload_progress[client_id] = {
            "progress": round((len(uploaded_chunks) / total_chunks) * 100, 2),
            "filename": file_name,
            "uploaded_size": uploaded_size,
            "total_size": file_size,
            "timestamp": time.time(),
            "stage": "uploading_to_server",
            "upload_id": upload_id,
        }

    if len(uploaded_chunks) < total_chunks or not all(os.path.exists(path) for path in chunk_paths):
        return JSONResponse(content={
            "success": True,
            "upload_complete": False,
            "chunk_index": chunk_index,
            "chunks_uploaded": len(uploaded_chunks),
            "total_chunks": total_chunks,
            "progress": round((len(uploaded_chunks) / total_chunks) * 100, 2),
            "upload_id": upload_id,
        }), None

    merge_lock_path = os.path.join(session_dir, ".merge.lock")
    try:
        merge_lock_fd = os.open(merge_lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(merge_lock_fd)
    except FileExistsError:
        return JSONResponse(content={
            "success": True,
            "upload_complete": False,
            "merging": True,
            "chunks_uploaded": len(uploaded_chunks),
            "total_chunks": total_chunks,
            "progress": 100,
            "upload_id": upload_id,
        }), None

    merged_file = safe_upload_target_path(session_dir, file_name, allow_nested=False)
    merge_files_to_path(chunk_paths, merged_file)
    with contextlib.suppress(FileNotFoundError):
        os.remove(merge_lock_path)
    if file_size > 0 and os.path.getsize(merged_file) != file_size:
        return error_response("Merged firmware size mismatch", 400), None

    return None, {
        "path": merged_file,
        "name": os.path.basename(file_name),
        "size": os.path.getsize(merged_file),
        "upload_id": upload_id,
    }


async def _lock_devices(request: Request, client_id: str, devices: list, error_prefix="Devices occupied"):
    username = get_client_username_from_request(request)
    return await runtime.lock_firmware_devices(
        client_id=client_id,
        username=username,
        devices=devices,
        error_prefix=error_prefix,
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
            })
        else:
            return JSONResponse(content={"in_progress": False})



async def _upload_firmware_to_test_host(ssh, client_id: str, source, remote_path: str, filename: str, file_size: int):
    import scp

    upload_progress_data = {"current_percentage": 0.0, "last_lock_update": 0.0}
    upload_complete = threading.Event()
    upload_error = [None]

    def update_global_progress(percentage: float, sent: int):
        if percentage - upload_progress_data["last_lock_update"] < 10:
            return
        try:
            with runtime.global_state.firmware_upload_progress_lock:
                runtime.global_state.firmware_upload_progress[client_id] = {
                    "progress": percentage,
                    "filename": filename,
                    "uploaded_size": sent,
                    "total_size": file_size,
                    "timestamp": time.time(),
                    "stage": "uploading_to_server",
                }
            upload_progress_data["last_lock_update"] = percentage
        except Exception as e:
            logger.error(f"[Firmware Burn] Failed to update upload progress: {e}")

    def upload_progress(_filename, size, sent):
        percentage = (sent / size) * 100 if size > 0 else 0.0
        upload_progress_data["current_percentage"] = percentage
        update_global_progress(percentage, sent)

    def upload_file_worker():
        scp_client = None
        try:
            scp_client = scp.SCPClient(ssh.get_transport(), progress=upload_progress)
            if hasattr(source, "read"):
                with contextlib.suppress(Exception):
                    source.seek(0)
                scp_client.putfo(source, remote_path, size=file_size)
            else:
                scp_client.put(source, remote_path)
        except Exception as e:
            logger.error(f"[Firmware Burn] Upload error: {e}")
            upload_error[0] = str(e)
        finally:
            if scp_client:
                with contextlib.suppress(Exception):
                    scp_client.close()
            upload_complete.set()

    try:
        with runtime.global_state.firmware_upload_progress_lock:
            runtime.global_state.firmware_upload_progress[client_id] = {
                "progress": 0.0,
                "filename": filename,
                "uploaded_size": 0,
                "total_size": file_size,
                "timestamp": time.time(),
                "stage": "uploading_to_server",
            }

        await runtime.safe_websocket_send(client_id, {
            "type": "file_upload_progress",
            "filename": filename,
            "percentage": 0,
            "total_size": file_size,
            "uploaded_size": 0,
        })

        upload_thread = threading.Thread(target=upload_file_worker, daemon=True)
        upload_thread.start()

        last_percentage = 0.0
        last_update_time = time.time()
        while not upload_complete.is_set():
            await asyncio.sleep(1.0)
            current_percentage = upload_progress_data.get("current_percentage", 0.0)
            current_time = time.time()

            if abs(current_percentage - last_percentage) > 1.0 and (current_time - last_update_time) > 2.0:
                sent_size = int((current_percentage / 100) * file_size)
                await runtime.safe_websocket_send(client_id, {
                    "type": "file_upload_progress",
                    "filename": filename,
                    "percentage": round(current_percentage, 2),
                    "total_size": file_size,
                    "uploaded_size": sent_size,
                })
                last_percentage = current_percentage
                last_update_time = current_time

        upload_thread.join(timeout=300)
        if upload_thread.is_alive():
            raise RuntimeError("Upload timed out")
        if upload_error[0]:
            raise RuntimeError(f"Upload failed: {upload_error[0]}")

        await runtime.safe_websocket_send(client_id, {
            "type": "file_upload_progress",
            "filename": filename,
            "percentage": 100,
            "total_size": file_size,
            "uploaded_size": file_size,
        })
        await runtime.safe_websocket_send(client_id, {
            "type": "log_update",
            "log": "Firmware file upload complete",
            "log_type": "success",
        })
    finally:
        with runtime.global_state.firmware_upload_progress_lock:
            runtime.global_state.firmware_upload_progress.pop(client_id, None)



@router.post("/api/burn/firmware")
async def burn_firmware(request: Request, h: str | None = Query(None), help: bool = Query(False)):
    """Firmware burning - supports file upload."""
    resp = runtime.generate_help_or_continue(help, "POST", "/api/burn/firmware")
    if resp:
        return resp

    try:
        client_id = runtime.get_client_id_from_request(request)

        form = await request.form()
        merged_firmware = None
        if form.get("chunk_index") is not None or form.get("check_chunks") is not None:
            chunk_response, merged_firmware = await _handle_firmware_chunk_upload(form, client_id)
            if chunk_response is not None:
                return chunk_response

        devices_param = request.query_params.get("devices")
        if devices_param:
            devices = devices_param.split(",")
        else:
            devices_str = form.get("devices")
            devices = devices_str.split(",") if devices_str else []

        if not devices:
            return error_response("No devices selected")

        locked_devices, lock_err = await _lock_devices(request, client_id, devices, "The following devices are occupied")
        if lock_err:
            return lock_err

        firmware_file = form.get("firmware_file")
        firmware_path = form.get("firmware_path", "").strip()
        if merged_firmware:
            firmware_file = None
            firmware_path = merged_firmware["path"]

        if not firmware_file and not firmware_path:
            await runtime.release_firmware_devices(client_id, locked_devices)
            return error_response("Please upload a firmware file or provide a firmware path")

        config = runtime.config_manager.load_config()
        with runtime.ssh_manager.optional_connection(config) as ssh:
            if not ssh:
                await runtime.release_firmware_devices(client_id, locked_devices)
                return error_response("SSH connection failed")

            try:
                gms_suite_dir = get_default_suites_path(config)
                if firmware_file:
                    firmware_name = os.path.basename(firmware_file.filename or "").strip()
                    if not firmware_name:
                        await runtime.release_firmware_devices(client_id, locked_devices)
                        return error_response("Invalid firmware filename")

                    firmware_stream = firmware_file.file
                    try:
                        firmware_stream.seek(0, os.SEEK_END)
                        firmware_size = firmware_stream.tell()
                        firmware_stream.seek(0)
                    except Exception as e:
                        await runtime.release_firmware_devices(client_id, locked_devices)
                        return error_response(f"Failed to inspect firmware size: {e}")

                    if firmware_size <= 0:
                        await runtime.release_firmware_devices(client_id, locked_devices)
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
                            await runtime.release_firmware_devices(client_id, locked_devices)
                            return error_response(f"Firmware not found: {firmware_path}")
                    elif os.path.exists(firmware_path):
                        local_firmware_path = firmware_path
                    else:
                        remote_candidate = os.path.join(gms_suite_dir, firmware_path)
                        if await asyncio.to_thread(_remote_file_exists, ssh, remote_candidate):
                            remote_firmware = remote_candidate
                        else:
                            await runtime.release_firmware_devices(client_id, locked_devices)
                            return error_response(f"Firmware not found: {firmware_path}")

                    if local_firmware_path:
                        file_size = os.path.getsize(local_firmware_path)
                        if file_size <= 0:
                            await runtime.release_firmware_devices(client_id, locked_devices)
                            return error_response("Firmware file is empty")
                        await _upload_firmware_to_test_host(ssh, client_id, local_firmware_path, remote_firmware, firmware_name, file_size)

                # Upload upgrade_tool only after the firmware source has been
                # validated. Missing paths must not reboot devices into loader.
                logger.info("[Firmware Burn] Uploading upgrade_tool...")
                local_tool = os.path.join(runtime.project_root, "tools", "upgrade_tool")
                remote_tool = os.path.join(gms_suite_dir, "upgrade_tool")

                if not os.path.exists(local_tool):
                    await runtime.release_firmware_devices(client_id, locked_devices)
                    return error_response(f"upgrade_tool not found: {local_tool}")

                import scp
                scp_client = scp.SCPClient(ssh.get_transport())
                scp_client.put(local_tool, remote_tool)
                scp_client.close()

                # Enter Loader mode
                for device in devices:
                    cmd = f"adb -s {device} reboot loader"
                    await asyncio.to_thread(runtime.ssh_manager.execute_command, ssh, cmd, timeout=5)

                await asyncio.sleep(8)

                # Check Loader devices
                quoted_suite_dir = shlex.quote(gms_suite_dir)
                check_cmd = f"cd {quoted_suite_dir} && ./upgrade_tool ld"
                output, _, _ = await asyncio.to_thread(runtime.ssh_manager.execute_command, ssh, check_cmd, timeout=5)

                if "List of rockusb connected(0)" in output or "List of rockusb connected" not in output:
                    await runtime.release_firmware_devices(client_id, locked_devices)
                    return error_response(f"No Loader devices detected. Output:\n{output}")

                # Burn firmware
                burn_cmd = f"cd {quoted_suite_dir} && ./upgrade_tool uf {shlex.quote(remote_firmware)}"

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
                    await runtime.release_firmware_devices(client_id, locked_devices)
                    # 设备锁已释放，通知前端刷新 ADB 设备状态（避免界面仍显示锁定）
                    if client_id in runtime.global_state.websocket_connections:
                        with contextlib.suppress(Exception):
                            await runtime.safe_websocket_send(client_id, {"type": "firmware_burn_complete", "devices": devices, "success": True})
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
                    await runtime.release_firmware_devices(client_id, locked_devices)
                    return error_response(error_output or "Firmware burn failed")

            except Exception as e:
                await runtime.release_firmware_devices(client_id, locked_devices)
                runtime.store_notification(client_id, "Firmware burn error", str(e)[:300], "error", "firmware", {"devices": devices, "firmware": firmware_name if 'firmware_name' in dir() else ""})
                return error_response(str(e))

    except Exception as e:
        import traceback
        logger.error(f"Error in burn_firmware: {e}")
        logger.error(f"Traceback: {traceback.format_exc()}")
        return error_response(str(e), 500)



@router.post("/api/burn/gsi")
async def burn_gsi(request: Request):
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
        if not script_path:
            return error_response("Script path is required")
        if not system_img:
            return error_response("System image path is required")

        locked_devices, lock_err = await _lock_devices(request, client_id, devices)
        if lock_err:
            return lock_err

        config = runtime.config_manager.load_config()
        with runtime.ssh_manager.optional_connection(config) as ssh:
            if not ssh:
                await runtime.release_firmware_devices(client_id, locked_devices)
                return error_response("SSH connection failed")

            try:
                import scp

                gms_suite_dir = get_default_suites_path(config)

                # Upload script
                local_script = os.path.join(runtime.project_root, "scripts", "run_GSI_Burn.sh")
                remote_script = os.path.join(gms_suite_dir, "run_GSI_Burn.sh")

                if os.path.exists(local_script):
                    def _upload_gsi_script():
                        scp_client = scp.SCPClient(ssh.get_transport())
                        scp_client.put(local_script, remote_script)
                        scp_client.close()
                        runtime.ssh_manager.execute_command(ssh, f"chmod +x {shlex.quote(remote_script)}")
                    await asyncio.to_thread(_upload_gsi_script)
                else:
                    return error_response(f"GSI burn script not found: {local_script}")

            # Upload misc.img
                local_misc = os.path.join(runtime.project_root, "tools", "misc.img")
                remote_misc = os.path.join(gms_suite_dir, "misc.img")
                if os.path.exists(local_misc):
                    scp_client = scp.SCPClient(ssh.get_transport())
                    scp_client.put(local_misc, remote_misc)
                    scp_client.close()

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
                        await runtime.safe_websocket_send(client_id, {"type": "log_update", "log": f"Starting GSI burn for {len(devices)} devices...", "log_type": "info"})

                for device in devices:
                    img_args = f"--system {shlex.quote(resolved_system)}"
                    if remote_vendor:
                        img_args += f" --vendor {shlex.quote(remote_vendor)}"

                    burn_cmd = f"{shlex.quote(remote_script)} {shlex.quote(device)} {img_args}"

                    if client_id in runtime.global_state.websocket_connections:
                        with contextlib.suppress(Exception):
                            await runtime.safe_websocket_send(client_id, {"type": "log_update", "log": f"Burning device: {device}", "log_type": "info"})

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

                    final_output = "".join(output_buffer)
                    exit_status = stdout.channel.recv_exit_status()
                    error_output = ""
                    if stderr.channel.recv_ready():
                        error_output = stderr.read().decode("utf-8", errors="ignore")

                    if exit_status == 0:
                        results.append({"device": device, "success": True, "output": final_output})
                        if client_id in runtime.global_state.websocket_connections:
                            with contextlib.suppress(Exception):
                                await runtime.safe_websocket_send(client_id, {"type": "log_update", "log": f"Device {device} GSI burn complete", "log_type": "success"})
                    else:
                        results.append({"device": device, "success": False, "error": error_output, "output": final_output})
                        if client_id in runtime.global_state.websocket_connections:
                            try:
                                error_msg = error_output or "Unknown error"
                                if final_output:
                                    lines = final_output.strip().split("\n")
                                    if lines:
                                        last_lines = lines[-3:]
                                        error_detail = " ".join(last_lines)
                                        if error_detail and len(error_detail) < 200:
                                            error_msg = error_detail
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
                        runtime.store_notification(client_id, "GSI burn complete", f"Devices: {', '.join(devices)}", "success", "firmware", {"devices": devices, "results": results})
                    except Exception as notify_error:
                        logger.warning("[GSI Burn] Failed to store success notification: %s", notify_error)
                    # 设备锁已释放，通知前端刷新 ADB 设备状态
                    if client_id in runtime.global_state.websocket_connections:
                        with contextlib.suppress(Exception):
                            await runtime.safe_websocket_send(client_id, {"type": "firmware_burn_complete", "devices": devices, "success": True})
                    return JSONResponse(content={"success": True, "message": "GSI burn completed successfully", "results": results})
                else:
                    failed = [r.get("device") for r in results if not r.get("success")]
                    try:
                        runtime.store_notification(client_id, "GSI burn failed", f"Failed: {', '.join(failed)}", "error", "firmware", {"devices": devices, "results": results})
                    except Exception as notify_error:
                        logger.warning("[GSI Burn] Failed to store failure notification: %s", notify_error)
                    return error_response("Some devices failed", results=results)

            except Exception as e:
                try:
                    runtime.store_notification(client_id, "GSI burn error", str(e)[:300], "error", "firmware", {"devices": devices})
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
async def burn_sn(req: SNBurnRequest):
    """SN burning - burn serial number to selected devices."""
    try:
        devices = req.devices
        sn_code = req.sn_code

        if not devices:
            return error_response("No devices selected", 400)
        if not sn_code:
            return error_response("SN code is required", 400)

        config = runtime.config_manager.load_config()
        with runtime.ssh_manager.optional_connection(config) as ssh:
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
