from __future__ import annotations

import asyncio
import logging
import re
import shlex
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from foundation.errors import handle_api_errors
from foundation.networking import is_local_host
from foundation.responses import error_response, success_response
from foundation.security import sanitize_device_ids

from . import reconnect, runtime
from .locks import device_lock_manager
from .manager import device_manager, has_blocked_adb_process
from .models import DeviceActionRequest, DeviceLockRequest, DeviceShellRequest, WifiConnectRequest
from .screens_api import router as screens_router
from .support import SSHConnection, broadcast_device_lock_update
from .usbip import wait_for_adb_serial_ready
from .utils import DeviceUtils


logger = logging.getLogger(__name__)
router = APIRouter()


def _known_usbip_device_ids() -> set:
    return set(_known_usbip_sources().keys())


def _known_usbip_sources() -> dict[str, dict[str, Any]]:
    """Return USB/IP source records from persisted, in-memory, and manager state.

    Later sources override earlier ones so a current attach result wins over
    older persisted runtime data, while persisted data still survives restarts.
    """
    from features.devices.usbip import usbip_manager

    sources: dict[str, dict[str, Any]] = {}

    runtime_sources = (runtime.config_manager.get_runtime_config() or {}).get(
        "usbip_devices_source"
    ) or {}
    if isinstance(runtime_sources, dict):
        for device_id, source in runtime_sources.items():
            if device_id and isinstance(source, dict):
                sources[str(device_id)] = dict(source)

    with runtime.global_state.usbip_devices_source_lock:
        for device_id, source in runtime.global_state.usbip_devices_source.items():
            if device_id and isinstance(source, dict):
                sources[str(device_id)] = dict(source)

    for device_id, source in (getattr(usbip_manager, "device_sources", {}) or {}).items():
        if device_id and isinstance(source, dict):
            sources[str(device_id)] = dict(source)

    return sources


def _source_host_token(source: str) -> str:
    source = str(source or "").strip()
    if "@" in source:
        return source.rsplit("@", 1)[1].strip()
    return source


def _active_usbip_source_hosts(config: dict[str, Any]) -> set[str] | None:
    """Return remote hosts currently attached through usbip on the test host."""
    command = "usbip port"
    try:
        if is_local_host(runtime.config_manager.get_ubuntu_host(config)):
            output, _error, code = runtime.run_local_shell_command(command, 10)
        else:
            with SSHConnection(config) as ssh:
                output, _error, code = runtime.ssh_manager.execute_command(
                    ssh, command, timeout=10
                )
        if code != 0:
            return None
    except Exception as exc:
        logger.info("[USB/IP] Failed to query active usbip ports: %s", exc)
        return None

    hosts: set[str] = set()
    for line in (output or "").splitlines():
        match = re.search(r"\b(?:Remote|remote)\s+host\s*[:=]\s*([^\s]+)", line)
        if match:
            hosts.add(match.group(1).strip())
        hosts.update(re.findall(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", line))
    return hosts


def _run_on_test_host(config: dict[str, Any], command: str, timeout: int = 10):
    if is_local_host(runtime.config_manager.get_ubuntu_host(config)):
        return runtime.run_local_shell_command(command, timeout)
    with SSHConnection(config) as ssh:
        return runtime.ssh_manager.execute_command(ssh, command, timeout=timeout)


def _active_usbip_serials(config: dict[str, Any]) -> set[str] | None:
    """Return ADB serials currently backed by Linux USB/IP vhci devices."""
    command = (
        "for f in $(find /sys/devices/platform/vhci_hcd* -name serial -type f 2>/dev/null); do "
        "[ -f \"$f\" ] || continue; "
        "s=$(cat \"$f\" 2>/dev/null); "
        "[ -n \"$s\" ] && [ \"$s\" != \"vhci_hcd.0\" ] && echo \"$s\"; "
        "done | sort -u"
    )
    try:
        output, _error, code = _run_on_test_host(config, command, timeout=10)
        if code != 0:
            return None
    except Exception as exc:
        logger.info("[USB/IP] Failed to query active usbip serials: %s", exc)
        return None
    return {line.strip() for line in (output or "").splitlines() if line.strip()}


def _clear_usbip_source_record(device_id: str, sources: dict[str, dict[str, Any]]) -> None:
    from features.devices.usbip import usbip_manager

    with runtime.global_state.usbip_devices_source_lock:
        runtime.global_state.usbip_devices_source.pop(device_id, None)

    getattr(usbip_manager, "device_sources", {}).pop(device_id, None)
    sources.pop(device_id, None)

    try:
        runtime_config = runtime.config_manager.get_runtime_config()
        runtime_sources = runtime_config.get("usbip_devices_source", {})
        if isinstance(runtime_sources, dict) and device_id in runtime_sources:
            runtime_sources.pop(device_id, None)
            runtime_config["usbip_devices_source"] = runtime_sources
            runtime.config_manager.save_runtime_config(runtime_config)
    except Exception as exc:
        logger.warning("[USB/IP] Failed to clear stale source for %s: %s", device_id, exc)


def _prune_inactive_usbip_sources(
    device_ids: list[str],
    sources: dict[str, dict[str, Any]],
    config: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """Drop stale USB/IP records for devices now visible without an active port."""
    current_usbip_devices = [device_id for device_id in device_ids if device_id in sources]
    if not current_usbip_devices:
        return sources

    active_serials = _active_usbip_serials(config)
    active_hosts = None if active_serials is not None else _active_usbip_source_hosts(config)
    if active_serials is None and active_hosts is None:
        return sources

    for device_id in current_usbip_devices:
        stale = False
        if active_serials is not None:
            stale = device_id not in active_serials
        else:
            source_host = _source_host_token(sources.get(device_id, {}).get("source", ""))
            stale = bool(source_host and source_host not in active_hosts)

        if stale:
            logger.info(
                "[USB/IP] Clearing stale source for %s; no active USB/IP transport",
                device_id,
            )
            _clear_usbip_source_record(device_id, sources)
    return sources


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
    client_id: str | None = None,
) -> dict[str, Any]:
    client_id = client_id or runtime.client_manager.get_client_id("127.0.0.1")
    locks = device_lock_manager.get_all_locks()
    devices_info = []
    ubuntu_host = runtime.config_manager.get_ubuntu_host(config)
    ubuntu_user = runtime.config_manager.get_ubuntu_user(config)

    all_usbip_sources = _prune_inactive_usbip_sources(
        device_ids,
        _known_usbip_sources(),
        config,
    )
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


def _cached_management_payload() -> dict[str, Any] | None:
    with runtime.global_state.device_cache_lock:
        cached_devices = runtime.global_state.device_cache.get("devices") or []
    if not cached_devices:
        return None

    devices_info = []
    for device in cached_devices:
        if not isinstance(device, dict):
            continue
        device_id = device.get("device_id") or device.get("serial_no") or device.get("serial")
        if not device_id:
            continue
        devices_info.append(
            {
                "device_id": device_id,
                "serial_no": device_id,
                "model": device.get("model") or "",
                "android_version": device.get("android_version") or "",
                "battery_level": device.get("battery_level") or "",
                "soc_model": device.get("soc_model") or "",
                "source_type": "usbip" if device.get("is_usbip") else "local",
                "source_host": device.get("source") or device.get("source_host") or "-",
                "status": device.get("status") or "online",
                "locked_by": device.get("locked_by") or "",
                "locked_username": device.get("locked_username") or "",
                "locked_client_id": device.get("locked_client_id") or "",
                "locked_by_self": bool(device.get("locked_by_self")),
            }
        )
    if not devices_info:
        return None
    return {
        "devices": devices_info,
        "success": True,
        "source": "cache",
        "warning": "ADB scan returned no devices; using cached device list",
    }


@router.get("/api/devices/management")
async def devices_management(request: Request):
    """Device management page - get detailed management info for all devices."""
    try:
        config = runtime.config_manager.load_config()

        if is_local_host(runtime.config_manager.get_ubuntu_host(config)):
            if has_blocked_adb_process():
                cached_payload = _cached_management_payload()
                if cached_payload:
                    return JSONResponse(content=cached_payload)
                return JSONResponse(
                    content={
                        "devices": [],
                        "success": True,
                        "source": "local",
                        "warning": "Local adb server is blocked; skipped adb scan",
                    }
                )
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
                cached_payload = _cached_management_payload()
                if cached_payload:
                    return JSONResponse(content=cached_payload)
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
                device_ids,
                _parse_management_device_props(props_output),
                config,
                runtime.get_client_id_from_request(request),
            )
            payload.update({"success": True, "source": "local"})
            return JSONResponse(content=payload)

        with SSHConnection(config) as ssh:
            output, _, _ = runtime.ssh_manager.execute_command(ssh, "adb devices", timeout=5)
            device_ids = DeviceUtils.parse_adb_devices(output)

            if not device_ids:
                cached_payload = _cached_management_payload()
                if cached_payload:
                    return JSONResponse(content=cached_payload)
                return JSONResponse(content={"devices": []})

            props_cmd = _build_management_props_command(device_ids)
            props_output, _, _ = runtime.ssh_manager.execute_command(ssh, props_cmd, timeout=15)

            return JSONResponse(
                content=_build_devices_management_payload(
                    device_ids,
                    _parse_management_device_props(props_output),
                    config,
                    runtime.get_client_id_from_request(request),
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
    devices = sanitize_device_ids(req.devices)
    if not devices:
        return error_response("No valid device serials", status_code=400)

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
async def connect_wifi(req: WifiConnectRequest):
    """Connect to WiFi."""
    try:
        devices = sanitize_device_ids(req.devices)
        if not devices:
            return error_response("No valid device serials", status_code=400)
        config = runtime.config_manager.load_config()
        wifi_defaults = runtime.config_manager.get_wifi_defaults(config)
        ssid = req.ssid or wifi_defaults["ssid"]
        password = req.password or wifi_defaults["password"]
        # Quote ssid/password so special characters can't break out of the adb
        # shell argument (they were previously interpolated raw between quotes).
        ssid_q = shlex.quote(ssid)
        password_q = shlex.quote(password)
        with runtime.ssh_manager.optional_connection(config) as ssh:
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
