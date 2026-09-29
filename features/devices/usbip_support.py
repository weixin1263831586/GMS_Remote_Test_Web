"""Shared helpers for the USB/IP integration API split.

从 integrations_api.py 拆出（2026-09 大文件收敛）：host 解析、
本地 worker 标识、cluster 设备标注/心跳/命令对账、来源清理 helper。
路由模块按需导入；integrations_api.py 聚合后再导出兼容旧测试。"""

from __future__ import annotations

import logging
import time

from fastapi import Request

from features.users import get_client_display_id_from_request
from foundation.networking import split_host_port

from . import runtime
from .adb_forward_api import (
    start_adb_forward as start_adb_forward,
)
from .adb_forward_api import (
    stop_adb_forward as stop_adb_forward,
)
from .usbip import detach_ubuntu_usbip_ports, usbip_manager
from .usbip_access import usbip_request_user
from .usbip_assignments import (
    load_usbip_assignments as _usbip_assignments,
)
from .usbip_assignments import (
    save_usbip_assignments as _save_usbip_assignments,
)
from .usbip_persistence import (
    usbip_assignment_lock as _usbip_assignment_lock,
)
from .utils import DeviceUtils


logger = logging.getLogger(__name__)

_USBIP_ATTACHING_STALE_SECONDS = 30 * 60
_USBIP_ADB_ENUMERATION_GRACE_SECONDS = 45

def _resolve_usbip_device_host(request: Request, config: dict | None = None, explicit: str | None = None) -> str:
    """Resolve the reachable Windows USB/IP host for this request."""
    if explicit:
        return explicit
    selected_config = config if config is not None else runtime.config_manager.load_config()
    client_id = runtime.get_client_id_from_request(request)
    if callable(runtime.resolve_tailscale_device_host):
        tunnel_host, _ = runtime.resolve_tailscale_device_host(request, client_id)
        if tunnel_host:
            return tunnel_host
    user = usbip_request_user(request)
    if user and user.role != "admin":
        return get_client_display_id_from_request(request) or ""
    return (
        selected_config.get("usbip_device_host")
        or selected_config.get("device_host")
        or get_client_display_id_from_request(request)
        or ""
    )

def _usbip_remote_host(device_host: str, usbip_attach_host: str | None = None) -> str:
    if usbip_attach_host:
        return usbip_attach_host
    host = str(device_host or "").split("@", 1)[-1]
    hostname, _port = split_host_port(host)
    return hostname or "127.0.0.1"

def _local_worker_id() -> str:
    from foundation.cluster_port import get_local_worker_id

    return get_local_worker_id()

def _adb_proxy_target_assignments(worker_id: str) -> list[dict]:
    """Return persisted ADB Proxy routes that currently target a Worker."""
    from .adb_proxy_service import adb_proxy_service

    return [
        item
        for item in adb_proxy_service.assignments().values()
        if str(item.get("target_worker_id") or "") == worker_id
    ]

def annotate_cluster_usbip_devices(
    devices: list[dict], worker_id: str = ""
) -> list[dict]:
    """Add persisted USB/IP source metadata to cluster device inventory."""
    metadata_by_serial: dict[str, dict[str, object]] = {}
    for assignment in _usbip_assignments().values():
        assignment_worker = str(assignment.get("worker_id") or "")
        if worker_id and assignment_worker != worker_id:
            continue
        if assignment.get("status") not in {"attached", "unknown", "cleanup_required"}:
            continue
        for serial in assignment.get("device_serials") or []:
            serial = str(serial or "").strip()
            if not serial:
                continue
            metadata = metadata_by_serial.setdefault(serial, {
                "is_usbip": True,
                "usbip_source_host": str(assignment.get("device_host") or ""),
                "usbip_busids": [],
            })
            busid = str(assignment.get("busid") or "").strip()
            if busid and busid not in metadata["usbip_busids"]:
                metadata["usbip_busids"].append(busid)

    annotated = []
    for device in devices:
        metadata = metadata_by_serial.get(str(device.get("serial") or ""))
        if not metadata:
            annotated.append(device)
            continue
        properties = {
            **(device.get("properties") or {}),
            **metadata,
        }
        annotated.append({
            **device,
            "transport": "usbip",
            "properties": properties,
        })
    return annotated

def reconcile_cluster_usbip_heartbeat(
    worker_id: str,
    devices: list[dict],
) -> bool:
    """Keep persisted USB/IP route state aligned with Worker ADB inventory.

    A target Worker restores its saved USB/IP attachments before publishing a
    heartbeat.  If the source export is unavailable after either host reboots,
    the route must not continue to look healthy merely because the Controller
    still has an ``attached`` record.
    """
    online_serials = {
        str(item.get("serial") or "").strip()
        for item in devices or []
        if str(item.get("serial") or "").strip()
        and str(item.get("state") or "").lower()
        not in {"offline", "unknown", "unauthorized"}
    }
    changed = False
    with _usbip_assignment_lock:
        assignments = _usbip_assignments()
        for key, assignment in list(assignments.items()):
            if str(assignment.get("worker_id") or "") != str(worker_id or ""):
                continue
            if assignment.get("status") not in {"attached", "unknown"}:
                continue
            expected = {
                str(serial or "").strip()
                for serial in assignment.get("device_serials") or []
                if str(serial or "").strip()
            }
            if not expected:
                continue
            # The USB transport can be attached before ADB publishes the new
            # device in the next Worker heartbeat.  Keep the confirmed
            # transport state during that normal enumeration window instead
            # of briefly presenting the route as unknown.
            try:
                assignment_age = time.time() - float(
                    assignment.get("timestamp") or 0
                )
            except (TypeError, ValueError):
                assignment_age = _USBIP_ADB_ENUMERATION_GRACE_SECONDS + 1
            if (
                assignment.get("status") == "attached"
                and not expected.issubset(online_serials)
                and 0 <= assignment_age <= _USBIP_ADB_ENUMERATION_GRACE_SECONDS
            ):
                continue
            next_status = (
                "attached" if expected.issubset(online_serials) else "unknown"
            )
            if assignment.get("status") == next_status:
                continue
            assignments[key] = {
                **assignment,
                "status": next_status,
                "timestamp": time.time(),
            }
            changed = True
        if changed:
            _save_usbip_assignments(assignments)
    return changed

def reconcile_cluster_usbip_command(command: dict, repository) -> None:
    """Apply a terminal Worker USB/IP result even after its HTTP waiter timed out."""
    command_type = str(command.get("command_type") or "")
    status = str(command.get("status") or "")
    if (
        command_type not in {"usbip_attach", "usbip_detach"}
        or status not in {"completed", "failed", "cancelled"}
    ):
        return

    worker_id = str(command.get("worker_id") or "")
    payload = command.get("payload") or {}
    result = command.get("result") or {}
    devices = result.get("devices")
    if isinstance(devices, list):
        repository.refresh_worker_devices(worker_id, devices)

    busids = {
        str(item or "").strip()
        for item in payload.get("busids") or []
        if str(item or "").strip()
    }
    if not busids:
        return
    device_host = str(payload.get("device_host") or "")
    source_host = str(payload.get("source_host") or "")
    command_generation = int(payload.get("generation") or 0)
    attached_serials = list(dict.fromkeys(
        str(item or "").strip()
        for item in result.get("new_devices") or []
        if str(item or "").strip()
    ))

    changed = False
    with _usbip_assignment_lock:
        assignments = _usbip_assignments()
        for key, current in list(assignments.items()):
            if (
                str(current.get("worker_id") or "") != worker_id
                or str(current.get("busid") or "") not in busids
                or (
                    device_host
                    and str(current.get("device_host") or "") != device_host
                )
                or (
                    source_host
                    and str(current.get("source_host") or "") not in {
                        "", source_host
                    }
                )
            ):
                continue
            if (
                command_generation
                and int(current.get("generation") or 0) != command_generation
            ):
                logger.info(
                    "[USB/IP] ignored stale terminal command %s generation=%s current=%s",
                    command.get("id"),
                    command_generation,
                    current.get("generation"),
                )
                continue
            if command_type == "usbip_detach":
                if status == "completed":
                    assignments.pop(key, None)
                    changed = True
                continue
            if status == "completed":
                current.update({
                    "source_host": source_host
                    or str(current.get("source_host") or ""),
                    "device_serials": attached_serials
                    or list(current.get("device_serials") or []),
                    "status": "attached",
                    "timestamp": time.time(),
                })
                assignments[key] = current
                changed = True
            elif current.get("status") in {"attaching", "unknown"}:
                if "回滚未完成" in str(command.get("error") or ""):
                    current.update({
                        "status": "cleanup_required",
                        "timestamp": time.time(),
                    })
                    assignments[key] = current
                else:
                    assignments.pop(key, None)
                changed = True
        if changed:
            _save_usbip_assignments(assignments)
def _persist_device_source_removal(devices_to_remove: list):
    """Remove device IDs from runtime config's usbip_devices_source and save."""
    if not devices_to_remove:
        return
    try:
        existing_runtime = runtime.config_manager.get_runtime_config()
        usbip_sources = existing_runtime.get("usbip_devices_source", {})
        for device_id in devices_to_remove:
            if device_id in usbip_sources:
                del usbip_sources[device_id]
        existing_runtime["usbip_devices_source"] = usbip_sources
        if runtime.config_manager.save_runtime_config(existing_runtime):
            logger.info(f"[USB/IP Stop] Persisted device source removal for {len(devices_to_remove)} devices")
    except Exception as e:
        logger.warning(f"[USB/IP Stop] Failed to persist device source removal: {e}")

def _usbip_devices_for_host(device_host: str) -> list[str]:
    """Return known USB/IP device ids for a host from memory and runtime config."""
    devices = set()
    with runtime.global_state.usbip_devices_source_lock:
        for device_id, device_info in runtime.global_state.usbip_devices_source.items():
            if (device_info or {}).get("source") == device_host:
                devices.add(device_id)
    for device_id, source in (getattr(usbip_manager, "device_sources", {}) or {}).items():
        if (source or {}).get("source") == device_host:
            devices.add(device_id)
    try:
        runtime_sources = (runtime.config_manager.get_runtime_config() or {}).get("usbip_devices_source") or {}
        if isinstance(runtime_sources, dict):
            for device_id, device_info in runtime_sources.items():
                if (device_info or {}).get("source") == device_host:
                    devices.add(device_id)
    except Exception as e:
        logger.warning("[USB/IP Stop] Failed to read runtime USB/IP sources: %s", e)
    return list(devices)

def _clear_usbip_device_sources(
    device_host: str,
    devices_to_remove: list[str],
) -> None:
    devices_to_remove = list(dict.fromkeys(devices_to_remove or []))
    with runtime.global_state.usbip_devices_source_lock:
        for device_id in devices_to_remove:
            if device_id in runtime.global_state.usbip_devices_source:
                del runtime.global_state.usbip_devices_source[device_id]
                logger.info(f"[USB/IP Stop] Removed device source: {device_id} from {device_host}")

    for device_id in devices_to_remove:
        if device_id in usbip_manager.device_sources:
            del usbip_manager.device_sources[device_id]

    _persist_device_source_removal(devices_to_remove)

def _invalidate_device_cache() -> None:
    """Clear the device-list cache so the next /api/devices/list re-queries ADB.

    USB/IP disconnect must invalidate it, otherwise the stale cache still
    returns the just-disconnected device (with its is_usbip flag) within TTL.
    """
    with runtime.global_state.device_cache_lock:
        runtime.global_state.device_cache = {"devices": [], "timestamp": 0}

def _mark_usbip_source_disconnected(
    device_host: str, *, has_remaining_assignments: bool,
) -> None:
    if has_remaining_assignments:
        return
    with runtime.global_state.usbip_states_lock:
        runtime.global_state.usbip_states[device_host] = {
            "connected": False,
            "timestamp": time.time(),
            "transport_connected": False,
            "adb_ready": False,
            "reconnecting": False,
            "protocol_status": {},
        }


__all__ = [
    "_adb_devices_on_ssh",
    "_adb_proxy_target_assignments",
    "_clear_usbip_device_sources",
    "_detach_ubuntu_usbip_for_devices",
    "_invalidate_device_cache",
    "_local_worker_id",
    "_mark_usbip_source_disconnected",
    "_persist_device_source_removal",
    "_resolve_usbip_device_host",
    "_usbip_devices_for_host",
    "_usbip_remote_host",
    "_wait_for_adb_devices_removed",
    "annotate_cluster_usbip_devices",
    "reconcile_cluster_usbip_command",
    "reconcile_cluster_usbip_heartbeat",
]


def _wait_for_adb_devices_removed(
    ssh,
    expected_removed: set[str],
    attempts: int = 6,
) -> set[str]:
    """Wait until target serials are no longer ADB-online after physical detach."""
    remaining = set(expected_removed) & _adb_devices_on_ssh(ssh)
    for _ in range(max(0, attempts - 1)):
        if not remaining:
            break
        time.sleep(1)
        remaining = set(expected_removed) & _adb_devices_on_ssh(ssh)
    return remaining

def _detach_ubuntu_usbip_for_devices(
    ssh,
    *,
    device_host: str,
    usbip_attach_host: str | None,
    devices_to_remove: list[str],
    busids: list[str] | None = None,
    detach_all: bool = False,
    settle: bool = True,
) -> dict[str, object]:
    """Detach Ubuntu USB/IP ports and verify target ADB serials disappear."""
    expected_removed = {str(device_id) for device_id in devices_to_remove or [] if str(device_id)}
    detach_kwargs = {"detach_all": detach_all}
    if busids:
        detach_kwargs["busids"] = busids
    detached_ports = detach_ubuntu_usbip_ports(
        ssh,
        _usbip_remote_host(device_host, usbip_attach_host),
        **detach_kwargs,
    )
    remaining = expected_removed & _adb_devices_on_ssh(ssh) if expected_removed else set()
    if remaining and not detach_all and not busids:
        logger.warning(
            "[USB/IP Stop] Target devices still present after host-filtered detach: %s; "
            "falling back to detach all Ubuntu USB/IP ports",
            sorted(remaining),
        )
        fallback_ports = detach_ubuntu_usbip_ports(ssh, None, detach_all=True)
        detached_ports = list(dict.fromkeys([*detached_ports, *fallback_ports]))
        remaining = expected_removed & _adb_devices_on_ssh(ssh)
    if remaining and settle:
        # ADB keeps detached USB/IP serials briefly as "offline"; wait for the
        # USB hotplug event to reap them before declaring a device still online.
        remaining = _wait_for_adb_devices_removed(ssh, expected_removed)
    return {
        "detached_ports": detached_ports,
        "remaining_devices": sorted(remaining),
    }

def _adb_devices_on_ssh(ssh) -> set[str]:
    result = runtime.ssh_manager.execute_command(
        ssh,
        "adb devices",
        timeout=8,
    )
    return set(DeviceUtils.parse_adb_devices(result.stdout or result.stderr or ""))
