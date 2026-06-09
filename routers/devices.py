"""Devices router - device management APIs."""

import os
import re
import shlex
import subprocess
import time
import asyncio
import logging
from datetime import datetime
from typing import Dict, Any, List, Optional, Tuple

from fastapi import APIRouter, HTTPException, Query, Request, Body
from fastapi.responses import JSONResponse

from core.config import config_manager
from core.ssh import ssh_manager
from core.devices import device_manager
from core.devices import (
    SSHConnection,
    DeviceSSHConnection,
    get_or_create_user_state,
    get_device_properties_optimized,
    release_device_locks,
    broadcast_device_lock_update,
    safe_websocket_send,
    ssh_connection_failed_response,
)
from core.error_handling import handle_api_errors
from core.api_help import generate_help_or_continue
from core.device_utils import DeviceUtils
from core.schemas import DeviceActionRequest, DeviceLockRequest, DeviceShellRequest, WifiConnectRequest
from core.enums import VerifiedBootState
from core.state import global_state
from core.clients import get_client_id_from_request, get_client_ip, parse_client_id
from core.notifications import store_notification, safe_websocket_send as _ws_send
from modules.device_lock_manager import device_lock_manager
from modules.client_manager import client_manager
from core.test_suite_utils import get_default_suites_path
from core.usbip import wait_for_adb_serial_ready
from core.settings import DEVICE_CACHE_TTL, PROJECT_ROOT
from core.network import run_local_shell_command

logger = logging.getLogger(__name__)

router = APIRouter()




@router.get("/api/devices/list")
@handle_api_errors
async def get_connected_devices(
    request: Request,
    help: bool = Query(False),
    force_refresh: bool = Query(False),
):
    """Get all connected device list (same as adb devices)."""
    resp = generate_help_or_continue(help, "GET", "/api/devices/list")
    if resp:
        return resp

    # Track user access
    client_id = get_client_id_from_request(request)
    get_or_create_user_state(client_id)

    # Refresh device list first
    devices = await asyncio.to_thread(device_manager.get_connected_devices, force_refresh)

    # Clean up removed device source records
    current_device_set = set(devices)
    devices_to_remove = [
        dev_id
        for dev_id in global_state.usbip_devices_source.keys()
        if dev_id not in current_device_set
    ]
    if devices_to_remove:
        logger.info(f"[Devices API] Cleaning up removed devices: {devices_to_remove}")
        with global_state.usbip_devices_source_lock:
            for dev_id in devices_to_remove:
                del global_state.usbip_devices_source[dev_id]

    # Check cache
    now = datetime.now().timestamp()
    if not force_refresh and now - global_state.device_cache["timestamp"] < DEVICE_CACHE_TTL:
        cached_devices = global_state.device_cache["devices"]
        cached_device_set = {
            item.get("device_id")
            for item in cached_devices
            if isinstance(item, dict) and item.get("device_id")
        }
        if cached_device_set == current_device_set:
            return JSONResponse(content=cached_devices)

    devices_with_status = []

    for device_id in devices:
        device_info = {"device_id": device_id, "status": "online", "locked": False}

        # Check lock status
        client_ip = get_client_ip(request)
        client_id_from_ip = client_manager.get_client_id(client_ip)
        lock_status = device_lock_manager.get_lock_status(device_id)

        if lock_status:
            device_info["locked"] = True
            device_info["locked_by"] = lock_status["locked_by"]
            device_info["locked_by_self"] = lock_status.get("client_id") == client_id_from_ip
            device_info["locked_at"] = lock_status["locked_at"]
        else:
            device_info["locked_by"] = ""
            device_info["locked_by_self"] = False

        # Check USB/IP source
        if device_id in global_state.usbip_devices_source:
            source = global_state.usbip_devices_source[device_id]
            device_info["source"] = source["source"]
            device_info["is_usbip"] = True

        devices_with_status.append(device_info)

    # Update cache
    with global_state.device_cache_lock:
        global_state.device_cache = {"devices": devices_with_status, "timestamp": now}

    return JSONResponse(content=devices_with_status)


async def _manage_bootloader_lock(devices: List[str], action: str) -> JSONResponse:
    """Common bootloader lock/unlock handler."""
    from core.api_response import ApiResponse

    try:
        if not devices:
            return ApiResponse.error("No devices selected", status_code=400)

        valid_device_pattern = re.compile(r"^[a-zA-Z0-9.:-]+$")
        for device_id in devices:
            if not valid_device_pattern.match(device_id):
                return ApiResponse.error(
                    f"Invalid device ID format: {device_id}", status_code=400
                )

        config = config_manager.load_config()

        with ssh_manager.connection(config) as ssh:
            results = []

            local_script = os.path.join(
                PROJECT_ROOT, "scripts", "run_Device_Lock.sh"
            )
            remote_script = os.path.join(get_default_suites_path(config), "run_Device_Lock.sh")

            if not os.path.exists(local_script):
                return ApiResponse.error(
                    f"Script file not found: {local_script}", status_code=404
                )

            try:
                with ssh.open_sftp() as sftp:
                    sftp.put(local_script, remote_script)
                ssh_manager.execute_command(ssh, f"chmod +x '{remote_script}'")
            except Exception as e:
                return ApiResponse.error(
                    f"Script upload failed: {str(e)}", status_code=500
                )

            for device_id in devices:
                try:
                    cmd = f"bash '{remote_script}' '{device_id}' '{action}'"
                    output, error, code = ssh_manager.execute_command(ssh, cmd)

                    if code == 0:
                        start_time = time.time()
                        while time.time() - start_time < 60:
                            check_cmd = f"adb -s {device_id} get-state"
                            check_output, _, check_code = ssh_manager.execute_command(
                                ssh, check_cmd
                            )
                            if "device" in check_output.lower():
                                break
                    await asyncio.sleep(2)

                    results.append(
                        {
                            "device": device_id,
                            "success": code == 0,
                            "output": output[-200:] if output else error,
                        }
                    )
                except Exception as e:
                    results.append({"device": device_id, "success": False, "error": str(e)})

            success_count = sum(1 for r in results if r.get("success", False))
            failed_count = len(results) - success_count

            response_data = {
                "results": results,
                "summary": {
                    "total": len(results),
                    "success": success_count,
                    "failed": failed_count,
                },
            }

            action_text = "lock" if action == "lock" else "unlock"
            return ApiResponse.success(response_data, f"Device {action_text} operation completed")

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error managing device lock: {e}")
        return ApiResponse.error(str(e), status_code=500)


@router.post("/api/devices/bootloader-lock")
async def lock_bootloader(
    request: Request,
    help: bool = Query(False),
    req: DeviceLockRequest = Body(None),
):
    """Lock device Bootloader."""
    resp = generate_help_or_continue(help, "POST", "/api/devices/bootloader-lock")
    if resp:
        return resp

    devices = req.devices if req.devices else []
    if req.device_id:
        devices = [req.device_id]

    return await _manage_bootloader_lock(devices, "lock")


@router.post("/api/devices/bootloader-unlock")
async def unlock_bootloader(
    request: Request,
    help: bool = Query(False),
    req: DeviceLockRequest = Body(None),
):
    """Unlock device Bootloader."""
    resp = generate_help_or_continue(help, "POST", "/api/devices/bootloader-unlock")
    if resp:
        return resp

    devices = req.devices if req.devices else []
    if req.device_id:
        devices = [req.device_id]

    return await _manage_bootloader_lock(devices, "unlock")


@router.post("/api/devices/bootloader-status")
async def check_bootloader_status(req: DeviceActionRequest):
    """Check device Bootloader lock status (GREEN=locked, ORANGE=unlocked)."""
    from core.api_response import ApiResponse

    try:
        with SSHConnection() as ssh:
            async def check_single_device(device_id: str) -> Dict:
                output, error, code = ssh_manager.execute_command(
                    ssh,
                    f"adb -s {device_id} shell getprop ro.boot.verifiedbootstate",
                )
                state = output.strip()

                try:
                    boot_state = VerifiedBootState(state)
                    is_locked = boot_state.is_locked
                    status_text = boot_state.display_text
                except ValueError:
                    is_locked = False
                    status_text = f"Unknown state ({state})"

                return {
                    "device": device_id,
                    "locked": is_locked,
                    "state": state,
                    "status": status_text,
                }

            results = await asyncio.gather(
                *[check_single_device(d) for d in req.devices]
            )

            return ApiResponse.success({"results": results}, "Lock status check completed")

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error checking lock status: {e}")
        return ApiResponse.error(str(e), status_code=500)


@router.post("/api/devices/info")
async def get_device_info(req: DeviceActionRequest):
    """Get device detailed information."""
    from core.api_response import ApiResponse

    try:
        with SSHConnection() as ssh:
            async def get_single_device_info(device_id: str) -> Dict:
                device_info = {"device": device_id, "properties": {}}

                base_info = device_manager.get_device_info(device_id, ssh)

                field_mapping = {
                    "serial_no": "Serial Number",
                    "model": "Model",
                    "android_version": "Android Version",
                    "fingerprint": "Fingerprint",
                    "build_type": "Build Type",
                    "build_tags": "Build Tags",
                    "build_date": "Build Date",
                    "sdk_version": "SDK Version",
                    "security_patch": "Security Patch",
                }

                for key, label in field_mapping.items():
                    if key in base_info:
                        device_info["properties"][label] = base_info[key]

                extra_props = await get_device_properties_optimized(device_id, ssh)

                prop_mapping = {
                    "boot_state": ("Boot State", lambda x: x if x else "Unknown"),
                    "api_level": (
                        "API Level",
                        lambda x: x.split("[")[-1].replace("]", "")
                        if "[" in x
                        else (x or "Unknown"),
                    ),
                    "mali_version": ("Mali Version", lambda x: x or "Unknown"),
                    "mem_total": ("Total Memory", lambda x: f"{x} KB" if x else "Unknown"),
                    "mem_free": ("Free Memory", lambda x: f"{x} KB" if x else "Unknown"),
                    "timezone": ("Timezone", lambda x: x or "Unknown"),
                    "locale": ("Language", lambda x: x or "Unknown"),
                    "data_partition": (
                        "DATA Partition",
                        lambda x: x.split()[-1] if x and "userdata" in x else "Unknown",
                    ),
                }

                for key, (label, formatter) in prop_mapping.items():
                    if key in extra_props:
                        device_info["properties"][label] = formatter(extra_props[key])

                return device_info

            results = await asyncio.gather(
                *[get_single_device_info(d) for d in req.devices]
            )

            return ApiResponse.success({"results": results}, "Device info retrieved")

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting device info: {e}")
        return ApiResponse.error(str(e), status_code=500)


def _build_management_props_command(device_ids: List[str]) -> str:
    """Build command to fetch device management properties."""
    commands = []
    for device_id in device_ids:
        device_shell = (
            f'echo "===DEVICE:{device_id}===" && '
            "getprop ro.serialno && "
            "getprop ro.product.model && "
            "getprop ro.build.version.release && "
            'dumpsys battery | grep "^  level:" | cut -d: -f2 | tr -d " " && '
            "getprop ro.soc.model"
        )
        commands.append(
            f"adb -s {shlex.quote(device_id)} shell {shlex.quote(device_shell)}"
        )
    return " ; ".join(commands)


def _parse_management_device_props(props_output: str) -> Dict[str, Dict[str, str]]:
    """Parse device properties output from management command."""
    device_data: Dict[str, Dict[str, str]] = {}
    current_device = None

    for line in props_output.split("\n"):
        line = line.strip()
        if line.startswith("===DEVICE:"):
            current_device = line.split("===DEVICE:")[1].split("===")[0]
            device_data[current_device] = {
                "serial_no": "",
                "model": "",
                "android_version": "",
                "battery_level": "",
                "soc_model": "",
            }
        elif current_device and line:
            if not device_data[current_device]["serial_no"]:
                device_data[current_device]["serial_no"] = line
            elif not device_data[current_device]["model"]:
                device_data[current_device]["model"] = line
            elif not device_data[current_device]["android_version"]:
                device_data[current_device]["android_version"] = line
            elif not device_data[current_device]["battery_level"]:
                device_data[current_device]["battery_level"] = line
            elif not device_data[current_device]["soc_model"]:
                device_data[current_device]["soc_model"] = line

    return device_data


def _build_devices_management_payload(
    device_ids: List[str],
    device_data: Dict[str, Dict[str, str]],
    config: Dict[str, Any],
) -> Dict[str, Any]:
    """Build the management payload for all devices."""
    from core.usbip import usbip_manager

    client_id = client_manager.get_client_id("127.0.0.1")
    locks = device_lock_manager.get_all_locks()
    devices_info = []
    ubuntu_host = config_manager.get_ubuntu_host(config)
    ubuntu_user = config_manager.get_ubuntu_user(config)

    all_usbip_sources = {**global_state.usbip_devices_source, **usbip_manager.device_sources}
    current_device_set = set(device_ids)
    devices_to_remove = [
        dev_id for dev_id in all_usbip_sources if dev_id not in current_device_set
    ]

    if devices_to_remove:
        logger.info(
            f"[Device Management] Cleaning up removed devices from memory: {devices_to_remove}"
        )
        with global_state.usbip_devices_source_lock:
            for dev_id in devices_to_remove:
                global_state.usbip_devices_source.pop(dev_id, None)
        for dev_id in devices_to_remove:
            usbip_manager.device_sources.pop(dev_id, None)

    for device_id in device_ids:
        props = device_data.get(device_id, {})
        lock_info = locks.get(device_id, {})

        if device_id in all_usbip_sources:
            source_type = "usbip"
            source_host = all_usbip_sources.get(device_id, {}).get("source", "Unknown")
        else:
            source_type = "local"
            source_host = f"{ubuntu_user}@{ubuntu_host}"

        devices_info.append(
            {
                "device_id": device_id,
                "serial_no": props.get("serial_no", device_id),
                "model": props.get("model", ""),
                "android_version": props.get("android_version", ""),
                "battery_level": props.get("battery_level", ""),
                "soc_model": props.get("soc_model", ""),
                "source_type": source_type,
                "source_host": source_host,
                "status": "online",
                "locked_by": lock_info.get("client_id", "") if device_id in locks else "",
                "locked_by_self": (
                    lock_info.get("client_id") == client_id
                    if device_id in locks
                    else False
                ),
            }
        )

    return {"devices": devices_info}


@router.get("/api/devices/management")
async def devices_management():
    """Device management page - get detailed management info for all devices."""
    from core.test_suite_utils import is_config_host_local

    try:
        config = config_manager.load_config()

        if is_config_host_local(config):
            output, error, code = await asyncio.to_thread(
                run_local_shell_command, "adb devices", 5
            )
            if code != 0 and not output:
                logger.warning(
                    f"[Device Management] Local adb devices failed: {error}"
                )
                return JSONResponse(
                    content={"devices": [], "success": True, "warning": error}
                )

            device_ids = DeviceUtils.parse_adb_devices(output)
            if not device_ids:
                return JSONResponse(
                    content={"devices": [], "success": True, "source": "local"}
                )

            props_cmd = _build_management_props_command(device_ids)
            props_output, props_error, props_code = await asyncio.to_thread(
                run_local_shell_command, props_cmd, 15
            )
            if props_code != 0:
                logger.warning(
                    f"[Device Management] Local device properties failed: {props_error}"
                )

            payload = _build_devices_management_payload(
                device_ids, _parse_management_device_props(props_output), config
            )
            payload.update({"success": True, "source": "local"})
            return JSONResponse(content=payload)

        with SSHConnection(config) as ssh:
            output, _, _ = ssh_manager.execute_command(ssh, "adb devices", timeout=5)
            device_ids = DeviceUtils.parse_adb_devices(output)

            if not device_ids:
                return JSONResponse(content={"devices": []})

            props_cmd = _build_management_props_command(device_ids)
            props_output, _, _ = ssh_manager.execute_command(ssh, props_cmd, timeout=15)

            return JSONResponse(
                content=_build_devices_management_payload(
                    device_ids,
                    _parse_management_device_props(props_output),
                    config,
                )
            )

    except Exception as e:
        logger.error(f"Error getting devices management: {e}")
        return JSONResponse(
            content={"success": False, "error": str(e)}, status_code=500
        )


@router.get("/api/devices/user-locked")
async def list_user_locks():
    """List all user-locked devices."""
    return JSONResponse(
        content={"success": True, "data": device_lock_manager.get_all_locks()}
    )


@router.post("/api/devices/reboot")
@handle_api_errors
async def reboot_devices(req: DeviceActionRequest):
    """Reboot devices."""
    from core.api_response import ApiResponse

    with SSHConnection() as ssh:
        async def reboot_single_device(device_id: str) -> Dict:
            result = device_manager.reboot_device(device_id, ssh)
            result["device"] = device_id
            return result

        results = await asyncio.gather(
            *[reboot_single_device(d) for d in req.devices]
        )
        return ApiResponse.device_results(results, "Device reboot")


@router.post("/api/devices/remount")
@handle_api_errors
async def remount_devices(req: DeviceActionRequest, request: Request):
    """Remount devices."""
    from core.api_response import ApiResponse

    client_id = get_client_id_from_request(request)

    with SSHConnection() as ssh:
        async def remount_single_device(device_id: str) -> Dict:
            output, error, code = ssh_manager.execute_command(
                ssh, f"adb -s {device_id} root", timeout=15
            )

            await safe_websocket_send(
                client_id,
                {
                    "type": "log_update",
                    "log": f"[{device_id}] adb root: {output.strip()}",
                    "log_type": "info",
                },
            )

            await asyncio.sleep(2)

            output, error, code = ssh_manager.execute_command(
                ssh, f"adb -s {device_id} remount", timeout=15
            )

            await safe_websocket_send(
                client_id,
                {
                    "type": "log_update",
                    "log": f"[{device_id}] adb remount: {output.strip()}",
                    "log_type": "info",
                },
            )

            result = device_manager.remount_device(device_id, ssh)
            result["device"] = device_id
            return result

        results = await asyncio.gather(
            *[remount_single_device(d) for d in req.devices]
        )
        return ApiResponse.device_results(results, "Device Remount")


@router.post("/api/devices/wifi")
async def connect_wifi(req: WifiConnectRequest):
    """Connect to WiFi."""
    try:
        config = config_manager.load_config()
        ssh = ssh_manager.get_connection(config)
        if not ssh:
            raise HTTPException(status_code=500, detail="SSH connection failed")

        results = []
        for device_id in req.devices:
            enable_cmd = f"adb -s {device_id} shell cmd wifi set-wifi-enabled enabled"
            connect_cmd = (
                f'adb -s {device_id} shell cmd wifi connect-network "{req.ssid}" wpa2 "{req.password}"'
            )
            full_cmd = f"{enable_cmd} && sleep 2 && {connect_cmd}"

            output, error, code = ssh_manager.execute_command(ssh, full_cmd)
            results.append({"device": device_id, "success": code == 0})

        ssh_manager.return_connection(ssh)

        success_count = sum(1 for r in results if r.get("success", False))
        return JSONResponse(
            content={
                "success": True,
                "results": results,
                "summary": {
                    "total": len(results),
                    "success": success_count,
                    "failed": len(results) - success_count,
                },
            }
        )
    except Exception as e:
        logger.error(f"Error connecting WiFi: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"{str(e)}. Please check configuration and parameters.",
        )


@router.post("/api/devices/shell")
async def open_device_shell(req: DeviceShellRequest, request: Request):
    """Open device ADB Shell - prepare device connection for terminal page."""
    try:
        config = config_manager.load_config()
        ssh = ssh_manager.get_connection(config)
        if not ssh:
            return JSONResponse(
                content={"success": False, "message": "SSH connection failed"},
                status_code=500,
            )

        ready_result = await asyncio.to_thread(
            wait_for_adb_serial_ready, ssh, req.serial_no, 30
        )

        ssh_manager.return_connection(ssh)

        if ready_result.get("ready"):
            client_id = get_client_id_from_request(request)

            if not hasattr(global_state, "device_shells"):
                global_state.device_shells = {}

            global_state.device_shells[client_id] = {
                "serial_no": req.serial_no,
                "connected_at": datetime.now().isoformat(),
            }

            return JSONResponse(
                content={
                    "success": True,
                    "message": f"Device {req.serial_no} is ready",
                    "serial_no": req.serial_no,
                }
            )
        else:
            detail = (
                ready_result.get("state")
                or ready_result.get("devices")
                or "No response"
            )
            logger.warning(
                f"[Device Shell] Device {req.serial_no} not ready: {detail}"
            )
            return JSONResponse(
                content={
                    "success": False,
                    "message": f"Device {req.serial_no} is not online or unresponsive: {detail}",
                },
                status_code=400,
            )
    except Exception as e:
        logger.error(f"Error opening device shell: {e}")
        return JSONResponse(
            content={"success": False, "message": f"Failed to open shell: {str(e)}"},
            status_code=500,
        )


@router.post("/api/devices/scrcpy")
async def show_device_screens(req: DeviceActionRequest):
    """Display device screen (launch scrcpy mirroring)."""
    from core.vnc import vnc_manager
    from core.usbip import usbip_manager

    try:
        devices = req.devices

        config = config_manager.load_config()
        ubuntu_user = config_manager.get_ubuntu_user(config)
        ubuntu_host = config_manager.get_ubuntu_host(config)

        if not devices:
            ssh = ssh_manager.get_connection(config)
            if ssh:
                try:
                    stdout, stderr, code = ssh_manager.execute_command(
                        ssh, "adb devices", timeout=5
                    )
                    ssh_manager.return_connection(ssh)
                    if code == 0 and stdout:
                        lines = stdout.strip().split("\n")[1:]
                        devices = [
                            line.split()[0]
                            for line in lines
                            if line.strip() and "\tdevice" in line
                        ]
                except Exception:
                    pass

        if not devices:
            return JSONResponse(
                content={"success": False, "error": "No devices selected"},
                status_code=400,
            )

        ssh = ssh_manager.get_connection(config)
        if not ssh:
            return ssh_connection_failed_response()

        try:
            vnc_check_cmd = (
                f"curl -s -o /dev/null -w '%{{http_code}}' http://{ubuntu_host}:6080 --connect-timeout 3"
            )
            vnc_output, _, _ = ssh_manager.execute_command(ssh, vnc_check_cmd, timeout=5)
            vnc_available = vnc_output.strip() == "200"

            scrcpy_path = config.get("scrcpy_path", "")
            if scrcpy_path:
                scrcpy_path = scrcpy_path.replace("${ubuntu_user}", ubuntu_user)
                scrcpy_check_cmd = (
                    f"test -f '{scrcpy_path}' && echo 'exists' || echo 'not_found'"
                )
                scrcpy_output, _, scrcpy_code = ssh_manager.execute_command(
                    ssh, scrcpy_check_cmd
                )

                if "not_found" in scrcpy_output:
                    ssh_manager.return_connection(ssh)
                    return JSONResponse(
                        content={
                            "success": False,
                            "error": f"scrcpy not found: {scrcpy_path}",
                            "instructions": "Please check scrcpy_path in config",
                        },
                        status_code=404,
                    )
            else:
                scrcpy_check_cmd = "which scrcpy"
                scrcpy_output, _, scrcpy_code = ssh_manager.execute_command(
                    ssh, scrcpy_check_cmd
                )

                if scrcpy_code != 0:
                    ssh_manager.return_connection(ssh)
                    return JSONResponse(
                        content={
                            "success": False,
                            "error": "scrcpy not installed",
                            "instructions": "sudo apt-get install -y scrcpy",
                        },
                        status_code=404,
                    )
                scrcpy_path = "scrcpy"

            results = []
            vnc_sessions = []

            existing_devices = []
            for device_id in devices:
                is_healthy, pid_or_error = DeviceUtils.check_scrcpy_healthy(
                    ssh, device_id
                )

                if is_healthy and pid_or_error:
                    existing_devices.append(device_id)
                    logger.info(
                        f"Detected already mirrored device: {device_id} (PID: {pid_or_error})"
                    )
                else:
                    DeviceUtils.kill_process(
                        ssh, f"scrcpy.*-s {device_id}"
                    )

            new_devices = [d for d in devices if d not in existing_devices]

            if not new_devices:
                ssh_manager.return_connection(ssh)
                return JSONResponse(
                    content={
                        "success": True,
                        "message": f"All {len(devices)} devices already being mirrored",
                        "results": [
                            {
                                "device": d,
                                "started": False,
                                "already_running": True,
                            }
                            for d in devices
                        ],
                        "vnc_sessions": [
                            {"device": d, "message": "Already running"} for d in devices
                        ],
                        "note": "All devices already being mirrored",
                    }
                )

            positions = DeviceUtils.calculate_window_positions(
                existing_devices + new_devices, max_window_width=350
            )

            for idx, device_id in enumerate(sorted(existing_devices + new_devices)):
                if device_id not in new_devices:
                    continue

                x_offset = positions["start_x"] + idx * (
                    positions["window_width"] + positions["horizontal_gap"]
                )
                y_offset = positions["start_y"]
                window_width = positions["window_width"]
                window_height = positions["window_height"]
                cmd = (
                    f"export DISPLAY=:0 && "
                    f"if [ -f /run/user/1000/gdm/Xauthority ]; then "
                    f"export XAUTHORITY=/run/user/1000/gdm/Xauthority; "
                    f"else "
                    f"export XAUTHORITY=/home/{ubuntu_user}/.Xauthority; "
                    f"fi && "
                    f"(nohup {scrcpy_path} -s {device_id} "
                    f"--max-size 800 "
                    f"--stay-awake "
                    f"--window-title '{device_id}' "
                    f"--window-x {x_offset} "
                    f"--window-y {y_offset} "
                    f"--window-width {window_width} "
                    f"--window-height {window_height} "
                    f"> /tmp/scrcpy_{device_id}.log 2>&1 &)"
                )

                ssh_manager.execute_command(ssh, cmd, timeout=10)

                await asyncio.sleep(0.3)
                check_cmd = (
                    f"pgrep -f 'scrcpy.*-s {device_id}' && echo 'RUNNING' || echo 'NOT_RUNNING'"
                )
                check_output, _, _ = ssh_manager.execute_command(
                    ssh, check_cmd, timeout=5
                )
                is_started = "RUNNING" in check_output

                results.append(
                    {
                        "device": device_id,
                        "started": is_started,
                        "position": {
                            "x": x_offset,
                            "y": y_offset,
                            "width": window_width,
                            "height": window_height,
                        },
                    }
                )

                vnc_sessions.append(
                    {
                        "device": device_id,
                        "url": (
                            f"http://{ubuntu_host}:6080/vnc.html?autoconnect=true"
                            if vnc_available
                            else None
                        ),
                        "message": "VNC view available" if vnc_available else "Local display only",
                    }
                )

            ssh_manager.return_connection(ssh)

            newly_started = [r["device"] for r in results if r.get("started")]
            failed_devices = [r["device"] for r in results if not r.get("started")]

            message_parts = []
            if newly_started:
                message_parts.append(
                    f"Started {len(newly_started)} screen mirrors: {', '.join(newly_started)}"
                )
            if failed_devices:
                message_parts.append(
                    f"{len(failed_devices)} devices failed to start: {', '.join(failed_devices)}"
                )

            message = "\n".join(message_parts) if message_parts else "Screen mirror started"

            return JSONResponse(
                content={
                    "success": len(failed_devices) == 0,
                    "message": message,
                    "results": results,
                    "vnc_sessions": vnc_sessions,
                    "desktop_url": "/desktop",
                    "note": (
                        'Click "Host Desktop" to view screens'
                        if vnc_available
                        else "VNC not started, screen only shown locally"
                    ),
                }
            )
        except Exception:
            ssh_manager.return_connection(ssh)
            raise

    except Exception as e:
        logger.error(f"Error showing device screens: {e}")
        return JSONResponse(
            content={"success": False, "error": str(e)}, status_code=500
        )
