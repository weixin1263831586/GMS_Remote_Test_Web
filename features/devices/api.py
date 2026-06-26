"""Devices router - device management APIs."""

import asyncio
import logging
import os
import re
import time
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from foundation.errors import handle_api_errors
from foundation.responses import error_response, success_response

from . import runtime
from . import reconnect
from .locks import device_lock_manager
from .manager import device_manager
from .models import (
    DeviceActionRequest,
    DeviceLockRequest,
    VerifiedBootState,
)
from .operations_api import (
    _build_devices_management_payload as _build_devices_management_payload,
)
from .operations_api import (
    _build_management_props_command as _build_management_props_command,
)
from .operations_api import (
    _parse_management_device_props as _parse_management_device_props,
)
from .operations_api import (
    connect_wifi as connect_wifi,
)
from .operations_api import (
    reboot_devices as reboot_devices,
)
from .operations_api import (
    router as operations_router,
)
from .screens_api import show_device_screens as show_device_screens
from .support import (
    SSHConnection,
    get_device_properties_optimized,
    get_or_create_user_state,
)


logger = logging.getLogger(__name__)

router = APIRouter()


def _help_or_continue(help: bool, method: str, path: str):
    if runtime.generate_help_or_continue is None:
        return None
    return runtime.generate_help_or_continue(help, method, path)


def _default_suites_path(config: dict[str, Any]) -> str:
    ubuntu_user = runtime.config_manager.get_ubuntu_user(config)
    return config.get("suites_path", f"/home/{ubuntu_user}/GMS-Suite")


def _api_success(data=None, message="操作成功"):
    return success_response(data=data, message=message)


def _api_error(message, status_code=500, **extra_fields):
    return error_response(message, status_code=status_code, **extra_fields)


def _device_results(results, operation_name):
    success_count = sum(result.get("success", False) for result in results)
    failed_count = len(results) - success_count
    return _api_success(
        {
            "results": results,
            "summary": {
                "total": len(results),
                "success": success_count,
                "failed": failed_count,
            },
        },
        f"{operation_name}完成: 成功 {success_count} 台, 失败 {failed_count} 台",
    )


def _known_usbip_device_ids() -> set:
    """Return USB/IP device ids from in-memory and persisted runtime state."""
    device_ids = set()
    with runtime.global_state.usbip_devices_source_lock:
        device_ids.update(runtime.global_state.usbip_devices_source.keys())
    runtime_sources = (runtime.config_manager.get_runtime_config() or {}).get("usbip_devices_source") or {}
    if isinstance(runtime_sources, dict):
        device_ids.update(str(device_id) for device_id in runtime_sources if device_id)
    return device_ids




@router.get("/api/devices/list")
@handle_api_errors
async def get_connected_devices(
    request: Request,
    help: bool = Query(False),
    force_refresh: bool = Query(False),
):
    """Get all connected device list (same as adb devices)."""
    resp = _help_or_continue(help, "GET", "/api/devices/list")
    if resp:
        return resp

    # Track user access
    client_id = runtime.get_client_id_from_request(request)
    get_or_create_user_state(client_id)

    # Refresh device list first
    raw_devices = await asyncio.to_thread(device_manager.get_connected_devices, force_refresh)
    reconnect.reconcile_observed_usbip_devices(raw_devices)
    devices = reconnect.filter_suppressed_usbip_devices(raw_devices)

    # Keep USB/IP source records for disconnected devices. They are needed for
    # server-side auto reconnect after device reboot; manual USB/IP disconnect
    # is responsible for clearing them.
    current_device_set = set(devices)

    # Check cache
    now = datetime.now().timestamp()
    if not force_refresh and now - runtime.global_state.device_cache["timestamp"] < runtime.device_cache_ttl:
        cached_devices = runtime.global_state.device_cache["devices"]
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
        client_ip = runtime.get_client_ip(request)
        client_id_from_ip = runtime.client_manager.get_client_id(client_ip)
        lock_status = device_lock_manager.get_lock_status(device_id)

        if lock_status:
            device_info["locked"] = True
            device_info["locked_by"] = lock_status["locked_by"]
            device_info["locked_username"] = lock_status.get("username", "")
            device_info["locked_client_id"] = lock_status.get("client_id", "")
            device_info["locked_by_self"] = lock_status.get("client_id") == client_id_from_ip
            device_info["locked_at"] = lock_status["locked_at"]
        else:
            device_info["locked_by"] = ""
            device_info["locked_username"] = ""
            device_info["locked_client_id"] = ""
            device_info["locked_by_self"] = False

        # Check USB/IP source
        if device_id in runtime.global_state.usbip_devices_source:
            source = runtime.global_state.usbip_devices_source[device_id]
            device_info["source"] = source["source"]
            device_info["is_usbip"] = True

        devices_with_status.append(device_info)

    # Update cache
    with runtime.global_state.device_cache_lock:
        runtime.global_state.device_cache = {"devices": devices_with_status, "timestamp": now}

    return JSONResponse(content=devices_with_status)


async def _manage_bootloader_lock(devices: list[str], action: str) -> JSONResponse:
    """Common bootloader lock/unlock handler."""
    try:
        if not devices:
            return _api_error("No devices selected", status_code=400)

        valid_device_pattern = re.compile(r"^[a-zA-Z0-9.:-]+$")
        for device_id in devices:
            if not valid_device_pattern.match(device_id):
                return _api_error(
                    f"Invalid device ID format: {device_id}", status_code=400
                )

        config = runtime.config_manager.load_config()

        with runtime.ssh_manager.connection(config) as ssh:
            results = []

            local_script = os.path.join(
                runtime.project_root, "scripts", "run_Device_Lock.sh"
            )
            remote_script = os.path.join(_default_suites_path(config), "run_Device_Lock.sh")

            if not os.path.exists(local_script):
                return _api_error(
                    f"Script file not found: {local_script}", status_code=404
                )

            try:
                with ssh.open_sftp() as sftp:
                    sftp.put(local_script, remote_script)
                runtime.ssh_manager.execute_command(ssh, f"chmod +x '{remote_script}'")
            except Exception as e:
                return _api_error(
                    f"Script upload failed: {e!s}", status_code=500
                )

            for device_id in devices:
                try:
                    cmd = f"bash '{remote_script}' '{device_id}' '{action}'"
                    output, error, code = runtime.ssh_manager.execute_command(ssh, cmd)

                    if code == 0:
                        start_time = time.time()
                        while time.time() - start_time < 60:
                            check_cmd = f"adb -s {device_id} get-state"
                            check_output, _, _check_code = runtime.ssh_manager.execute_command(
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

            action_text = "unlock" if action == "unlock" else "lock"
            return _api_success(response_data, f"Device {action_text} operation completed")

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error managing device lock: {e}")
        return _api_error(str(e), status_code=500)


def _resolve_device_lock_devices(req: DeviceLockRequest) -> list[str]:
    """Extract device list from a lock/unlock request."""
    if req.device_id:
        return [req.device_id]
    return req.devices or []


@router.post("/api/devices/bootloader-lock")
async def lock_bootloader(
    help: bool = Query(False),
    req: DeviceLockRequest = Body(None),
):
    """Lock device Bootloader."""
    resp = _help_or_continue(help, "POST", "/api/devices/bootloader-lock")
    if resp:
        return resp
    return await _manage_bootloader_lock(_resolve_device_lock_devices(req), "lock")


@router.post("/api/devices/bootloader-unlock")
async def unlock_bootloader(
    help: bool = Query(False),
    req: DeviceLockRequest = Body(None),
):
    """Unlock device Bootloader."""
    resp = _help_or_continue(help, "POST", "/api/devices/bootloader-unlock")
    if resp:
        return resp
    return await _manage_bootloader_lock(_resolve_device_lock_devices(req), "unlock")


@router.post("/api/devices/bootloader-status")
async def check_bootloader_status(req: DeviceActionRequest):
    """Check device Bootloader lock status (GREEN=locked, ORANGE=unlocked)."""
    try:
        with SSHConnection() as ssh:
            async def check_single_device(device_id: str) -> dict:
                output, _error, _code = runtime.ssh_manager.execute_command(
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

            return _api_success({"results": results}, "Lock status check completed")

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error checking lock status: {e}")
        return _api_error(str(e), status_code=500)


@router.post("/api/devices/info")
async def get_device_info(req: DeviceActionRequest):
    """Get device detailed information."""
    try:
        with SSHConnection() as ssh:
            async def get_single_device_info(device_id: str) -> dict:
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

            return _api_success({"results": results}, "Device info retrieved")

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting device info: {e}")
        return _api_error(str(e), status_code=500)


router.include_router(operations_router)
