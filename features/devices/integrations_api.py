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

__all__ = ["install_usbipd", "router"]


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
        runtime_config = runtime.config_manager.get_runtime_config() or {}
        runtime_config["usbip_cluster_assignments"] = assignments
        saved = runtime.config_manager.save_runtime_config(runtime_config)
    if not saved:
        raise RuntimeError("无法保存USB/IP集群分配状态")


def _usbip_assignment_key(device_host: str, busid: str) -> str:
    return f"{device_host}|{busid}"


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
        return error_response(result.get("error", "USB设备枚举失败"), status_code=500)
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
        grouped: dict[tuple[str, str], list[str]] = {}
        for item in assignments:
            group = (
                str(item.get("worker_id") or ""),
                str(item.get("source_host") or ""),
            )
            grouped.setdefault(group, []).append(str(item.get("busid") or ""))
        cluster_selections = [
            {
                "device_host": client_id,
                "source_host": source_host,
                "worker_id": worker_id,
                "busids": sorted(filter(None, busids)),
            }
            for (worker_id, source_host), busids in grouped.items()
        ]

    logger.info(f"[USB/IP Status] device_host={client_id}, connected={connected}, device_count={len(runtime.global_state.usbip_devices_source)}")
    return JSONResponse(content={
        "connected": connected,
        "device_host": client_id,
        "device_count": len(runtime.global_state.usbip_devices_source),
        "transport_connected": bool(state_info.get("transport_connected", False)),
        "adb_ready": bool(state_info.get("adb_ready", False)),
        "reconnecting": bool(state_info.get("reconnecting", False)),
        "protocol_status": state_info.get("protocol_status") or {},
        "cluster_selection": cluster_selections[0] if cluster_selections else None,
        "cluster_selections": cluster_selections,
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
        if worker_id:
            from features.cluster import get_cluster_service
            from features.cluster.api import _require_cluster_enabled, _run_worker_command

            cluster = get_cluster_service()
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
                        return error_response(
                            f"USB设备已接入其他Worker: {', '.join(conflicts)}",
                            status_code=409,
                        )
                    for busid in busids:
                        assignments[_usbip_assignment_key(device_host, busid)] = {
                            "device_host": device_host,
                            "source_host": "",
                            "worker_id": worker_id,
                            "busid": busid,
                            "status": "attaching",
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
                        "source_host": prepared["source_host"],
                        "busids": prepared["busids"],
                    }
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
                    attached_serials = list(dict.fromkeys(
                        str(item or "").strip()
                        for item in (
                            result.get("new_devices")
                            or [
                                device.get("serial")
                                for device in result.get("devices") or []
                            ]
                        )
                        if str(item or "").strip()
                    ))
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
                        assignments[_usbip_assignment_key(device_host, busid)] = {
                            "device_host": device_host,
                            "source_host": prepared["source_host"],
                            "worker_id": worker_id,
                            "busid": busid,
                            "device_serials": attached_serials,
                            "status": "attached",
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
                    "success": True,
                    "device_host": device_host,
                    "worker_id": worker_id,
                    "source_host": prepared["source_host"],
                    "busids": prepared["busids"],
                    "transport_connected": bool(result.get("attached_busids")),
                    "adb_ready": bool(result.get("devices")),
                    "device_list": [
                        item.get("serial")
                        for item in result.get("devices") or []
                        if item.get("serial")
                    ],
                    "message": f"USB/IP设备已接入 {worker_id}",
                    **result,
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
                return error_response(
                    f"USB设备已接入其他Worker: {', '.join(conflicts)}",
                    status_code=409,
                )

        start_kwargs = {"usbip_attach_host": usbip_attach_host}
        if busids:
            start_kwargs["selected_busids"] = busids
        result = await asyncio.to_thread(
            usbip_manager.start_usbip,
            device_host,
            device_password,
            **start_kwargs,
        )
        result["device_host"] = device_host

        if result.get("success"):
            if worker_id and busids:
                with _usbip_assignment_lock:
                    assignments = _usbip_assignments()
                    for busid in busids:
                        assignments[_usbip_assignment_key(device_host, busid)] = {
                            "device_host": device_host,
                            "source_host": _usbip_remote_host(
                                device_host, usbip_attach_host
                            ),
                            "worker_id": worker_id,
                            "busid": busid,
                            "status": "attached",
                            "timestamp": time.time(),
                        }
                    _save_usbip_assignments(assignments)
            device_list = result.get("device_list", [])
            result["transport_connected"] = bool(result.get("transport_connected") or result.get("devices"))
            result["adb_ready"] = bool(device_list)
            if not device_list:
                result.setdefault(
                    "message",
                    "USB/IP传输已连接，设备可能处于 reboot/fastboot/recovery 或 ADB 尚未枚举完成",
                )

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
        return error_response(str(e), status_code=500)


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
        from features.cluster.api import _require_cluster_enabled, _run_worker_command

        cluster = get_cluster_service()
        if req.worker_id != cluster.config.local_worker_id:
            _require_cluster_enabled(remote=True)
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
                        or current.get("status") == "attaching"
                    ):
                        invalid_assignments.append(busid)
                    else:
                        selected_assignments.append(current)
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
            if not claimed_serials:
                # Older assignments do not carry a BUSID→serial mapping. Claim
                # the Worker's visible inventory conservatively so a concurrent
                # test cannot start while the physical detach is in flight.
                claimed_serials = list(dict.fromkeys(
                    [
                        str(device.get("serial") or "").strip()
                        for device in cluster.repository.list_devices(
                            req.worker_id
                        )
                        if str(device.get("serial") or "").strip()
                    ]
                    + [
                        str(claim.get("serial") or "").strip()
                        for claim in cluster.repository.claims.list_active(
                            worker_id=req.worker_id
                        )
                        if str(claim.get("serial") or "").strip()
                    ]
                ))
            claim_source = ""
            if claimed_serials:
                operation_id = f"usbip-detach-{uuid.uuid4().hex}"
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
            command_payload = {
                "source_host": source_host,
                "busids": req.busids,
                "devices": claimed_serials,
                "lease_tokens": lease_tokens,
                "claim_source_id": claim_source,
                "release_claim_on_terminal": bool(claim_source),
            }
            try:
                result = await _run_worker_command(
                    req.worker_id,
                    "usbip_detach",
                    command_payload,
                    timeout=90,
                )
            except HTTPException as exc:
                if claim_source and exc.status_code != 504:
                    cluster.repository.claims.release(
                        claim_source, status="failed"
                    )
                raise
            except Exception:
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
                            f"usbipd unbind --busid {shlex.quote(busid)}",
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
