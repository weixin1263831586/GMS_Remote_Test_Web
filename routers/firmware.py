"""Firmware router - firmware burning, GSI burning, and serial number burning APIs."""

import os
import re
import time
import shutil
import asyncio
import logging
import subprocess
import threading
from datetime import datetime
from typing import Dict, Any, List, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from core.api_response import success_response, error_response
from core.config import config_manager
from core.ssh import ssh_manager
from core.schemas import SNBurnRequest
from core.devices import (
    SSHConnection,
    DeviceSSHConnection,
    release_device_locks,
    broadcast_device_lock_update,
    safe_websocket_send,
)
from core.error_handling import handle_api_errors
from core.state import global_state
from core.settings import GSI_PROGRESS_POLL_INTERVAL, GSI_PROGRESS_INCREMENT, GSI_PROGRESS_MAX, PROJECT_ROOT
from core.test_suite_utils import get_default_suites_path
from core.clients import get_client_id_from_request
from core.notifications import store_notification
from core.common_utils import strip_ansi_codes
from core.device_utils import DeviceUtils

from modules.device_lock_manager import device_lock_manager

logger = logging.getLogger(__name__)

router = APIRouter()

UPLOAD_PROGRESS_EXPIRATION = 10


def _generate_help_or_continue(help_flag: bool, method: str, path: str):
    if not help_flag:
        return None
    try:
        from routers.system import generate_per_api_help_text
        help_text = generate_per_api_help_text(method, path)
    except ImportError:
        return None
    if help_text:
        return PlainTextResponse(
            content=help_text,
            headers={"Content-Type": "text/plain; charset=utf-8", "Cache-Control": "public, max-age=300"},
        )
    return None


# ==================== Upload Progress ====================

@router.get("/api/burn/upload-progress")
async def get_firmware_upload_progress(request: Request):
    """Query firmware upload progress."""
    client_id = get_client_id_from_request(request)

    with global_state.firmware_upload_progress_lock:
        current_time = time.time()
        expired_clients = [
            cid for cid, data in global_state.firmware_upload_progress.items()
            if current_time - data["timestamp"] > UPLOAD_PROGRESS_EXPIRATION
        ]
        for cid in expired_clients:
            del global_state.firmware_upload_progress[cid]

        if client_id in global_state.firmware_upload_progress:
            progress_data = global_state.firmware_upload_progress[client_id]
            return JSONResponse(content={
                "in_progress": True,
                "progress": progress_data["progress"],
                "filename": progress_data["filename"],
                "uploaded_size": progress_data["uploaded_size"],
                "total_size": progress_data["total_size"],
            })
        else:
            return JSONResponse(content={"in_progress": False})


# ==================== Upload Helper ====================

async def _upload_firmware_to_test_host(ssh, client_id: str, source, remote_path: str, filename: str, file_size: int):
    """Upload firmware to test host with shared progress reporting."""
    import scp

    upload_progress_data = {"current_percentage": 0.0, "last_lock_update": 0.0}
    upload_complete = threading.Event()
    upload_error = [None]

    def update_global_progress(percentage: float, sent: int):
        if percentage - upload_progress_data["last_lock_update"] < 10:
            return
        try:
            with global_state.firmware_upload_progress_lock:
                global_state.firmware_upload_progress[client_id] = {
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
                try:
                    source.seek(0)
                except Exception:
                    pass
                scp_client.putfo(source, remote_path, size=file_size)
            else:
                scp_client.put(source, remote_path)
        except Exception as e:
            logger.error(f"[Firmware Burn] Upload error: {e}")
            upload_error[0] = str(e)
        finally:
            if scp_client:
                try:
                    scp_client.close()
                except Exception:
                    pass
            upload_complete.set()

    try:
        with global_state.firmware_upload_progress_lock:
            global_state.firmware_upload_progress[client_id] = {
                "progress": 0.0,
                "filename": filename,
                "uploaded_size": 0,
                "total_size": file_size,
                "timestamp": time.time(),
                "stage": "uploading_to_server",
            }

        await safe_websocket_send(client_id, {
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
                await safe_websocket_send(client_id, {
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

        await safe_websocket_send(client_id, {
            "type": "file_upload_progress",
            "filename": filename,
            "percentage": 100,
            "total_size": file_size,
            "uploaded_size": file_size,
        })
        await safe_websocket_send(client_id, {
            "type": "log_update",
            "log": "Firmware file upload complete",
            "log_type": "success",
        })
    finally:
        with global_state.firmware_upload_progress_lock:
            global_state.firmware_upload_progress.pop(client_id, None)


# ==================== Burn Firmware ====================

@router.post("/api/burn/firmware")
async def burn_firmware(request: Request, h: Optional[str] = Query(None), help: bool = Query(False)):
    """Firmware burning - supports file upload."""
    resp = _generate_help_or_continue(help, "POST", "/api/burn/firmware")
    if resp:
        return resp

    try:
        client_id = get_client_id_from_request(request)

        devices_param = request.query_params.get("devices")
        if devices_param:
            devices = devices_param.split(",")
        else:
            form = await request.form()
            devices_str = form.get("devices")
            devices = devices_str.split(",") if devices_str else []

        if not devices:
            return error_response("No devices selected")

        config = config_manager.load_config()
        username = config.get("client_username", "unknown")

        locked_devices = []
        failed_devices = []
        for device_id in devices:
            success, message = device_lock_manager.lock_device(device_id, client_id, username)
            if success:
                locked_devices.append(device_id)
            else:
                failed_devices.append({"device_id": device_id, "error": message})

        if failed_devices:
            await release_device_locks(client_id, locked_devices, broadcast=False)
            error_msg = "The following devices are occupied:\n"
            for fail in failed_devices:
                error_msg += f"- {fail['device_id']} ({fail['error']})\n"
            return JSONResponse(content={"success": False, "error": error_msg.strip(), "failed_devices": failed_devices}, status_code=409)

        await broadcast_device_lock_update(locked_devices)

        form = await request.form()
        firmware_file = form.get("firmware_file")
        firmware_path = form.get("firmware_path", "").strip()

        if not firmware_file and not firmware_path:
            await release_device_locks(client_id, locked_devices)
            return error_response("Please upload a firmware file or provide a firmware path")

        ssh = ssh_manager.get_connection(config)
        if not ssh:
            await release_device_locks(client_id, locked_devices)
            return error_response("SSH connection failed")

        try:
            # Upload upgrade_tool
            logger.info("[Firmware Burn] Uploading upgrade_tool...")
            import shlex
            gms_suite_dir = get_default_suites_path(config)
            local_tool = os.path.join(PROJECT_ROOT, "tools", "upgrade_tool")
            remote_tool = os.path.join(gms_suite_dir, "upgrade_tool")

            if not os.path.exists(local_tool):
                ssh_manager.return_connection(ssh)
                await release_device_locks(client_id, locked_devices)
                return error_response(f"upgrade_tool not found: {local_tool}")

            import scp
            scp_client = scp.SCPClient(ssh.get_transport())
            scp_client.put(local_tool, remote_tool)
            scp_client.close()

            # Handle firmware file
            if firmware_file:
                firmware_name = os.path.basename(firmware_file.filename or "").strip()
                if not firmware_name:
                    ssh_manager.return_connection(ssh)
                    await release_device_locks(client_id, locked_devices)
                    return error_response("Invalid firmware filename")

                firmware_stream = firmware_file.file
                try:
                    firmware_stream.seek(0, os.SEEK_END)
                    firmware_size = firmware_stream.tell()
                    firmware_stream.seek(0)
                except Exception as e:
                    ssh_manager.return_connection(ssh)
                    await release_device_locks(client_id, locked_devices)
                    return error_response(f"Failed to inspect firmware size: {e}")

                if firmware_size <= 0:
                    ssh_manager.return_connection(ssh)
                    await release_device_locks(client_id, locked_devices)
                    return error_response("Uploaded firmware file is empty")

                remote_firmware = os.path.join(gms_suite_dir, firmware_name)
                await _upload_firmware_to_test_host(ssh, client_id, firmware_stream, remote_firmware, firmware_name, firmware_size)
            else:
                firmware_name = os.path.basename(firmware_path.rstrip("/"))
                remote_firmware = os.path.join(gms_suite_dir, firmware_name)
                local_firmware_path = None

                if firmware_path.startswith("/") or firmware_path.startswith("./"):
                    quoted = shlex.quote(firmware_path)
                    check_cmd = f"test -f {quoted} && echo 'found' || echo 'not_found'"
                    output, _, _ = ssh_manager.execute_command(ssh, check_cmd, timeout=5)
                    if "found" in output:
                        remote_firmware = firmware_path
                    elif os.path.exists(firmware_path):
                        local_firmware_path = firmware_path
                    else:
                        ssh_manager.return_connection(ssh)
                        await release_device_locks(client_id, locked_devices)
                        return error_response(f"Firmware not found: {firmware_path}")
                elif os.path.exists(firmware_path):
                    local_firmware_path = firmware_path
                else:
                    remote_candidate = os.path.join(gms_suite_dir, firmware_path)
                    check_cmd = f"test -f {shlex.quote(remote_candidate)} && echo 'found' || echo 'not_found'"
                    output, _, _ = ssh_manager.execute_command(ssh, check_cmd, timeout=5)
                    if "found" in output:
                        remote_firmware = remote_candidate
                    else:
                        ssh_manager.return_connection(ssh)
                        await release_device_locks(client_id, locked_devices)
                        return error_response(f"Firmware not found: {firmware_path}")

                if local_firmware_path:
                    file_size = os.path.getsize(local_firmware_path)
                    if file_size <= 0:
                        ssh_manager.return_connection(ssh)
                        await release_device_locks(client_id, locked_devices)
                        return error_response("Firmware file is empty")
                    await _upload_firmware_to_test_host(ssh, client_id, local_firmware_path, remote_firmware, firmware_name, file_size)

            # Enter Loader mode
            for device in devices:
                cmd = f"adb -s {device} reboot loader"
                ssh_manager.execute_command(ssh, cmd, timeout=5)

            await asyncio.sleep(8)

            # Check Loader devices
            check_cmd = f"cd {gms_suite_dir} && ./upgrade_tool ld"
            output, _, _ = ssh_manager.execute_command(ssh, check_cmd, timeout=5)

            if "List of rockusb connected(0)" in output or "List of rockusb connected" not in output:
                ssh_manager.return_connection(ssh)
                await release_device_locks(client_id, locked_devices)
                return error_response(f"No Loader devices detected. Output:\n{output}")

            # Burn firmware
            burn_cmd = f"cd {gms_suite_dir} && ./upgrade_tool uf {shlex.quote(remote_firmware)}"

            if client_id in global_state.websocket_connections:
                try:
                    await safe_websocket_send(client_id, {"type": "log_update", "log": "Starting firmware burn...", "log_type": "info"})
                except Exception:
                    pass

            stdin, stdout, stderr = ssh.exec_command(burn_cmd, get_pty=True, timeout=300)
            output_buffer = []

            firmware_burn_start = False
            current_progress = 0
            last_progress_time = 0

            while not stdout.channel.exit_status_ready():
                current_time = asyncio.get_event_loop().time()

                if stdout.channel.recv_ready():
                    chunk = stdout.channel.recv(1024).decode("utf-8", errors="ignore")
                    output_buffer.append(chunk)
                    clean_chunk = strip_ansi_codes(chunk)

                    if "Download Firmware Start" in clean_chunk and not firmware_burn_start:
                        firmware_burn_start = True
                        current_progress = 0
                        last_progress_time = current_time

                    if client_id in global_state.websocket_connections:
                        try:
                            for line in clean_chunk.split("\n"):
                                line = line.strip()
                                if line:
                                    if firmware_burn_start:
                                        if any(kw in line.lower() for kw in ["error", "failed", "fail"]):
                                            await safe_websocket_send(client_id, {"type": "log_update", "log": line, "log_type": "error"})
                                        continue
                                    await safe_websocket_send(client_id, {"type": "log_update", "log": line, "log_type": "info"})
                        except Exception as e:
                            logger.error(f"[Firmware Burn] Log send failed: {e}")

                if firmware_burn_start and (current_time - last_progress_time > GSI_PROGRESS_POLL_INTERVAL):
                    current_progress = min(current_progress + GSI_PROGRESS_INCREMENT, GSI_PROGRESS_MAX)
                    last_progress_time = current_time

                    if client_id in global_state.websocket_connections:
                        try:
                            await safe_websocket_send(client_id, {"type": "firmware_progress", "percentage": current_progress})
                        except Exception:
                            pass

                await asyncio.sleep(0.1)

            final_output = "".join(output_buffer)
            exit_status = stdout.channel.recv_exit_status()

            if exit_status == 0:
                ssh_manager.return_connection(ssh)

                if client_id in global_state.websocket_connections:
                    try:
                        await safe_websocket_send(client_id, {"type": "firmware_progress", "percentage": 100})
                    except Exception:
                        pass
                    try:
                        await safe_websocket_send(client_id, {"type": "log_update", "log": "Firmware burn complete!", "log_type": "success"})
                    except Exception:
                        pass

                store_notification(client_id, "Firmware burn complete", f"Devices: {', '.join(devices)}", "success", "firmware", {"devices": devices, "firmware": firmware_name})
                await release_device_locks(client_id, locked_devices)
                return success_response(message="Firmware burn completed successfully")
            else:
                error_output = final_output or stderr.read().decode("utf-8", errors="ignore")
                ssh_manager.return_connection(ssh)

                if client_id in global_state.websocket_connections:
                    try:
                        await safe_websocket_send(client_id, {"type": "log_update", "log": f"Firmware burn failed (exit code: {exit_status})", "log_type": "error"})
                        if error_output and len(error_output) < 500:
                            await safe_websocket_send(client_id, {"type": "log_update", "log": f"Error: {error_output[:200]}", "log_type": "error"})
                    except Exception:
                        pass

                store_notification(client_id, "Firmware burn failed", (error_output or "Burn failed")[:300], "error", "firmware", {"devices": devices, "firmware": firmware_name, "exit_status": exit_status})
                await release_device_locks(client_id, locked_devices)
                return error_response(error_output or "Firmware burn failed")

        except Exception as e:
            ssh_manager.return_connection(ssh)
            await release_device_locks(client_id, locked_devices)
            store_notification(client_id, "Firmware burn error", str(e)[:300], "error", "firmware", {"devices": devices, "firmware": firmware_name if 'firmware_name' in dir() else ""})
            return error_response(str(e))

    except Exception as e:
        import traceback
        logger.error(f"Error in burn_firmware: {e}")
        logger.error(f"Traceback: {traceback.format_exc()}")
        return error_response(str(e), 500)


# ==================== Burn GSI ====================

@router.post("/api/burn/gsi")
async def burn_gsi(request: Request):
    """GSI burning using run_GSI_Burn.sh script."""
    try:
        client_id = get_client_id_from_request(request)
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

        config = config_manager.load_config()
        username = config.get("client_username", "unknown")

        locked_devices = []
        failed_devices = []
        for device_id in devices:
            success, message = device_lock_manager.lock_device(device_id, client_id, username)
            if success:
                locked_devices.append(device_id)
            else:
                failed_devices.append({"device_id": device_id, "error": message})

        if failed_devices:
            await release_device_locks(client_id, locked_devices, broadcast=False)
            error_msg = "Devices occupied:\n"
            for fail in failed_devices:
                error_msg += f"- {fail['device_id']} ({fail['error']})\n"
            return JSONResponse(content={"success": False, "error": error_msg.strip(), "failed_devices": failed_devices}, status_code=409)

        await broadcast_device_lock_update(locked_devices)

        config = config_manager.load_config()
        ssh = ssh_manager.get_connection(config)
        if not ssh:
            await release_device_locks(client_id, locked_devices)
            return error_response("SSH connection failed")

        try:
            import scp
            import shlex

            gms_suite_dir = get_default_suites_path(config)

            # Upload script
            local_script = os.path.join(PROJECT_ROOT, "scripts", "run_GSI_Burn.sh")
            remote_script = os.path.join(gms_suite_dir, "run_GSI_Burn.sh")

            if os.path.exists(local_script):
                scp_client = scp.SCPClient(ssh.get_transport())
                scp_client.put(local_script, remote_script)
                scp_client.close()
                ssh_manager.execute_command(ssh, f"chmod +x {remote_script}")
            else:
                ssh_manager.return_connection(ssh)
                return error_response(f"GSI burn script not found: {local_script}")

            # Upload misc.img
            local_misc = os.path.join(PROJECT_ROOT, "tools", "misc.img")
            remote_misc = os.path.join(gms_suite_dir, "misc.img")
            if os.path.exists(local_misc):
                scp_client = scp.SCPClient(ssh.get_transport())
                scp_client.put(local_misc, remote_misc)
                scp_client.close()

            # Handle vendor image
            remote_vendor = ""
            if vendor_img:
                if os.path.exists(vendor_img):
                    vendor_name = os.path.basename(vendor_img)
                    remote_vendor = os.path.join(gms_suite_dir, vendor_name)
                    scp_client = scp.SCPClient(ssh.get_transport())
                    scp_client.put(vendor_img, remote_vendor)
                    scp_client.close()
                else:
                    remote_vendor = vendor_img

            results = []

            if client_id in global_state.websocket_connections:
                try:
                    await safe_websocket_send(client_id, {"type": "log_update", "log": f"Starting GSI burn for {len(devices)} devices...", "log_type": "info"})
                except Exception:
                    pass

            for device in devices:
                img_args = f"--system {system_img}"
                if remote_vendor:
                    img_args += f" --vendor {remote_vendor}"

                burn_cmd = f"{remote_script} {device} {img_args}"

                if client_id in global_state.websocket_connections:
                    try:
                        await safe_websocket_send(client_id, {"type": "log_update", "log": f"Burning device: {device}", "log_type": "info"})
                    except Exception:
                        pass

                stdin, stdout, stderr = ssh.exec_command(burn_cmd, get_pty=True, timeout=600)
                output_buffer = []

                while not stdout.channel.exit_status_ready():
                    if stdout.channel.recv_ready():
                        chunk = stdout.channel.recv(1024).decode("utf-8", errors="ignore")
                        output_buffer.append(chunk)
                        clean_chunk = strip_ansi_codes(chunk)

                        if client_id in global_state.websocket_connections:
                            try:
                                for line in clean_chunk.split("\n"):
                                    line = line.strip()
                                    if line:
                                        await safe_websocket_send(client_id, {"type": "log_update", "log": line, "log_type": "info"})
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
                    if client_id in global_state.websocket_connections:
                        try:
                            await safe_websocket_send(client_id, {"type": "log_update", "log": f"Device {device} GSI burn complete", "log_type": "success"})
                        except Exception:
                            pass
                else:
                    results.append({"device": device, "success": False, "error": error_output, "output": final_output})
                    if client_id in global_state.websocket_connections:
                        try:
                            error_msg = error_output or "Unknown error"
                            if final_output:
                                lines = final_output.strip().split("\n")
                                if lines:
                                    last_lines = lines[-3:]
                                    error_detail = " ".join(last_lines)
                                    if error_detail and len(error_detail) < 200:
                                        error_msg = error_detail
                            await safe_websocket_send(client_id, {"type": "log_update", "log": f"Device {device} GSI burn failed: {error_msg}", "log_type": "error"})
                        except Exception:
                            pass

            ssh_manager.return_connection(ssh)
            await release_device_locks(client_id, locked_devices)

            all_success = all(r["success"] for r in results)
            if all_success:
                store_notification(client_id, "GSI burn complete", f"Devices: {', '.join(devices)}", "success", "firmware", {"devices": devices, "results": results})
                return JSONResponse(content={"success": True, "message": "GSI burn completed successfully", "results": results})
            else:
                failed = [r.get("device") for r in results if not r.get("success")]
                store_notification(client_id, "GSI burn failed", f"Failed: {', '.join(failed)}", "error", "firmware", {"devices": devices, "results": results})
                return JSONResponse(content={"success": False, "error": "Some devices failed", "results": results})

        except Exception as e:
            ssh_manager.return_connection(ssh)
            store_notification(client_id, "GSI burn error", str(e)[:300], "error", "firmware", {"devices": devices})
            await release_device_locks(client_id, locked_devices)
            return error_response(str(e))

    except Exception as e:
        logger.error(f"Error in burn_gsi: {e}")
        return error_response(str(e), 500)


# ==================== Burn Serial Number ====================

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

        config = config_manager.load_config()
        ssh = ssh_manager.get_connection(config)
        if not ssh:
            return error_response("SSH connection failed", 500)

        try:
            results = []
            for device_id in devices:
                results.append({
                    "device": device_id,
                    "success": False,
                    "error": "SN burning requires device in loader mode. Feature needs specific tool support.",
                })

            ssh_manager.return_connection(ssh)
            return JSONResponse(content={"success": True, "results": results})
        except Exception as e:
            ssh_manager.return_connection(ssh)
            raise e
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error burning SN: {e}")
        raise HTTPException(status_code=500, detail=str(e))
