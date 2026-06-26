from __future__ import annotations

import asyncio
import logging
import shlex
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from foundation.errors import handle_api_errors
from foundation.networking import is_local_host
from foundation.responses import error_response, success_response

from . import reconnect, runtime
from .locks import device_lock_manager
from .manager import device_manager
from .models import DeviceActionRequest, DeviceLockRequest, DeviceShellRequest, WifiConnectRequest
from .screens_api import router as screens_router
from .support import SSHConnection, broadcast_device_lock_update
from .usbip import wait_for_adb_serial_ready
from .utils import DeviceUtils


logger = logging.getLogger(__name__)
router = APIRouter()


def _known_usbip_device_ids() -> set:
    device_ids = set()
    with runtime.global_state.usbip_devices_source_lock:
        device_ids.update(runtime.global_state.usbip_devices_source.keys())
    runtime_sources = (runtime.config_manager.get_runtime_config() or {}).get(
        "usbip_devices_source"
    ) or {}
    if isinstance(runtime_sources, dict):
        device_ids.update(str(device_id) for device_id in runtime_sources if device_id)
    return device_ids


def _device_results(results, operation_name):
    success_count = sum(result.get("success", False) for result in results)
    failed_count = len(results) - success_count
    return success_response(
        data={
            "results": results,
            "summary": {
                "total": len(results),
                "success": success_count,
                "failed": failed_count,
            },
        },
        message=f"{operation_name}完成: 成功 {success_count} 台, 失败 {failed_count} 台",
    )


def _build_management_props_command(device_ids: list[str]) -> str:
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


def _parse_management_device_props(props_output: str) -> dict[str, dict[str, str]]:
    device_data: dict[str, dict[str, str]] = {}
    current_device = None

    prop_keys = ("serial_no", "model", "android_version", "battery_level", "soc_model")

    for line in props_output.split("\n"):
        line = line.strip()
        if line.startswith("===DEVICE:"):
            current_device = line.split("===DEVICE:")[1].split("===")[0]
            device_data[current_device] = dict.fromkeys(prop_keys, "")
        elif current_device and line:
            props = device_data[current_device]
            for key in prop_keys:
                if not props[key]:
                    props[key] = line
                    break

    return device_data


def _build_devices_management_payload(
    device_ids: list[str],
    device_data: dict[str, dict[str, str]],
    config: dict[str, Any],
) -> dict[str, Any]:
    from features.devices.usbip import usbip_manager

    client_id = runtime.client_manager.get_client_id("127.0.0.1")
    locks = device_lock_manager.get_all_locks()
    devices_info = []
    ubuntu_host = runtime.config_manager.get_ubuntu_host(config)
    ubuntu_user = runtime.config_manager.get_ubuntu_user(config)

    all_usbip_sources = {**runtime.global_state.usbip_devices_source, **usbip_manager.device_sources}
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
                "locked_by": lock_info.get("username", "") if device_id in locks else "",
                "locked_username": lock_info.get("username", "") if device_id in locks else "",
                "locked_client_id": lock_info.get("client_id", "") if device_id in locks else "",
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
    try:
        config = runtime.config_manager.load_config()

        if is_local_host(runtime.config_manager.get_ubuntu_host(config)):
            output, error, code = await asyncio.to_thread(
                runtime.run_local_shell_command, "adb devices", 5
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
                runtime.run_local_shell_command, props_cmd, 15
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
            output, _, _ = runtime.ssh_manager.execute_command(ssh, "adb devices", timeout=5)
            device_ids = DeviceUtils.parse_adb_devices(output)

            if not device_ids:
                return JSONResponse(content={"devices": []})

            props_cmd = _build_management_props_command(device_ids)
            props_output, _, _ = runtime.ssh_manager.execute_command(ssh, props_cmd, timeout=15)

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


@router.post("/api/devices/force-release")
async def force_release_device_locks(req: DeviceLockRequest):
    """Force release platform device occupation locks."""
    device_ids = []
    if req.device_id:
        device_ids.append(req.device_id)
    if req.devices:
        device_ids.extend(req.devices)
    device_ids = list(dict.fromkeys(
        str(device_id or "").strip()
        for device_id in device_ids
        if str(device_id or "").strip()
    ))

    if not device_ids:
        return JSONResponse(
            content={"success": False, "error": "No devices selected"},
            status_code=400,
        )

    results = []
    for device_id in device_ids:
        success, message = device_lock_manager.force_unlock_device(device_id)
        results.append(
            {
                "device_id": device_id,
                "success": success,
                "message": message,
            }
        )

    await broadcast_device_lock_update(device_ids)
    return JSONResponse(
        content={
            "success": all(item["success"] for item in results),
            "results": results,
        }
    )


@router.post("/api/devices/reboot")
@handle_api_errors
async def reboot_devices(req: DeviceActionRequest):
    """Reboot devices."""
    usbip_device_ids = _known_usbip_device_ids()
    usbip_reconnect_hosts: dict[str, list[str]] = {}
    runtime_sources = (runtime.config_manager.get_runtime_config() or {}).get(
        "usbip_devices_source"
    ) or {}
    with runtime.global_state.usbip_devices_source_lock:
        memory_sources = dict(runtime.global_state.usbip_devices_source)

    async def reboot_single_device(device_id: str) -> dict:
        wait_for_online = device_id not in usbip_device_ids
        if not wait_for_online:
            source_info = memory_sources.get(device_id) or (
                runtime_sources.get(device_id) if isinstance(runtime_sources, dict) else {}
            ) or {}
            device_host = str(source_info.get("source") or "").strip()
            if device_host:
                usbip_reconnect_hosts.setdefault(device_host, []).append(device_id)
        result = await asyncio.to_thread(
            device_manager.reboot_device,
            device_id,
            None,
            wait_for_online,
        )
        result["device"] = device_id
        result["usbip_reconnect_expected"] = not wait_for_online
        return result

    results = await asyncio.gather(
        *[reboot_single_device(d) for d in req.devices]
    )
    if usbip_reconnect_hosts:
        for device_host, device_ids in usbip_reconnect_hosts.items():
            reconnect.schedule_usbip_reconnect(
                device_host,
                reason="USB/IP device reboot requested",
                expected_devices=device_ids,
            )
    return _device_results(results, "Device reboot")


@router.post("/api/devices/remount")
@handle_api_errors
async def remount_devices(req: DeviceActionRequest, request: Request):
    """Remount devices."""
    client_id = runtime.get_client_id_from_request(request)

    with SSHConnection() as ssh:
        async def remount_single_device(device_id: str) -> dict:
            await runtime.safe_websocket_send(
                client_id,
                {
                    "type": "log_update",
                    "log": f"[{device_id}] adb root && adb remount",
                    "log_type": "info",
                },
            )

            result = device_manager.remount_device(device_id, ssh)
            await runtime.safe_websocket_send(
                client_id,
                {
                    "type": "log_update",
                    "log": f"[{device_id}] remount: {(result.get('output') or '').strip()}",
                    "log_type": "info" if result.get("success") else "error",
                },
            )
            result["device"] = device_id
            return result

        results = await asyncio.gather(
            *[remount_single_device(d) for d in req.devices]
        )
        return _device_results(results, "Device Remount")


@router.post("/api/devices/wifi")
async def connect_wifi(req: WifiConnectRequest):
    """Connect to WiFi."""
    try:
        config = runtime.config_manager.load_config()
        with runtime.ssh_manager.optional_connection(config) as ssh:
            if not ssh:
                return error_response("SSH connection failed", status_code=500)

            results = []
            for device_id in req.devices:
                enable_cmd = f"adb -s {device_id} shell cmd wifi set-wifi-enabled enabled"
                connect_cmd = (
                    f'adb -s {device_id} shell cmd wifi connect-network "{req.ssid}" wpa2 "{req.password}"'
                )
                full_cmd = f"{enable_cmd} && sleep 2 && {connect_cmd}"

                _output, _error, code = runtime.ssh_manager.execute_command(ssh, full_cmd)
                results.append({"device": device_id, "success": code == 0})

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
        return error_response(f"{e!s}. Please check configuration and parameters.", status_code=500)


@router.post("/api/devices/shell")
async def open_device_shell(req: DeviceShellRequest, request: Request):
    """Open device ADB Shell - prepare device connection for terminal page."""
    try:
        config = runtime.config_manager.load_config()
        with runtime.ssh_manager.optional_connection(config) as ssh:
            if not ssh:
                return JSONResponse(
                    content={"success": False, "message": "SSH connection failed"},
                    status_code=500,
                )

            ready_result = await asyncio.to_thread(
                wait_for_adb_serial_ready, ssh, req.serial_no, 30
            )

            if ready_result.get("ready"):
                client_id = runtime.get_client_id_from_request(request)

                if not hasattr(runtime.global_state, "device_shells"):
                    runtime.global_state.device_shells = {}

                runtime.global_state.device_shells[client_id] = {
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
            content={"success": False, "message": f"Failed to open shell: {e!s}"},
            status_code=500,
        )


router.include_router(screens_router)
