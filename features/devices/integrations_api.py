from __future__ import annotations

import asyncio
import logging
import shlex
import threading
import time
import uuid

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from features.auth import require_elevated_admin
from features.users import get_client_display_id_from_request
from foundation.networking import split_host_port
from foundation.responses import error_response

from . import reconnect, runtime
from .adb_forward_api import (
    router as adb_forward_router,
)
from .adb_forward_api import (
    start_adb_forward as start_adb_forward,
)
from .adb_forward_api import (
    stop_adb_forward as stop_adb_forward,
)
from .locks import device_lock_manager
from .manager import device_manager
from .models import USBIPDisconnectRequest, USBIPStartRequest
from .support import (
    DeviceSSHConnection,
    acquire_device_operation_claim,
    audit_device_operation,
    format_device_list_info,
    notify_device_change,
    release_device_operation_claim,
)
from .usbip import detach_ubuntu_usbip_ports, find_device_host_password, usbip_manager
from .usbip_access import enforce_usbip_host_access, usbip_request_user
from .usbip_install_api import install_usbipd
from .usbip_install_api import router as usbip_install_router
from .utils import DeviceUtils


logger = logging.getLogger(__name__)
router = APIRouter()
router.include_router(adb_forward_router)
router.include_router(usbip_install_router)
_usbip_assignment_lock = threading.RLock()
_USBIP_ATTACHING_STALE_SECONDS = 30 * 60
_USBIP_ADB_ENUMERATION_GRACE_SECONDS = 45

__all__ = ["install_usbipd", "router"]


def _usbip_error_fields(message: str) -> dict[str, object]:
    detail = str(message or "")
    lowered = detail.lower()
    rules = (
        (("ssh", "凭据"), "USBIP_SOURCE_SSH_FAILED", "请检查来源主机SSH地址、凭据和sshd服务。"),
        (("usbipd未安装",), "USBIPD_NOT_INSTALLED", "请先在Windows来源主机安装usbipd-win。"),
        (("未找到android设备", "未找到android usb", "设备已不可用"), "USBIP_SOURCE_DEVICE_NOT_FOUND", "请刷新USB设备列表并确认设备仍连接来源主机。"),
        (("绑定失败", "bind failed"), "USBIP_BIND_FAILED", "请检查usbipd共享状态和Windows管理员权限。"),
        (("vhci",), "VHCI_LOAD_FAILED", "请在目标Linux主机安装USB/IP工具并加载vhci_hcd。"),
        (("attach",), "USBIP_ATTACH_FAILED", "请检查TCP 3240、防火墙及残留USB/IP会话。"),
        (("unauthorized",), "ADB_UNAUTHORIZED", "请在设备端确认ADB授权。"),
        (("offline",), "ADB_OFFLINE", "请检查USB链路并等待ADB重新枚举。"),
        (("回滚", "cleanup"), "ROLLBACK_INCOMPLETE", "请使用当前接入中的“清理”操作完成残留会话清理。"),
    )
    for markers, code, remediation in rules:
        if any(marker in lowered for marker in markers):
            return {
                "error_code": code,
                "retryable": code not in {"USBIPD_NOT_INSTALLED"},
                "remediation": remediation,
            }
    return {
        "error_code": "USBIP_OPERATION_FAILED",
        "retryable": True,
        "remediation": "请查看USB/IP分层状态和来源/目标主机日志后重试。",
    }


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


def _attached_usbip_serials(result: dict) -> list[str]:
    values = result.get("new_devices") or result.get("device_list")
    if not values:
        values = [
            device.get("serial")
            for device in result.get("devices") or []
            if isinstance(device, dict)
        ]
    return list(dict.fromkeys(
        str(item or "").strip()
        for item in values
        if str(item or "").strip()
    ))


def _rollback_local_usbip_attach(
    config: dict,
    *,
    device_host: str,
    usbip_attach_host: str | None,
    busids: list[str],
    device_password: str,
    device_serials: list[str],
) -> dict:
    errors: list[str] = []
    detached_ports: list[str] = []
    ubuntu_ssh = runtime.ssh_manager.get_connection(config)
    if not ubuntu_ssh:
        errors.append("无法连接接入主机执行USB/IP回滚")
    else:
        try:
            detached_ports = detach_ubuntu_usbip_ports(
                ubuntu_ssh,
                _usbip_remote_host(device_host, usbip_attach_host),
                busids=busids,
            )
        except Exception as exc:
            errors.append(f"接入主机USB/IP回滚失败: {exc}")
        finally:
            runtime.ssh_manager.return_connection(ubuntu_ssh)

    source_cleanup = usbip_manager.detach_source_sessions(
        device_host,
        busids,
        device_password,
    )
    if not source_cleanup.get("success"):
        errors.append(
            "来源主机USB/IP回滚失败: "
            + str(source_cleanup.get("error") or "unknown error")
        )

    for serial in device_serials:
        source = usbip_manager.device_sources.get(serial) or {}
        if source.get("source") == device_host:
            usbip_manager.device_sources.pop(serial, None)

    return {
        "success": not errors,
        "detached_ports": detached_ports,
        "errors": errors,
    }


def _usbip_assignments() -> dict[str, dict]:
    getter = getattr(runtime.config_manager, "get_runtime_config", None)
    runtime_config = getter() if callable(getter) else {}
    runtime_config = runtime_config or {}
    assignments = runtime_config.get("usbip_cluster_assignments") or {}
    return dict(assignments) if isinstance(assignments, dict) else {}


def _save_usbip_assignments(assignments: dict[str, dict]) -> None:
    updater = getattr(runtime.config_manager, "update_runtime_config", None)
    if callable(updater):
        saved = updater({"usbip_cluster_assignments": assignments})
    else:
        getter = getattr(runtime.config_manager, "get_runtime_config", None)
        runtime_config = getter() if callable(getter) else {}
        runtime_config = runtime_config or {}
        runtime_config["usbip_cluster_assignments"] = assignments
        saved = runtime.config_manager.save_runtime_config(runtime_config)
    if not saved:
        raise RuntimeError("无法保存USB/IP集群分配状态")


def _record_usbip_network_quality(
    device_host: str,
    worker_id: str,
    quality: dict[str, object],
) -> None:
    if not quality:
        return
    with _usbip_assignment_lock:
        getter = getattr(runtime.config_manager, "get_runtime_config", None)
        runtime_config = getter() if callable(getter) else {}
        runtime_config = runtime_config or {}
        history = runtime_config.get("usbip_network_quality_history") or []
        if not isinstance(history, list):
            history = []
        history = [
            *history[-99:],
            {
                **quality,
                "device_host": device_host,
                "worker_id": worker_id,
                "timestamp": time.time(),
            },
        ]
        updater = getattr(runtime.config_manager, "update_runtime_config", None)
        if callable(updater):
            updater({"usbip_network_quality_history": history})
        else:
            runtime_config["usbip_network_quality_history"] = history
            runtime.config_manager.save_runtime_config(runtime_config)


def _local_worker_id() -> str:
    try:
        from features.cluster import get_cluster_service

        return str(get_cluster_service().config.local_worker_id or "worker-local")
    except Exception:
        return "worker-local"


def _persist_local_usbip_sources(device_host: str, serials: list[str]) -> None:
    """Persist USB/IP source metadata used by local device-list endpoints."""
    serials = list(dict.fromkeys(
        str(serial or "").strip()
        for serial in serials
        if str(serial or "").strip()
    ))
    if not serials:
        return

    timestamp = time.time()
    updates: dict[str, dict[str, object]] = {}
    try:
        runtime_config = runtime.config_manager.get_runtime_config() or {}
        persisted = runtime_config.get("usbip_devices_source") or {}
        if not isinstance(persisted, dict):
            persisted = {}
        persisted = dict(persisted)
        for serial in serials:
            existing_source = str(
                (persisted.get(serial) or {}).get("source") or ""
            ).strip()
            if existing_source and existing_source != device_host:
                logger.info(
                    "[USB/IP] Keep existing source for %s: %s (new: %s)",
                    serial,
                    existing_source,
                    device_host,
                )
                continue
            updates[serial] = {"source": device_host, "timestamp": timestamp}
        if updates:
            persisted.update(updates)
            updater = getattr(runtime.config_manager, "update_runtime_config", None)
            if callable(updater):
                saved = updater({"usbip_devices_source": persisted})
            else:
                runtime_config["usbip_devices_source"] = persisted
                saved = runtime.config_manager.save_runtime_config(runtime_config)
            if not saved:
                raise RuntimeError("无法保存USB/IP设备来源")
    except Exception as exc:
        logger.warning("[USB/IP] Failed to persist local device sources: %s", exc)

    if not updates:
        return
    with runtime.global_state.usbip_devices_source_lock:
        runtime.global_state.usbip_devices_source.update(updates)
    getattr(usbip_manager, "device_sources", {}).update(updates)


def _reconcile_usbip_assignment_serials(
    device_host: str,
    source_devices: list[dict],
) -> bool:
    """Backfill assignment serials from the authoritative source busid list."""
    serial_by_busid = {
        str(item.get("busid") or ""): str(item.get("serial") or "").strip()
        for item in source_devices
        if str(item.get("busid") or "") and str(item.get("serial") or "").strip()
    }
    if not serial_by_busid:
        return False

    local_serials: list[str] = []
    changed = False
    with _usbip_assignment_lock:
        assignments = _usbip_assignments()
        for key, assignment in assignments.items():
            if str(assignment.get("device_host") or "") != device_host:
                continue
            serial = serial_by_busid.get(str(assignment.get("busid") or ""))
            if not serial:
                continue
            if assignment.get("device_serials") != [serial]:
                assignments[key] = {**assignment, "device_serials": [serial]}
                changed = True
            if str(assignment.get("worker_id") or "") == _local_worker_id():
                local_serials.append(serial)
        if changed:
            _save_usbip_assignments(assignments)
    _persist_local_usbip_sources(device_host, local_serials)
    return changed


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


def _usbip_assignment_key(device_host: str, busid: str) -> str:
    return f"{device_host}|{busid}"


def _next_transport_generation(assignments: dict[str, dict]) -> int:
    return max(
        int(time.time() * 1000),
        max(
            (int(item.get("generation") or 0) for item in assignments.values()),
            default=0,
        ) + 1,
    )


def _is_usbip_recoverable_attach_error(exc: Exception) -> bool:
    detail = str(getattr(exc, "detail", "") or exc).lower()
    return (
        "busy (exported)" in detail
        or "残留usb/ip会话占用" in detail
        or "device in error state" in detail
    )


def _is_usbip_export_busy(exc: Exception) -> bool:
    detail = str(getattr(exc, "detail", "") or exc).lower()
    return "busy (exported)" in detail or "残留usb/ip会话占用" in detail


def _usbip_worker_command_timeout(busids: list[str]) -> int:
    """Cover the Worker's bounded attach+rollback budget for a multi-select."""
    return 120 + 25 * max(1, len(busids))


def _preserve_usbip_assignment_after_error(exc: Exception) -> str:
    detail = str(getattr(exc, "detail", "") or exc)
    if isinstance(exc, HTTPException) and exc.status_code == 504:
        return "unknown"
    if "回滚未完成" in detail:
        return "cleanup_required"
    return ""


def _adb_devices_on_ssh(ssh) -> set[str]:
    output, error, _code = runtime.ssh_manager.execute_command(
        ssh,
        "adb devices",
        timeout=8,
    )
    return set(DeviceUtils.parse_adb_devices(output or error or ""))


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

# ==================== USB/IP Status ====================

@router.get("/api/usbip/source-devices")
async def list_usbip_source_devices(
    request: Request,
    device_host: str | None = None,
    _elevated=Depends(require_elevated_admin),
):
    """List selectable Android USB busids on an authorized source host."""
    config = runtime.config_manager.load_config()
    resolved = _resolve_usbip_device_host(request, config, device_host)
    enforce_usbip_host_access(request, device_host, resolved)
    result = await asyncio.to_thread(usbip_manager.list_source_devices, resolved)
    if not result.get("success"):
        if "凭据" in str(result.get("error") or ""):
            return error_response(
                result.get("error"), status_code=401,
                need_password=True, device_host=resolved,
            )
        message = result.get("error", "USB设备枚举失败")
        return error_response(
            message,
            status_code=500,
            **_usbip_error_fields(message),
        )
    _reconcile_usbip_assignment_serials(
        resolved,
        result.get("devices") or [],
    )
    return JSONResponse(content=result)


@router.get("/api/usbip/status")
async def get_usbip_status(
    request: Request,
    device_host: str | None = None,
):
    """Get USB/IP status (supports specifying host)."""
    config = runtime.config_manager.load_config()
    request_host = _resolve_usbip_device_host(request, config)
    enforce_usbip_host_access(request, device_host, request_host)
    client_id = device_host or request_host

    with runtime.global_state.usbip_states_lock:
        state_info = runtime.global_state.usbip_states.get(client_id, {"connected": False, "timestamp": 0})
        connected = state_info["connected"]

    if state_info.get("reconnecting"):
        current_devices = await asyncio.to_thread(
            device_manager.get_connected_devices,
            True,
        )
        reconnect.reconcile_observed_usbip_devices(current_devices)
        with runtime.global_state.usbip_states_lock:
            state_info = runtime.global_state.usbip_states.get(client_id, state_info)
            connected = state_info["connected"]

    if not connected:
        with runtime.global_state.usbip_devices_source_lock:
            has_devices_from_host = any(
                device_info.get("source") == client_id
                for device_info in runtime.global_state.usbip_devices_source.values()
            )
            if has_devices_from_host:
                connected = True

    assignments = [
        item for item in _usbip_assignments().values()
        if str(item.get("device_host") or "") == client_id
    ]
    cluster_selections = []
    if assignments:
        connected = True
        grouped: dict[tuple[str, str], dict[str, object]] = {}
        for item in assignments:
            group = (
                str(item.get("worker_id") or ""),
                str(item.get("source_host") or ""),
            )
            grouped_item = grouped.setdefault(group, {
                "busids": [],
                "device_serials": [],
                "device_serials_by_busid": {},
                "statuses_by_busid": {},
                "generations_by_busid": {},
                "network_quality_by_busid": {},
            })
            busid = str(item.get("busid") or "")
            serials = list(dict.fromkeys(
                str(serial or "").strip()
                for serial in item.get("device_serials") or []
                if str(serial or "").strip()
            ))
            if busid:
                grouped_item["busids"].append(busid)
                grouped_item["device_serials_by_busid"][busid] = serials
                grouped_item["statuses_by_busid"][busid] = str(
                    item.get("status") or "unknown"
                )
                grouped_item["generations_by_busid"][busid] = int(
                    item.get("generation") or 0
                )
                grouped_item["network_quality_by_busid"][busid] = (
                    item.get("network_quality") or {}
                )
            for serial in serials:
                if serial not in grouped_item["device_serials"]:
                    grouped_item["device_serials"].append(serial)
        cluster_selections = [
            {
                "device_host": client_id,
                "source_host": source_host,
                "worker_id": worker_id,
                "busids": sorted(grouped_item["busids"]),
                "device_serials": grouped_item["device_serials"],
                "device_serials_by_busid": (
                    grouped_item["device_serials_by_busid"]
                ),
                "statuses_by_busid": grouped_item["statuses_by_busid"],
                "generations_by_busid": grouped_item["generations_by_busid"],
                "network_quality_by_busid": grouped_item[
                    "network_quality_by_busid"
                ],
            }
            for (worker_id, source_host), grouped_item in grouped.items()
        ]

    logger.info(f"[USB/IP Status] device_host={client_id}, connected={connected}, device_count={len(runtime.global_state.usbip_devices_source)}")
    assignment_states = {
        str(item.get("status") or "unknown") for item in assignments
    }
    protocol_state = str((state_info.get("protocol_status") or {}).get("mode") or "unknown")
    transport_state = (
        "cleanup_required" if "cleanup_required" in assignment_states
        else "detaching" if "detaching" in assignment_states
        else "attaching" if "attaching" in assignment_states
        else "degraded" if "unknown" in assignment_states
        else "attached" if connected
        else "disconnected"
    )
    readiness = (
        "test_ready" if protocol_state in {"adb", "fastboot", "recovery"}
        else "protocol_ready" if protocol_state not in {"unknown", "offline", "unauthorized"}
        else "transport_ready" if connected
        else "not_ready"
    )
    from .transport_registry import build_transport_records

    getter = getattr(runtime.config_manager, "get_runtime_config", None)
    runtime_config = getter() if callable(getter) else {}
    runtime_config = runtime_config or {}
    quality_history = [
        item for item in runtime_config.get("usbip_network_quality_history") or []
        if str(item.get("device_host") or "") == client_id
    ][-20:]
    return JSONResponse(content={
        "connected": connected,
        "device_host": client_id,
        "device_count": len(runtime.global_state.usbip_devices_source),
        "transport_connected": bool(state_info.get("transport_connected", False)),
        "adb_ready": bool(state_info.get("adb_ready", False)),
        "reconnecting": bool(state_info.get("reconnecting", False)),
        "protocol_status": state_info.get("protocol_status") or {},
        "transport_state": transport_state,
        "protocol_state": protocol_state,
        "readiness": readiness,
        "cluster_selection": cluster_selections[0] if cluster_selections else None,
        "cluster_selections": cluster_selections,
        "transport_records": build_transport_records(
            usbip_assignments=[
                {**item, "protocol_state": protocol_state}
                for item in assignments
            ]
        ),
        "network_quality_history": quality_history,
    })


# ==================== USB/IP Connect ====================

@router.post("/api/usbip/connect")
async def start_usbip(
    request: Request,
    req: USBIPStartRequest | None = Body(default=None),
    help: bool = Query(False),
    _elevated=Depends(require_elevated_admin),
):
    resp = (
        runtime.generate_help_or_continue(help, "POST", "/api/usbip/connect")
        if runtime.generate_help_or_continue is not None
        else None
    )
    if resp:
        return resp

    try:
        config = runtime.config_manager.load_config()
        client_id = runtime.get_client_id_from_request(request)

        request_data = req.model_dump() if req else {}

        usbip_attach_host = None
        tunnel_host = None

        explicit_device_host = request_data.get("device_host")
        if explicit_device_host:
            if usbip_request_user(request):
                enforce_usbip_host_access(
                    request, explicit_device_host,
                    _resolve_usbip_device_host(request, config),
                )
            device_host = explicit_device_host
        else:
            tunnel_host, tunnel_usbip_host = runtime.resolve_tailscale_device_host(request, client_id)
            if tunnel_host:
                device_host = tunnel_host
                usbip_attach_host = tunnel_usbip_host
                logger.info(f"[USB/IP] Tailscale direct mode: {device_host} attach={usbip_attach_host}")
            else:
                # 可连接主机优先级：显式配置 > 当前客户端 username@ip（client_hosts 映射）。
                # client_id 是用户安全边界，不能作为连接主机。
                # 可连接主机由 client_hosts 映射的 username@client_ip 构造。
                device_host = _resolve_usbip_device_host(request, config)
                if not device_host or '@' not in device_host:
                    # 没有可连接的主机地址（未配置、且 client_hosts 未映射当前客户端）。
                    return JSONResponse(content={
                        "success": False,
                        "error": "未配置设备主机地址。请在「配置」中设置 device_host（格式 user@ip，例如 gms@192.168.1.100），或确保当前客户端已识别为主机。",
                        "need_config": True,
                    }, status_code=400)

        logger.info(f"[USB/IP] Using device_host: {device_host}")
        try:
            from features.devices.reconnect import is_usbip_reconnect_suppressed
            if is_usbip_reconnect_suppressed(device_host) and not request_data.get("manual_connect"):
                return JSONResponse(content={
                    "success": False,
                    "manual_disconnect_suppressed": True,
                    "device_host": device_host,
                    "error": "USB/IP 已手动断开，自动重连已暂停；如需重新连接请点击本地设备。",
                })
        except Exception as e:
            logger.warning("[USB/IP] Failed to check reconnect suppression for %s: %s", device_host, e)

        windows_device_host = device_host

        submitted_device_password = request_data.get("device_password") or ""
        device_password = submitted_device_password or find_device_host_password(device_host, config) or config.get("device_pswd", "")
        if not device_password:
            return error_response(
                f"SSH credentials for {device_host} not found, please enter SSH password on login page",
                status_code=401,
                need_password=True,
                device_host=device_host,
            )

        worker_id = str(request_data.get("worker_id") or "")
        busids = [str(item) for item in request_data.get("busids") or []]
        if busids:
            active_siblings = [
                item for item in _usbip_assignments().values()
                if str(item.get("device_host") or "") == device_host
                and str(item.get("busid") or "") not in set(busids)
                and str(item.get("status") or "") in {
                    "attaching", "attached", "unknown", "cleanup_required",
                }
            ]
            if active_siblings:
                return error_response(
                    "该Windows来源仍有其他USB/IP设备处于活动状态；为避免停止全局ADB影响现有任务，本次接入已拒绝",
                    status_code=409,
                    error_code="USBIP_SOURCE_ADB_IN_USE",
                    remediation="请先断开该来源上的其他USB/IP分配，或将新设备接入另一台Windows来源主机。",
                )
        adb_proxy_routes: list[dict] = []
        proxy_serials: set[str] = set()
        source_devices: dict[str, str] = {}
        unknown_busids: list[str] = []
        if worker_id:
            from features.cluster import get_cluster_service
            from features.cluster.api import _require_cluster_enabled, _run_worker_command

            cluster = get_cluster_service()
            adb_proxy_routes = _adb_proxy_target_assignments(worker_id)
            if busids:
                list_source_devices = getattr(
                    usbip_manager, "list_source_devices", None
                )
                source_inventory = (
                    await asyncio.to_thread(
                        list_source_devices,
                        device_host,
                        device_password,
                    )
                    if callable(list_source_devices)
                    else {
                        "success": False,
                        "error": "当前USB/IP实现无法核对来源设备序列号",
                    }
                )
                if source_inventory.get("success"):
                    source_devices = {
                        str(item.get("busid") or ""): str(
                            item.get("serial") or ""
                        ).strip()
                        for item in source_inventory.get("devices") or []
                    }
                elif adb_proxy_routes:
                    return error_response(
                        source_inventory.get(
                            "error", "无法核对USB/IP设备序列号"
                        ),
                        status_code=409,
                    )
                else:
                    logger.info(
                        "[USB/IP] Source serial inventory unavailable: %s",
                        source_inventory.get("error") or "unknown error",
                    )
            if adb_proxy_routes:
                proxy_serials = {
                    str(serial or "").strip()
                    for item in adb_proxy_routes
                    for serial in item.get("devices") or []
                    if str(serial or "").strip()
                }
                if not busids:
                    return error_response(
                        "目标主机已有ADB Proxy设备；混合接入时必须明确选择USB设备",
                        status_code=409,
                    )
                unknown_busids = [
                    busid for busid in busids
                    if not source_devices.get(busid)
                ]
                if unknown_busids:
                    logger.info(
                        "[USB/IP] Source serial unavailable; deferring conflict "
                        "check to target side ADB: worker_id=%s busids=%s",
                        worker_id,
                        unknown_busids,
                    )
                serial_conflicts = sorted({
                    source_devices[busid]
                    for busid in busids
                    if source_devices[busid] in proxy_serials
                })
                if serial_conflicts:
                    return error_response(
                        (
                            "所选USB/IP设备与当前ADB Proxy设备序列号冲突: "
                            + ", ".join(serial_conflicts)
                        ),
                        status_code=409,
                    )
            if worker_id != cluster.config.local_worker_id:
                _require_cluster_enabled(remote=True)
                worker = cluster.repository.get_worker(worker_id) or {}
                if not (worker.get("capabilities") or {}).get("usbip_client"):
                    return error_response(
                        f"{worker_id} 尚未安装Worker USB/IP能力，请重新部署Worker",
                        status_code=409,
                    )
                if not busids:
                    return error_response("远端Worker接入必须选择USB设备", status_code=400)
                network_quality = {}
                if (worker.get("capabilities") or {}).get("usbip_preflight"):
                    preflight = await _run_worker_command(
                        worker_id,
                        "usbip_preflight",
                        {"source_host": _usbip_remote_host(device_host)},
                        timeout=20,
                    )
                    network_quality = preflight.get("network_quality") or {}
                    _record_usbip_network_quality(
                        device_host, worker_id, network_quality
                    )
                    if not network_quality.get("reachable"):
                        return error_response(
                            f"{worker_id} 无法连接USB/IP来源TCP 3240",
                            status_code=409,
                            error_code="USBIP_TCP_UNREACHABLE",
                            retryable=True,
                            remediation="请检查usbipd服务、TCP 3240防火墙和来源到Worker的网络路由。",
                            network_quality=network_quality,
                        )
                with _usbip_assignment_lock:
                    assignments = _usbip_assignments()
                    now = time.time()
                    assignments = {
                        key: value for key, value in assignments.items()
                        if not (
                            value.get("status") == "attaching"
                            and now - float(value.get("timestamp") or 0)
                            > _USBIP_ATTACHING_STALE_SECONDS
                        )
                    }
                    conflicts = [
                        busid for busid in busids
                        if (
                            (
                                assignments.get(
                                    _usbip_assignment_key(device_host, busid), {}
                                ).get("worker_id") not in {None, "", worker_id}
                            )
                            or assignments.get(
                                _usbip_assignment_key(device_host, busid), {}
                            ).get("status") in {
                                "attaching", "unknown", "cleanup_required",
                            }
                        )
                    ]
                    if conflicts:
                        conflict_targets = sorted({
                            str(assignments.get(
                                _usbip_assignment_key(device_host, busid), {}
                            ).get("worker_id") or "")
                            for busid in conflicts
                        } - {""})
                        return error_response(
                            (
                                f"USB设备已接入其他主机: {', '.join(conflicts)}"
                                + (
                                    f" → {', '.join(conflict_targets)}"
                                    if conflict_targets else ""
                                )
                            ),
                            status_code=409,
                        )
                    operation_generation = _next_transport_generation(assignments)
                    operation_id = f"usbip-attach-{uuid.uuid4().hex}"
                    for busid in busids:
                        assignments[_usbip_assignment_key(device_host, busid)] = {
                            "device_host": device_host,
                            "source_host": "",
                            "worker_id": worker_id,
                            "busid": busid,
                            "status": "attaching",
                            "generation": operation_generation,
                            "operation_id": operation_id,
                            "network_quality": network_quality,
                            "timestamp": time.time(),
                        }
                    _save_usbip_assignments(assignments)
                prepared: dict = {}
                try:
                    prepared = await asyncio.to_thread(
                        usbip_manager.bind_source_devices,
                        device_host,
                        busids,
                        device_password,
                    )
                    if not prepared.get("success"):
                        raise RuntimeError(
                            prepared.get("error", "USB/IP设备绑定失败")
                        )
                    prepared_busids = list(dict.fromkeys(
                        str(item or "").strip()
                        for item in prepared.get("busids") or []
                        if str(item or "").strip()
                    ))
                    if set(prepared_busids) != set(busids):
                        missing = [item for item in busids if item not in prepared_busids]
                        raise RuntimeError(
                            "USB/IP设备未全部完成绑定: " + ", ".join(missing)
                        )
                    prepared["busids"] = prepared_busids
                    attach_payload = {
                        "device_host": device_host,
                        "source_host": prepared["source_host"],
                        "busids": prepared["busids"],
                        "generation": operation_generation,
                        "operation_id": operation_id,
                    }
                    if adb_proxy_routes:
                        attach_payload["adb_server_socket"] = (
                            "tcp:127.0.0.1:5039"
                        )
                    command_timeout = _usbip_worker_command_timeout(
                        prepared["busids"]
                    )
                    recovered_stale_session = False
                    try:
                        result = await _run_worker_command(
                            worker_id,
                            "usbip_attach",
                            attach_payload,
                            timeout=command_timeout,
                        )
                    except HTTPException as attach_exc:
                        if (
                            not request_data.get("manual_connect")
                            or not _is_usbip_recoverable_attach_error(attach_exc)
                        ):
                            raise
                        logger.warning(
                            "[USB/IP] Recovering source export after attach error: "
                            "device_host=%s worker_id=%s busids=%s",
                            device_host,
                            worker_id,
                            prepared["busids"],
                        )
                        recovery = await asyncio.to_thread(
                            usbip_manager.detach_source_sessions,
                            device_host,
                            prepared["busids"],
                            device_password,
                        )
                        if not recovery.get("success"):
                            raise HTTPException(
                                status_code=409,
                                detail=(
                                    "USB/IP残留会话自动清理失败: "
                                    f"{recovery.get('error') or attach_exc.detail}"
                                ),
                            ) from attach_exc
                        rebound = await asyncio.to_thread(
                            usbip_manager.bind_source_devices,
                            device_host,
                            prepared["busids"],
                            device_password,
                        )
                        rebound_busids = list(dict.fromkeys(
                            str(item or "").strip()
                            for item in rebound.get("busids") or []
                            if str(item or "").strip()
                        ))
                        if (
                            not rebound.get("success")
                            or set(rebound_busids) != set(prepared["busids"])
                        ):
                            raise HTTPException(
                                status_code=409,
                                detail=(
                                    "USB/IP源设备恢复绑定失败: "
                                    f"{rebound.get('error') or attach_exc.detail}"
                                ),
                            ) from attach_exc
                        prepared.update({
                            "source_host": rebound.get("source_host")
                            or prepared["source_host"],
                            "busids": rebound_busids,
                        })
                        attach_payload.update({
                            "source_host": prepared["source_host"],
                            "busids": prepared["busids"],
                        })
                        await asyncio.sleep(1)
                        result = await _run_worker_command(
                            worker_id,
                            "usbip_attach",
                            attach_payload,
                            timeout=command_timeout,
                        )
                        recovered_stale_session = True
                    if recovered_stale_session:
                        result["recovered_stale_session"] = True
                    attached_serials = _attached_usbip_serials(result)
                    serial_conflicts = sorted(
                        set(attached_serials) & proxy_serials
                    )
                    if serial_conflicts:
                        rollback_error = ""
                        try:
                            await _run_worker_command(
                                worker_id,
                                "usbip_detach",
                                {
                                    "device_host": device_host,
                                    "source_host": prepared["source_host"],
                                    "busids": prepared["busids"],
                                    "generation": operation_generation,
                                    "operation_id": operation_id,
                                },
                                timeout=90,
                            )
                        except Exception as rollback_exc:
                            rollback_error = str(
                                getattr(rollback_exc, "detail", "")
                                or rollback_exc
                            )
                        source_cleanup = await asyncio.to_thread(
                            usbip_manager.detach_source_sessions,
                            device_host,
                            prepared["busids"],
                            device_password,
                        )
                        if not source_cleanup.get("success"):
                            rollback_error = "; ".join(filter(None, [
                                rollback_error,
                                str(source_cleanup.get("error") or ""),
                            ]))
                        detail = (
                            "USB/IP设备与当前ADB Proxy设备序列号冲突，"
                            "已自动回滚: "
                            + ", ".join(serial_conflicts)
                        )
                        if rollback_error:
                            detail += f"；回滚需要人工确认: {rollback_error}"
                        raise HTTPException(status_code=409, detail=detail)
                except Exception as exc:
                    with _usbip_assignment_lock:
                        assignments = _usbip_assignments()
                        preserved_status = _preserve_usbip_assignment_after_error(
                            exc
                        )
                        for busid in busids:
                            key = _usbip_assignment_key(device_host, busid)
                            current = assignments.get(key) or {}
                            if (
                                current.get("worker_id") == worker_id
                                and current.get("status") == "attaching"
                            ):
                                if preserved_status:
                                    current.update({
                                        "source_host": str(
                                            prepared.get("source_host") or ""
                                        ),
                                        "status": preserved_status,
                                        "timestamp": time.time(),
                                    })
                                    assignments[key] = current
                                else:
                                    assignments.pop(key, None)
                        _save_usbip_assignments(assignments)
                    detail = str(getattr(exc, "detail", "") or exc)
                    if (
                        isinstance(exc, HTTPException)
                        and exc.status_code in {409, 502}
                        and _is_usbip_export_busy(exc)
                    ):
                        raise HTTPException(status_code=409, detail=detail) from exc
                    raise
                with _usbip_assignment_lock:
                    assignments = _usbip_assignments()
                    attached_serials = _attached_usbip_serials(result)
                    known_serials = list(dict.fromkeys(
                        source_devices.get(busid, "")
                        for busid in prepared["busids"]
                        if source_devices.get(busid, "")
                    ))
                    reported_serials = attached_serials or known_serials
                    prepared_set = set(prepared["busids"])
                    for requested_busid in busids:
                        if requested_busid not in prepared_set:
                            assignments.pop(
                                _usbip_assignment_key(
                                    device_host, requested_busid
                                ),
                                None,
                            )
                    for busid in prepared["busids"]:
                        busid_serials = (
                            [source_devices[busid]]
                            if source_devices.get(busid)
                            else attached_serials
                            if len(prepared["busids"]) == 1
                            else []
                        )
                        assignments[_usbip_assignment_key(device_host, busid)] = {
                            "device_host": device_host,
                            "source_host": prepared["source_host"],
                            "worker_id": worker_id,
                            "busid": busid,
                            "device_serials": busid_serials,
                            "status": "attached",
                            "generation": operation_generation,
                            "operation_id": operation_id,
                            "timestamp": time.time(),
                        }
                    _save_usbip_assignments(assignments)
                # The attach command already returned the Worker's current device
                # list. Apply it immediately so the UI shows the newly attached
                # device without waiting for the next heartbeat (~15s lag).
                if "devices" in result:
                    try:
                        cluster.repository.refresh_worker_devices(
                            worker_id, result.get("devices") or []
                        )
                    except Exception as exc:
                        logger.warning(
                            "[USB/IP] failed to refresh worker devices for %s: %s",
                            worker_id,
                            exc,
                        )
                return JSONResponse(content={
                    **result,
                    "success": True,
                    "device_host": device_host,
                    "worker_id": worker_id,
                    "source_host": prepared["source_host"],
                    "busids": prepared["busids"],
                    "transport_connected": bool(result.get("attached_busids")),
                    "adb_ready": bool(result.get("devices")),
                    "network_quality": result.get("network_quality") or network_quality,
                    "device_list": [
                        item.get("serial")
                        for item in result.get("devices") or []
                        if item.get("serial")
                    ],
                    "device_serials": reported_serials,
                    "message": (
                        f"✅ USB/IP传输已连接，设备："
                        f"{', '.join(reported_serials) or '尚未识别'}"
                        + (
                            f"，已接入 {worker_id}"
                            if result.get("devices")
                            else f"，已接入 {worker_id}，等待ADB枚举完成"
                        )
                    ),
                })

        if worker_id and busids:
            with _usbip_assignment_lock:
                assignments = _usbip_assignments()
                conflicts = [
                    busid for busid in busids
                    if assignments.get(
                        _usbip_assignment_key(device_host, busid), {}
                    ).get("worker_id") not in {None, "", worker_id}
                ]
            if conflicts:
                conflict_targets = sorted({
                    str(assignments.get(
                        _usbip_assignment_key(device_host, busid), {}
                    ).get("worker_id") or "")
                    for busid in conflicts
                } - {""})
                return error_response(
                    (
                        f"USB设备已接入其他主机: {', '.join(conflicts)}"
                        + (
                            f" → {', '.join(conflict_targets)}"
                            if conflict_targets else ""
                        )
                    ),
                    status_code=409,
                )

        start_kwargs = {"usbip_attach_host": usbip_attach_host}
        if busids:
            start_kwargs["selected_busids"] = busids
        if adb_proxy_routes:
            start_kwargs["adb_server_socket"] = "tcp:127.0.0.1:5039"
        result = await asyncio.to_thread(
            usbip_manager.start_usbip,
            device_host,
            device_password,
            **start_kwargs,
        )
        result["device_host"] = device_host
        _record_usbip_network_quality(
            device_host,
            worker_id or _local_worker_id(),
            result.get("network_quality") or {},
        )

        if not result.get("success"):
            for key, value in _usbip_error_fields(
                str(result.get("error") or "USB/IP连接失败")
            ).items():
                result.setdefault(key, value)

        if result.get("success"):
            device_list = _attached_usbip_serials(result)
            known_serials = list(dict.fromkeys(
                source_devices.get(busid, "")
                for busid in busids
                if source_devices.get(busid, "")
            ))
            reported_serials = device_list or known_serials
            serial_conflicts = sorted(set(device_list) & proxy_serials)
            if serial_conflicts:
                rollback = await asyncio.to_thread(
                    _rollback_local_usbip_attach,
                    config,
                    device_host=device_host,
                    usbip_attach_host=usbip_attach_host,
                    busids=busids,
                    device_password=device_password,
                    device_serials=device_list,
                )
                detail = (
                    "USB/IP设备与当前ADB Proxy设备序列号冲突，"
                    "已自动回滚: "
                    + ", ".join(serial_conflicts)
                )
                if not rollback.get("success"):
                    detail += "；回滚需要人工确认: " + "; ".join(
                        rollback.get("errors") or []
                    )
                return error_response(detail, status_code=409)
            if worker_id and busids:
                with _usbip_assignment_lock:
                    assignments = _usbip_assignments()
                    local_generation = _next_transport_generation(assignments)
                    local_operation_id = f"usbip-attach-{uuid.uuid4().hex}"
                    for busid in busids:
                        busid_serials = (
                            [source_devices[busid]]
                            if source_devices.get(busid)
                            else device_list
                            if len(busids) == 1
                            else []
                        )
                        assignments[_usbip_assignment_key(device_host, busid)] = {
                            "device_host": device_host,
                            "source_host": _usbip_remote_host(
                                device_host, usbip_attach_host
                            ),
                            "worker_id": worker_id,
                            "busid": busid,
                            "device_serials": busid_serials,
                            "status": "attached",
                            "generation": local_generation,
                            "operation_id": local_operation_id,
                            "network_quality": result.get("network_quality") or {},
                            "timestamp": time.time(),
                        }
                    _save_usbip_assignments(assignments)
            result["transport_connected"] = bool(result.get("transport_connected") or result.get("devices"))
            result["adb_ready"] = bool(device_list)
            result["device_serials"] = reported_serials
            result["message"] = (
                "✅ USB/IP传输已连接，设备："
                f"{', '.join(reported_serials) or '尚未识别'}"
                + (
                    "，ADB已在线"
                    if device_list
                    else "，等待ADB枚举完成"
                )
            )
            if not worker_id or worker_id == _local_worker_id():
                _persist_local_usbip_sources(device_host, reported_serials)

            if request_data.get("manual_connect"):
                try:
                    from features.devices.reconnect import clear_usbip_reconnect_suppression
                    clear_usbip_reconnect_suppression(device_host, device_list)
                except Exception as e:
                    logger.warning("[USB/IP] Failed to clear reconnect suppression for devices %s: %s", device_list, e)

            if submitted_device_password:
                try:
                    if runtime.config_manager.upsert_device_host_password(device_host, submitted_device_password):
                        logger.info(f"[USB/IP Start] Saved SSH credential for {device_host}")
                except Exception as e:
                    logger.warning(f"[USB/IP Start] Failed to save SSH credential for {device_host}: {e}")

            with runtime.global_state.usbip_states_lock:
                runtime.global_state.usbip_states[device_host] = {
                    "connected": True,
                    "timestamp": time.time(),
                    "transport_connected": result["transport_connected"],
                    "adb_ready": result["adb_ready"],
                    "reconnecting": False,
                    "protocol_status": result.get("protocol_status") or {},
                }
            logger.info(f"[USB/IP Start] Set connected=True for device_host={device_host}")

            if device_list:
                existing_sources = {}
                with runtime.global_state.usbip_devices_source_lock:
                    existing_sources.update(runtime.global_state.usbip_devices_source)
                try:
                    runtime_sources = (
                        runtime.config_manager.get_runtime_config() or {}
                    ).get("usbip_devices_source") or {}
                    if isinstance(runtime_sources, dict):
                        existing_sources.update(runtime_sources)
                except Exception as e:
                    logger.warning("[USB/IP Start] Failed to read existing device sources: %s", e)

                source_updates = {}
                for device_id in device_list:
                    existing_source = str(
                        (existing_sources.get(device_id) or {}).get("source") or ""
                    ).strip()
                    if existing_source and existing_source != windows_device_host:
                        logger.info(
                            "[USB/IP Start] Keep existing source for %s: %s (new request: %s)",
                            device_id,
                            existing_source,
                            windows_device_host,
                        )
                        continue
                    source_updates[device_id] = {
                        "source": windows_device_host,
                        "timestamp": time.time(),
                    }

                with runtime.global_state.usbip_devices_source_lock:
                    runtime.global_state.usbip_devices_source.update(source_updates)
                logger.info(
                    "[USB/IP Start] Recorded device source: %s for devices: %s; skipped existing: %s",
                    windows_device_host,
                    sorted(source_updates),
                    sorted(set(device_list) - set(source_updates)),
                )

                # Persist USB/IP device sources to config
                try:
                    existing_runtime = runtime.config_manager.get_runtime_config()
                    usbip_sources = existing_runtime.get("usbip_devices_source", {})
                    usbip_sources.update(source_updates)
                    existing_runtime["usbip_devices_source"] = usbip_sources
                    if runtime.config_manager.save_runtime_config(existing_runtime):
                        logger.info(f"[USB/IP Start] Persisted device sources for {len(source_updates)} devices")
                except Exception as e:
                    logger.warning(f"[USB/IP Start] Failed to persist device sources: {e}")

        return JSONResponse(content=result)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error starting USB/IP: {e}")
        return error_response(
            str(e),
            status_code=500,
            **_usbip_error_fields(str(e)),
        )


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


# ==================== USB/IP Disconnect ====================

@router.post("/api/usbip/disconnect")
async def stop_usbip(
    request: Request,
    req: USBIPDisconnectRequest | None = Body(default=None),
    _elevated=Depends(require_elevated_admin),
):
    """Stop USB/IP forwarding (supports specifying host).

    Sensitive: disconnects/removes device forwarding, so requires admin elevation.
    """
    config = runtime.config_manager.load_config()
    client_id = runtime.get_client_id_from_request(request)
    tailscale_mode = False

    if req and req.worker_id:
        from features.cluster import get_cluster_service
        from features.cluster.api import _run_worker_command

        try:
            cluster = get_cluster_service()
        except (AttributeError, RuntimeError) as exc:
            logger.warning("[USB/IP Stop] cluster service unavailable: %s", exc)
            return error_response(
                "集群服务未初始化，无法执行远端 USB/IP 断开",
                status_code=503,
            )
        if req.worker_id != cluster.config.local_worker_id:
            # Cleanup must remain possible after cluster mode is disabled;
            # otherwise a persisted/physical remote attachment becomes
            # impossible to detach from the UI. New remote attaches still
            # require cluster mode in start_usbip().
            if not req.device_host or not req.busids:
                return error_response(
                    "远端 Worker 断开需要 device_host 和 busids", status_code=400
                )
            worker = cluster.repository.get_worker(req.worker_id) or {}
            if worker.get("status") not in {"online", "busy"}:
                return error_response("worker is not online", status_code=409)
            with _usbip_assignment_lock:
                assignments = _usbip_assignments()
                selected_assignments = []
                invalid_assignments = []
                for busid in req.busids:
                    current = assignments.get(
                        _usbip_assignment_key(req.device_host, busid)
                    ) or {}
                    if (
                        current.get("worker_id") != req.worker_id
                        or current.get("status") in {"attaching", "detaching"}
                    ):
                        invalid_assignments.append(busid)
                    else:
                        selected_assignments.append(current)
                disconnect_generation = _next_transport_generation(assignments)
                disconnect_operation_id = f"usbip-detach-{uuid.uuid4().hex}"
            if invalid_assignments:
                return error_response(
                    "USB/IP分配状态已变化，请刷新后重试: "
                    + ", ".join(invalid_assignments),
                    status_code=409,
                )
            claimed_serials = list(dict.fromkeys(
                str(serial or "").strip()
                for assignment in selected_assignments
                for serial in assignment.get("device_serials") or []
                if str(serial or "").strip()
            ))
            # Legacy/pending assignments may not yet have a BUSID→ADB serial
            # mapping. Do not claim every device on the Worker in that case:
            # those devices are unrelated to this physical USB/IP port and the
            # broad claim can block an otherwise safe cleanup detach.
            claim_source = ""
            if claimed_serials:
                operation_id = disconnect_operation_id
                claim_source = f"operation:{operation_id}"
                try:
                    claim_records = (
                        cluster.repository.acquire_device_operation_claim(
                            req.worker_id,
                            claimed_serials,
                            owner_id=_elevated.id,
                            source_type="cluster-usbip",
                            source_id=claim_source,
                            ttl_seconds=10 * 60,
                            username=_elevated.username,
                        )
                    )
                except ValueError as exc:
                    return error_response(str(exc), status_code=409)
                lease_tokens = cluster.repository.claim_fencing_tokens(
                    claim_records, operation_id
                )
            else:
                lease_tokens = []
            source_host = req.source_host or _usbip_remote_host(req.device_host)
            with _usbip_assignment_lock:
                assignments = _usbip_assignments()
                for busid in req.busids:
                    key = _usbip_assignment_key(req.device_host, busid)
                    current = assignments.get(key) or {}
                    if current.get("worker_id") == req.worker_id:
                        current.update({
                            "status": "detaching",
                            "generation": disconnect_generation,
                            "operation_id": disconnect_operation_id,
                            "timestamp": time.time(),
                        })
                        assignments[key] = current
                _save_usbip_assignments(assignments)
            command_payload = {
                "device_host": req.device_host,
                "source_host": source_host,
                "busids": req.busids,
                "devices": claimed_serials,
                "lease_tokens": lease_tokens,
                "claim_source_id": claim_source,
                "release_claim_on_terminal": bool(claim_source),
                "generation": disconnect_generation,
                "operation_id": disconnect_operation_id,
            }
            try:
                result = await _run_worker_command(
                    req.worker_id,
                    "usbip_detach",
                    command_payload,
                    timeout=90,
                )
            except HTTPException as exc:
                with _usbip_assignment_lock:
                    assignments = _usbip_assignments()
                    for busid in req.busids:
                        key = _usbip_assignment_key(req.device_host, busid)
                        current = assignments.get(key) or {}
                        if int(current.get("generation") or 0) == disconnect_generation:
                            current.update({"status": "unknown", "timestamp": time.time()})
                            assignments[key] = current
                    _save_usbip_assignments(assignments)
                if claim_source and exc.status_code != 504:
                    cluster.repository.claims.release(
                        claim_source, status="failed"
                    )
                raise
            except Exception:
                with _usbip_assignment_lock:
                    assignments = _usbip_assignments()
                    for busid in req.busids:
                        key = _usbip_assignment_key(req.device_host, busid)
                        current = assignments.get(key) or {}
                        if int(current.get("generation") or 0) == disconnect_generation:
                            current.update({"status": "unknown", "timestamp": time.time()})
                            assignments[key] = current
                    _save_usbip_assignments(assignments)
                if claim_source:
                    cluster.repository.claims.release(
                        claim_source, status="failed"
                    )
                raise
            already_detached = bool(result.get("already_detached"))
            if not result.get("detached_ports") and not already_detached:
                return error_response(
                    f"{req.worker_id} 未确认USB/IP设备已断开，保留分配记录",
                    status_code=502,
                )
            with _usbip_assignment_lock:
                assignments = _usbip_assignments()
                for busid in req.busids:
                    key = _usbip_assignment_key(req.device_host, busid)
                    current = assignments.get(key) or {}
                    if current.get("worker_id") == req.worker_id:
                        assignments.pop(key, None)
                _save_usbip_assignments(assignments)
            # The detach command already returned the Worker's settled device
            # list. Apply it immediately so the UI reflects the removal without
            # waiting for the next heartbeat (~15s lag).
            if "devices" in result:
                try:
                    cluster.repository.refresh_worker_devices(
                        req.worker_id, result.get("devices") or []
                    )
                except Exception as exc:
                    logger.warning(
                        "[USB/IP Stop] failed to refresh worker devices for %s: %s",
                        req.worker_id,
                        exc,
                    )
            return JSONResponse(content={
                "success": True,
                "device_host": req.device_host,
                "worker_id": req.worker_id,
                "busids": req.busids,
                "removed_devices": claimed_serials,
                "message": f"已从 {req.worker_id} 断开USB/IP设备",
                **result,
            })

    if req and req.device_host:
        config["device_host"] = req.device_host
    else:
        tunnel_host, tunnel_usbip_host = runtime.resolve_tailscale_device_host(request, client_id)
        if tunnel_host:
            config["device_host"] = tunnel_host
            config["usbip_attach_host"] = tunnel_usbip_host
            tailscale_mode = True
        else:
            config["device_host"] = _resolve_usbip_device_host(request, config)

    device_password = find_device_host_password(config["device_host"], config)
    if not device_password:
        device_password = config.get("device_pswd", "")
    if device_password:
        config["device_pswd"] = device_password

    devices_to_remove: list[str] = []
    usbip_attach_host = config.get("usbip_attach_host")
    ubuntu_detached_ports: list[str] = []
    remaining_devices_after_detach: list[str] = []
    claim_source_id = ""
    claim_records: list[dict] = []

    try:
        from features.devices.reconnect import (
            stop_usbip_reconnect_for_host,
            suppress_usbip_reconnect,
        )

        devices_to_remove = _usbip_devices_for_host(config["device_host"])
        if not devices_to_remove and device_lock_manager.get_all_locks():
            return JSONResponse(
                content={
                    "success": False,
                    "error": (
                        "USB/IP inventory is incomplete while device leases are active; "
                        "disconnect was refused"
                    ),
                },
                status_code=409,
            )
        claim_source_id, claim_records, conflict = acquire_device_operation_claim(
            request,
            devices_to_remove,
            "usbip-disconnect",
        )
        if conflict:
            return conflict
        suppress_usbip_reconnect(config["device_host"], devices_to_remove)
        stop_usbip_reconnect_for_host(config["device_host"], timeout=2)

        ubuntu_ssh = runtime.ssh_manager.get_connection(config)
        if ubuntu_ssh:
            try:
                detach_result = _detach_ubuntu_usbip_for_devices(
                    ubuntu_ssh,
                    device_host=config["device_host"],
                    usbip_attach_host=usbip_attach_host,
                    devices_to_remove=devices_to_remove,
                    busids=(req.busids if req else None),
                    detach_all=tailscale_mode,
                    settle=tailscale_mode,
                )
                ubuntu_detached_ports = list(detach_result["detached_ports"])
                remaining_devices_after_detach = list(detach_result["remaining_devices"])
                runtime.ssh_manager.return_connection(ubuntu_ssh)
            except Exception as e:
                ubuntu_ssh.close()
                logger.warning(f"[USB/IP Stop] detach Ubuntu usbip ports failed: {e}")

        if tailscale_mode:
            logger.info("[USB/IP Stop] Public mode keeps Windows usbipd bindings; only Ubuntu attach is detached")
            await asyncio.sleep(1)
            _clear_usbip_device_sources(config["device_host"], devices_to_remove)
        else:
            with DeviceSSHConnection(config) as win_ssh:
                selected_busids = list(req.busids) if req and req.busids else []
                if selected_busids:
                    for busid in selected_busids:
                        runtime.ssh_manager.execute_command(
                            win_ssh,
                            f"usbipd detach --busid {shlex.quote(busid)}",
                            timeout=10,
                        )
                else:
                    runtime.ssh_manager.execute_command(
                        win_ssh, "usbipd unbind --all", timeout=10
                    )
                await asyncio.sleep(2)

            _clear_usbip_device_sources(config["device_host"], devices_to_remove)
            if remaining_devices_after_detach:
                verification_ssh = runtime.ssh_manager.get_connection(config)
                if verification_ssh:
                    try:
                        remaining_devices_after_detach = sorted(
                            _wait_for_adb_devices_removed(
                                verification_ssh,
                                set(remaining_devices_after_detach),
                            )
                        )
                    finally:
                        runtime.ssh_manager.return_connection(verification_ssh)

        with runtime.global_state.usbip_states_lock:
            runtime.global_state.usbip_states[config["device_host"]] = {
                "connected": False,
                "timestamp": time.time(),
                "transport_connected": False,
                "adb_ready": False,
                "reconnecting": False,
                "protocol_status": {},
            }

        # 失效缓存，避免返回已断开的 USB/IP 设备。
        _invalidate_device_cache()

        disconnected_devices_info = format_device_list_info(devices_to_remove)
        logger.info(f"[USB/IP Stop] Connection cleared for {config['device_host']}, removed {len(devices_to_remove)} devices{disconnected_devices_info}")
        if remaining_devices_after_detach:
            logger.warning(
                "[USB/IP Stop] Devices still visible after detach cleanup: %s",
                remaining_devices_after_detach,
            )

        if req and req.worker_id and req.busids:
            with _usbip_assignment_lock:
                assignments = _usbip_assignments()
                for busid in req.busids:
                    key = _usbip_assignment_key(config["device_host"], busid)
                    current = assignments.get(key) or {}
                    if current.get("worker_id") == req.worker_id:
                        assignments.pop(key, None)
                _save_usbip_assignments(assignments)

        await notify_device_change(devices_to_remove, "USB/IP Stop")

        response = JSONResponse(content={
            "success": True,
            "message": f"Local devices disconnected{disconnected_devices_info}",
            "detached_ports": ubuntu_detached_ports,
            "removed_devices": devices_to_remove,
            "remaining_devices": remaining_devices_after_detach,
        })
        audit_device_operation(
            request,
            "usbip-disconnect",
            claim_records,
            response.status_code,
        )
        return response

    except HTTPException:
        # Windows 不可连接时仅清理连接和设备来源状态。
        if not devices_to_remove:
            devices_to_remove = _usbip_devices_for_host(config["device_host"])
        try:
            from features.devices.reconnect import suppress_usbip_reconnect
            suppress_usbip_reconnect(config["device_host"], devices_to_remove)
        except Exception:
            pass
        _clear_usbip_device_sources(config["device_host"], devices_to_remove)

        with runtime.global_state.usbip_states_lock:
            runtime.global_state.usbip_states[config["device_host"]] = {
                "connected": False,
                "timestamp": time.time(),
                "transport_connected": False,
                "adb_ready": False,
                "reconnecting": False,
                "protocol_status": {},
            }

        # 失效缓存，避免返回已断开的 USB/IP 设备。
        _invalidate_device_cache()

        disconnected_devices_info = format_device_list_info(devices_to_remove)
        logger.info(f"[USB/IP Stop] Connection cleared for {config['device_host']}, removed {len(devices_to_remove)} devices{disconnected_devices_info}")

        await notify_device_change(devices_to_remove, "USB/IP Stop")

        response = JSONResponse(content={
            "success": True,
            "message": f"Local devices disconnected{disconnected_devices_info}",
            "removed_devices": devices_to_remove,
        })
        audit_device_operation(
            request,
            "usbip-disconnect",
            claim_records,
            response.status_code,
        )
        return response
    except Exception as exc:
        if claim_records:
            audit_device_operation(
                request,
                "usbip-disconnect",
                claim_records,
                500,
                error=str(exc),
            )
        raise
    finally:
        release_device_operation_claim(claim_source_id)
