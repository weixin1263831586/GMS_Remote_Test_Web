from __future__ import annotations

import asyncio
import logging
import shlex
from datetime import datetime

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from features.auth import (
    CurrentUser,
    require_authenticated_user,
    require_elevated_admin,
)
from foundation.errors import handle_api_errors
from foundation.responses import error_response, success_response
from foundation.security import sanitize_device_ids
from foundation.security_audit import security_audit_logger

from . import reconnect, runtime
from .locks import device_lock_manager
from .management_api import (
    _known_usbip_device_ids,
)
from .management_api import (
    router as management_router,
)
from .manager import device_manager
from .models import DeviceActionRequest, DeviceLockRequest, DeviceShellRequest, WifiConnectRequest
from .screens_api import router as screens_router
from .support import (
    SSHConnection,
    broadcast_device_lock_update,
    device_claim_conflict_response,
    device_mutation_guard,
)
from .usbip import wait_for_adb_serial_ready


logger = logging.getLogger(__name__)
router = APIRouter()
router.include_router(management_router)


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


@router.get("/api/devices/user-locked")
async def list_user_locks(request: Request):
    """List all user-locked devices."""
    current_user = require_authenticated_user(request)
    locks = device_lock_manager.get_all_locks()
    if current_user.role != "admin":
        client_id = current_user.id
        locks = {
            key: value
            for key, value in locks.items()
            if value.get("client_id") == client_id
        }
    return JSONResponse(
        content={"success": True, "data": locks}
    )


@router.post("/api/devices/force-release")
async def force_release_device_locks(
    req: DeviceLockRequest,
    request: Request,
    admin: CurrentUser = Depends(require_elevated_admin),
):
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
    security_audit_logger.log_event({
        "action_type": "api",
        "source": "web",
        "operation": "force_release_device_claim",
        "method": request.method,
        "path": request.url.path,
        "status_code": 200 if all(item["success"] for item in results) else 409,
        "client_id": admin.id,
        "username": admin.username,
        "devices": device_ids,
        "results": results,
    })
    return JSONResponse(
        content={
            "success": all(item["success"] for item in results),
            "results": results,
        }
    )


@router.post("/api/devices/reboot")
@handle_api_errors
@device_mutation_guard("reboot")
async def reboot_devices(req: DeviceActionRequest, request: Request):
    """Reboot devices."""
    devices = sanitize_device_ids(req.devices)
    if not devices:
        return error_response("No valid device serials", status_code=400)
    client_id = require_authenticated_user(request).id
    conflict = device_claim_conflict_response(devices, client_id, allow_owner=True)
    if conflict:
        return conflict
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
        *[reboot_single_device(d) for d in devices]
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
@device_mutation_guard("remount")
async def remount_devices(req: DeviceActionRequest, request: Request):
    """Remount devices."""
    client_id = require_authenticated_user(request).id
    devices = sanitize_device_ids(req.devices)
    if not devices:
        return error_response("No valid device serials", status_code=400)
    conflict = device_claim_conflict_response(devices, client_id, allow_owner=True)
    if conflict:
        return conflict

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

            # remount_device does blocking SSH — run it off the event loop. The
            # surrounding for-loop is serial, so the shared ssh connection is
            # never used by two threads at once.
            result = await asyncio.to_thread(device_manager.remount_device, device_id, ssh)
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

        # Serial execution (was a false-concurrency gather) — frees the loop
        # between devices and keeps the shared ssh connection single-threaded.
        results = []
        for device_id in devices:
            results.append(await remount_single_device(device_id))
        return _device_results(results, "Device Remount")


@router.post("/api/devices/wifi")
@device_mutation_guard("wifi")
async def connect_wifi(req: WifiConnectRequest, request: Request):
    """Connect to WiFi."""
    try:
        devices = sanitize_device_ids(req.devices)
        if not devices:
            return error_response("No valid device serials", status_code=400)
        client_id = require_authenticated_user(request).id
        conflict = device_claim_conflict_response(devices, client_id, allow_owner=True)
        if conflict:
            return conflict
        config = runtime.config_manager.load_config()
        wifi_defaults = runtime.config_manager.get_wifi_defaults(config)
        ssid = req.ssid or wifi_defaults["ssid"]
        password = req.password or wifi_defaults["password"]
        # Wi-Fi 参数转义后再传给 ADB Shell。
        ssid_q = shlex.quote(ssid)
        password_q = shlex.quote(password)
        async with runtime.ssh_manager.async_optional_connection(config) as ssh:
            if not ssh:
                return error_response("SSH connection failed", status_code=500)

            def _connect_one(device_id: str) -> dict:
                enable_cmd = f"adb -s {device_id} shell cmd wifi set-wifi-enabled enabled"
                connect_cmd = (
                    f"adb -s {device_id} shell cmd wifi connect-network {ssid_q} wpa2 {password_q}"
                )
                full_cmd = f"{enable_cmd} && sleep 2 && {connect_cmd}"
                _output, _error, code = runtime.ssh_manager.execute_command(ssh, full_cmd)
                return {"device": device_id, "success": code == 0}

            # Serial to_thread — frees the loop between devices and keeps the
            # shared ssh connection single-threaded (paramiko is not thread-safe).
            results = []
            for device_id in devices:
                results.append(await asyncio.to_thread(_connect_one, device_id))

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
        client_id = require_authenticated_user(request).id
        conflict = device_claim_conflict_response(
            [req.serial_no],
            client_id,
        )
        if conflict:
            return conflict
        config = runtime.config_manager.load_config()
        async with runtime.ssh_manager.async_optional_connection(config) as ssh:
            if not ssh:
                return JSONResponse(
                    content={"success": False, "message": "SSH connection failed"},
                    status_code=500,
                )

            ready_result = await asyncio.to_thread(
                wait_for_adb_serial_ready, ssh, req.serial_no, 30
            )

            if ready_result.get("ready"):
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
