"""Devices router - device management APIs."""

import asyncio
import logging
import os
import re
import shlex
import time
from datetime import datetime

from fastapi import APIRouter, Body, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from features.test_execution import get_default_suites_path
from foundation.errors import handle_api_errors
from foundation.responses import error_response, success_response
from foundation.security import sanitize_device_ids

from . import reconnect, runtime
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
    _known_usbip_sources as _known_usbip_sources,
)
from .operations_api import (
    _parse_management_device_props as _parse_management_device_props,
)
from .operations_api import (
    _prune_inactive_usbip_sources as _prune_inactive_usbip_sources,
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

    now = datetime.now().timestamp()
    if not force_refresh:
        with runtime.global_state.device_cache_lock:
            cached_devices = runtime.global_state.device_cache.get("devices") or []
            cache_timestamp = runtime.global_state.device_cache.get("timestamp", 0)
        if cached_devices and now - cache_timestamp < runtime.device_cache_ttl:
            return JSONResponse(content=cached_devices)

    # Refresh device list first
    raw_devices = await asyncio.to_thread(device_manager.get_connected_devices, force_refresh)
    if not raw_devices:
        with runtime.global_state.device_cache_lock:
            cached_devices = runtime.global_state.device_cache.get("devices") or []
        if cached_devices:
            logger.warning("[Device] ADB scan returned no devices; keeping cached device list")
            return JSONResponse(content=cached_devices)

    reconnect.reconcile_observed_usbip_devices(raw_devices)
    devices = reconnect.filter_suppressed_usbip_devices(raw_devices)

    # Keep USB/IP source records for disconnected devices. They are needed for
    # server-side auto reconnect after device reboot; manual USB/IP disconnect
    # is responsible for clearing them.
    current_device_set = set(devices)

    # Check cache
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

    # 设备 -> 所属分组 id 列表（按当前用户读取其 per-user 分组；局部 import 规避循环依赖）
    from features.users import (
        build_device_group_map,
        current_username_for_request,
        load_device_groups,
    )
    group_map = build_device_group_map(
        load_device_groups(current_username_for_request(request))
    )

    usbip_sources = _prune_inactive_usbip_sources(
        devices,
        _known_usbip_sources(),
        runtime.config_manager.load_config()
        if hasattr(runtime.config_manager, "load_config")
        else {},
    )

    for device_id in devices:
        device_info = {"device_id": device_id, "status": "online", "locked": False}

        # 所属分组（仅内存查表，不增加网络往返）
        device_info["groups"] = group_map.get(device_id, [])

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
        if device_id in usbip_sources:
            source = usbip_sources[device_id]
            device_info["source"] = source.get("source", "")
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
            remote_script = os.path.join(get_default_suites_path(config), "run_Device_Lock.sh")

            if not os.path.exists(local_script):
                return _api_error(
                    f"Script file not found: {local_script}", status_code=404
                )

            try:
                with ssh.open_sftp() as sftp:
                    sftp.put(local_script, remote_script)
                runtime.ssh_manager.execute_command(ssh, f"chmod +x {shlex.quote(remote_script)}")
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
        devices = sanitize_device_ids(req.devices)
        if not devices:
            return _api_error("No valid device serials", status_code=400)
        with SSHConnection() as ssh:
            # Synchronous per-device SSH check — run serially off the event loop
            # (see get_device_info for why gather was false concurrency here).
            def check_single_device(device_id: str) -> dict:
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

            results = []
            for device_id in devices:
                results.append(await asyncio.to_thread(check_single_device, device_id))

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
        devices = sanitize_device_ids(req.devices)
        if not devices:
            return _api_error("No valid device serials", status_code=400)
        with SSHConnection() as ssh:
            # Per-device work is fully synchronous (get_device_info +
            # get_device_properties_optimized both do blocking SSH). The old
            # asyncio.gather looked concurrent but had no await suspension
            # point inside, so it ran serially while blocking the event loop.
            # Run each device off the loop in turn — same serial behaviour,
            # but the loop is freed between devices and the shared ssh
            # connection is never used by two threads at once (paramiko is not
            # thread-safe).
            def get_single_device_info(device_id: str) -> dict:
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

                extra_props = get_device_properties_optimized(device_id, ssh)

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

            results = []
            for device_id in devices:
                results.append(await asyncio.to_thread(get_single_device_info, device_id))

            return _api_success({"results": results}, "Device info retrieved")

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting device info: {e}")
        return _api_error(str(e), status_code=500)


router.include_router(operations_router)


# ==================== 设备分组：按属性自动分组 ====================


# 自动分组可用的属性维度 -> get_device_info 返回的 base_info 键
_AUTO_GROUP_KEYS = {
    "model": "model",
    "android_version": "android_version",
    "soc": "soc_model",
}


@router.post("/api/device-groups/auto")
@handle_api_errors
async def auto_group_devices(request: Request, req: dict = Body(default={})):
    """按设备属性（model / android_version / soc）一键生成分组定义（per-user）。

    结果写回当前用户的 device_groups（变成可再手动微调的持久分组），并返回新分组列表。
    每个分组名形如 "model: Pixel 7"，id 形如 "auto_<dim>_<sanitized>"。
    """
    from features.users import (
        current_username_for_request,
        enforce_exclusive_device_group,
        load_device_groups,
        normalize_device_groups,
        save_device_groups,
        soc_series,
    )

    dim = (req.get("by") or "").strip()
    info_key = _AUTO_GROUP_KEYS.get(dim)
    if not info_key:
        return _api_error(
            "by 必须是 model/android_version/soc", status_code=400
        )

    # 当前在线设备（用缓存即可，避免重复 SSH 扫描）
    raw_devices = await asyncio.to_thread(device_manager.get_connected_devices)
    if not raw_devices:
        return _api_success({"groups": []}, "当前无在线设备")

    # 收集每台设备的属性值
    value_to_devices: dict[str, list[str]] = {}
    with SSHConnection() as ssh:
        for device_id in raw_devices:
            base_info = await asyncio.to_thread(
                device_manager.get_device_info, device_id, ssh
            )
            value = str(base_info.get(info_key) or "未知").strip() or "未知"
            # SOC 维度做系列归并：RK3576S -> RK3576，RK3588S -> RK3588，
            # 同数字系列的设备归到同一个分组；其他维度保持原值。
            if dim == "soc":
                value = soc_series(value)
            value_to_devices.setdefault(value, []).append(device_id)

    def _sanitize(value: str) -> str:
        return re.sub(r"[^a-zA-Z0-9_-]+", "_", value).strip("_") or "unknown"

    username = current_username_for_request(request)
    existing_groups = load_device_groups(username)
    # 去掉所有旧的自动分组（不限维度），保留用户手建分组
    kept = [
        g for g in existing_groups
        if not str(g.get("id", "")).startswith("auto_")
    ]

    new_groups = list(kept)
    for value, devs in value_to_devices.items():
        gid = f"auto_{dim}_{_sanitize(value)}"
        # 若与手建分组 id 冲突则跳过该值，避免覆盖用户数据
        if any(g["id"] == gid for g in new_groups):
            gid = f"{gid}_{devs[0][:4]}"
        new_groups.append({
            "id": gid,
            "name": f"{dim}: {value}",
            "device_ids": devs,
        })

    # 规整一次（补 color、去重 device_ids、followed 默认 False）
    new_groups = normalize_device_groups(new_groups)
    # 互斥语义：自动分出的组之间互斥（同一设备只进一个 auto 组），也从手建组抢回设备
    for g in new_groups:
        if g["id"].startswith(f"auto_{dim}_"):
            enforce_exclusive_device_group(new_groups, g["id"], g["device_ids"])
    save_device_groups(username, new_groups)

    return _api_success({"groups": new_groups}, f"已按 {dim} 生成 {len(value_to_devices)} 个分组")
