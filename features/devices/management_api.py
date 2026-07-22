from __future__ import annotations

import asyncio
import logging
import re
import shlex
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from features.auth import CurrentUser, require_authenticated_user_when_auth_required
from foundation.networking import is_local_host

from . import runtime
from .locks import device_lock_manager
from .manager import has_blocked_adb_process
from .support import SSHConnection
from .utils import DeviceUtils


logger = logging.getLogger(__name__)
router = APIRouter()


def _known_usbip_device_ids() -> set:
    return set(_known_usbip_sources().keys())


def _known_usbip_sources() -> dict[str, dict[str, Any]]:
    """Return persisted, in-memory, and currently attached USB/IP sources."""
    from .usbip import usbip_manager

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
    return source.rsplit("@", 1)[1].strip() if "@" in source else source


def _active_usbip_source_hosts(config: dict[str, Any]) -> set[str] | None:
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


def _clear_usbip_source_record(
    device_id: str,
    sources: dict[str, dict[str, Any]],
) -> None:
    from .usbip import usbip_manager

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
    """Drop stale source records when no matching USB/IP transport exists."""
    current_usbip_devices = [item for item in device_ids if item in sources]
    if not current_usbip_devices:
        return sources

    active_serials = _active_usbip_serials(config)
    active_hosts = None if active_serials is not None else _active_usbip_source_hosts(config)
    if active_serials is None and active_hosts is None:
        return sources

    for device_id in current_usbip_devices:
        if active_serials is not None:
            stale = device_id not in active_serials
        else:
            source_host = _source_host_token(sources.get(device_id, {}).get("source", ""))
            stale = bool(source_host and source_host not in active_hosts)
        if stale:
            logger.info("[USB/IP] Clearing stale source for %s", device_id)
            _clear_usbip_source_record(device_id, sources)
    return sources


def _build_management_props_command(device_ids: list[str]) -> str:
    commands = []
    for device_id in device_ids:
        device_shell = (
            f'echo "===DEVICE:{device_id}===" && '
            "getprop ro.serialno && getprop ro.product.model && "
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
    username: str | None = None,
) -> dict[str, Any]:
    client_id = client_id or runtime.client_manager.get_client_id("127.0.0.1")
    locks = device_lock_manager.get_all_locks()
    ubuntu_host = runtime.config_manager.get_ubuntu_host(config)
    ubuntu_user = runtime.config_manager.get_ubuntu_user(config)

    from features.users import auto_assign_new_devices, build_device_group_map

    all_sources = _prune_inactive_usbip_sources(
        device_ids, _known_usbip_sources(), config
    )
    for device_id in device_ids:
        source = all_sources.get(device_id)
        device_data.setdefault(device_id, {})["source_host"] = (
            source.get("source", "Unknown")
            if source
            else f"{ubuntu_user}@{ubuntu_host}"
        )
    group_map = build_device_group_map(auto_assign_new_devices(username, device_data))

    devices_info = []
    for device_id in device_ids:
        props = device_data.get(device_id, {})
        lock_info = locks.get(device_id, {})
        source = all_sources.get(device_id)
        devices_info.append(
            {
                "device_id": device_id,
                "serial_no": props.get("serial_no", device_id),
                "model": props.get("model", ""),
                "android_version": props.get("android_version", ""),
                "battery_level": props.get("battery_level", ""),
                "soc_model": props.get("soc_model", ""),
                "source_type": "usbip" if source else "local",
                "source_host": source.get("source", "Unknown") if source else f"{ubuntu_user}@{ubuntu_host}",
                "status": "online",
                "locked_by": lock_info.get("username", ""),
                "locked_username": lock_info.get("username", ""),
                "locked_client_id": lock_info.get("client_id", ""),
                "locked_by_self": lock_info.get("client_id") == client_id,
                "lease_id": lock_info.get("lease_id", ""),
                "lease_generation": lock_info.get("generation", 0),
                "groups": group_map.get(device_id, []),
            }
        )
    return {"devices": devices_info}


def _cached_management_payload(client_id: str) -> dict[str, Any] | None:
    with runtime.global_state.device_cache_lock:
        cached_devices = runtime.global_state.device_cache.get("devices") or []
    devices_info = []
    for device in cached_devices:
        if not isinstance(device, dict):
            continue
        device_id = device.get("device_id") or device.get("serial_no") or device.get("serial")
        if not device_id:
            continue
        lock = device_lock_manager.get_lock_status(device_id) or {}
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
                "locked_by": lock.get("locked_by") or "",
                "locked_username": lock.get("username") or "",
                "locked_client_id": lock.get("client_id") or "",
                "locked_by_self": lock.get("client_id") == client_id,
                "lease_id": lock.get("lease_id", ""),
                "lease_generation": lock.get("generation", 0),
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


def _mgmt_username(request: Request) -> str | None:
    try:
        from features.auth import get_authenticated_user

        user = get_authenticated_user(request)
    except Exception:
        return None
    return getattr(user, "username", None) if user else None


def _sanitize_management_payload(
    payload: dict[str, Any],
    *,
    principal: CurrentUser | None,
) -> dict[str, Any]:
    """Hide another owner's identity and lease capabilities from operators."""

    sanitized = dict(payload)
    devices: list[dict[str, Any]] = []
    for raw in payload.get("devices") or []:
        item = dict(raw)
        owned_by_principal = bool(item.get("locked_by_self"))
        if (principal is None or principal.role != "admin") and not owned_by_principal:
            occupied = bool(
                item.get("locked_by")
                or item.get("locked_client_id")
                or item.get("lease_id")
            )
            item["locked_by"] = "occupied" if occupied else ""
            item["locked_username"] = ""
            item["locked_client_id"] = ""
            item["lease_id"] = ""
            item["lease_generation"] = 0
        devices.append(item)
    sanitized["devices"] = devices
    return sanitized


@router.get("/api/devices/management")
async def devices_management(request: Request):
    """Return detailed device management data without leaking cached ownership."""
    principal = require_authenticated_user_when_auth_required(request)

    def response(payload: dict[str, Any], status_code: int = 200):
        return JSONResponse(
            content=_sanitize_management_payload(payload, principal=principal),
            status_code=status_code,
        )

    try:
        config = runtime.config_manager.load_config()
        client_id = runtime.get_client_id_from_request(request)
        if is_local_host(runtime.config_manager.get_ubuntu_host(config)):
            if has_blocked_adb_process():
                cached = _cached_management_payload(client_id)
                return response(cached or {
                    "devices": [], "success": True, "source": "local",
                    "warning": "Local adb server is blocked; skipped adb scan",
                })
            output, error, code = await asyncio.to_thread(
                runtime.run_local_shell_command, "adb devices", 5
            )
            if code != 0 and not output:
                return response({
                    "devices": [], "success": True, "warning": error,
                })
            device_ids = DeviceUtils.parse_adb_devices(output)
            if not device_ids:
                return response(_cached_management_payload(client_id) or {
                    "devices": [], "success": True, "source": "local",
                })
            props_output, props_error, props_code = await asyncio.to_thread(
                runtime.run_local_shell_command,
                _build_management_props_command(device_ids),
                15,
            )
            if props_code != 0:
                logger.warning("[Device Management] property query failed: %s", props_error)
            payload = _build_devices_management_payload(
                device_ids, _parse_management_device_props(props_output), config,
                client_id, _mgmt_username(request),
            )
            payload.update({"success": True, "source": "local"})
            return response(payload)

        ssh = runtime.ssh_manager.get_connection(config)
        if not ssh:
            cached = _cached_management_payload(client_id)
            if cached:
                cached.update({"stale": True, "warning": "SSH connection failed; showing cached device data"})
                return response(cached)
            return response({
                "success": False, "devices": [], "source": "ssh",
                "error": "SSH connection failed",
                "warning": "设备主机 SSH 连接失败，请检查主机、账号、密码或密钥配置。",
            })
        try:
            output, _, _ = runtime.ssh_manager.execute_command(ssh, "adb devices", timeout=5)
            device_ids = DeviceUtils.parse_adb_devices(output)
            if not device_ids:
                return response(_cached_management_payload(client_id) or {"devices": []})
            props_output, _, _ = runtime.ssh_manager.execute_command(
                ssh, _build_management_props_command(device_ids), timeout=15
            )
            return response(_build_devices_management_payload(
                device_ids, _parse_management_device_props(props_output), config,
                client_id, _mgmt_username(request),
            ))
        finally:
            runtime.ssh_manager.return_connection(ssh)
    except Exception as exc:
        logger.error("Error getting devices management: %s", exc, exc_info=True)
        return response({"success": False, "error": str(exc)}, status_code=500)
