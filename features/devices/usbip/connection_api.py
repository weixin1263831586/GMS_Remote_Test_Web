"""USB/IP connection and disconnection routes; shared lifecycle and authorization."""

from __future__ import annotations

import asyncio
import logging
import shlex
import time
import uuid

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from features.auth import require_elevated_admin
from foundation.error_model import record_internal_error
from foundation.responses import error_response

from .. import runtime
from ..adb.api import (
    start_adb_forward as start_adb_forward,
)
from ..adb.api import (
    stop_adb_forward as stop_adb_forward,
)
from ..locks import device_lock_manager
from ..models import USBIPDisconnectRequest, USBIPStartRequest
from ..support import (
    DeviceSSHConnection,
    acquire_device_operation_claim,
    audit_device_operation,
    format_device_list_info,
    notify_device_change,
    release_device_operation_claim,
)
from .access import enforce_usbip_host_access, usbip_request_user
from .assignments import (
    load_usbip_assignments as _usbip_assignments,
)
from .assignments import (
    mark_usbip_detach_unknown as _mark_usbip_detach_unknown,
)
from .assignments import (
    next_transport_generation as _next_transport_generation,
)
from .assignments import (
    prune_stale_unknown_usbip_assignments as _prune_stale_unknown_usbip_assignments,
)
from .assignments import (
    save_usbip_assignments as _save_usbip_assignments,
)
from .assignments import (
    usbip_assignment_key as _usbip_assignment_key,
)
from .linux_source import stop_ubuntu_usbip_server
from .manager import detach_ubuntu_usbip_ports, find_device_host_password, usbip_manager
from .operations import (
    has_remaining_usbip_assignments,
    selected_usbip_serials,
    serialize_usbip_operation,
)
from .operations import (
    usbip_error_fields as _usbip_error_fields,
)
from .persistence import (
    persist_local_usbip_sources as _persist_local_usbip_sources,
)
from .persistence import (
    record_usbip_network_quality as _record_usbip_network_quality,
)
from .persistence import (
    record_usbip_source_os as _record_usbip_source_os,
)
from .persistence import update_usbip_source_records
from .persistence import (
    usbip_assignment_lock as _usbip_assignment_lock,
)
from .support import _USBIP_ATTACHING_STALE_SECONDS as _USBIP_ATTACHING_STALE_SECONDS
from .support import _adb_proxy_target_assignments as _adb_proxy_target_assignments
from .support import _clear_usbip_device_sources as _clear_usbip_device_sources
from .support import _detach_ubuntu_usbip_for_devices as _detach_ubuntu_usbip_for_devices
from .support import _invalidate_device_cache as _invalidate_device_cache
from .support import _local_worker_id as _local_worker_id
from .support import _mark_usbip_source_disconnected as _mark_usbip_source_disconnected
from .support import _resolve_usbip_device_host as _resolve_usbip_device_host
from .support import _usbip_devices_for_host as _usbip_devices_for_host
from .support import _usbip_remote_host as _usbip_remote_host
from .support import _wait_for_adb_devices_removed as _wait_for_adb_devices_removed


logger = logging.getLogger(__name__)


router = APIRouter()


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

def _is_usbip_recoverable_attach_error(exc: Exception) -> bool:
    detail = str(getattr(exc, "detail", "") or exc).lower()
    return (
        "busy (exported)" in detail
        or "残留usb/ip会话占用" in detail
        or "device in error state" in detail
        or "usbip_attach_unstable" in detail
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




# ==================== USB/IP Connect ====================

@router.post("/api/usbip/connect")
@serialize_usbip_operation
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
        adb_proxy_routes: list[dict] = []
        proxy_serials: set[str] = set()
        source_devices: dict[str, str] = {}
        unknown_busids: list[str] = []
        if worker_id:
            from foundation.cluster_port import get_cluster_service
            from foundation.cluster_port import require_cluster_enabled as _require_cluster_enabled
            from foundation.cluster_port import run_worker_command as _run_worker_command

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
                if source_inventory.get("success"):
                    # Windows can allocate a new BUSID after unplug/replug or
                    # protocol re-enumeration.  An old degraded assignment
                    # must not block the currently connected BUSID forever.
                    stale_keys = _prune_stale_unknown_usbip_assignments(
                        device_host,
                        set(source_devices),
                    )
                    if stale_keys:
                        logger.info(
                            "[USB/IP] Removed stale BUSID assignments: %s",
                            ", ".join(stale_keys),
                        )
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
                        "该来源主机仍有其他USB/IP设备处于活动状态；为避免停止全局ADB或重启USB/IP导出影响现有任务，本次接入已拒绝",
                        status_code=409,
                        error_code="USBIP_SOURCE_ADB_IN_USE",
                        remediation="请先断开该来源上的其他USB/IP分配，或将新设备接入其他来源主机。",
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
                        # Ubuntu 来源的 usbipd 导出进程按需启动；Windows
                        # usbipd 常驻服务不可达才是网络问题。先尝试启动
                        # 来源侧导出进程并重试一次探测。
                        prep = await asyncio.to_thread(
                            usbip_manager.ensure_source_export_ready,
                            device_host,
                            busids,
                            device_password,
                        )
                        if not prep.get("success"):
                            logger.warning(
                                "[USB/IP] Source export ensure failed for %s: %s",
                                device_host,
                                prep.get("detail") or "unknown error",
                            )
                        if prep.get("started"):
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
                            export_detail = str(
                                prep.get("detail") or prep.get("error") or ""
                            ).strip()
                            if not prep.get("success") and export_detail:
                                # 来源侧导出进程启动/校验失败（版本过低、未安装、
                                # 启动失败）时 TCP 预检必然失败；返回真实原因，
                                # 而不是误导性的防火墙/路由建议。
                                error_fields = {
                                    "error_code": "USBIP_TCP_UNREACHABLE",
                                    "retryable": True,
                                    "network_quality": network_quality,
                                }
                                if prep.get("install_guide"):
                                    error_fields["install_guide"] = str(
                                        prep["install_guide"]
                                    )
                                return error_response(
                                    f"{worker_id} 无法连接USB/IP来源TCP 3240：{export_detail}",
                                    status_code=409,
                                    **error_fields,
                                )
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
                    if prepared.get("source_os"):
                        _record_usbip_source_os(device_host, str(prepared["source_os"]))
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
                            "source_os": prepared.get("source_os") or "windows",
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
                            "source_os": result.get("source_os") or "windows",
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
            if result.get("source_os"):
                _record_usbip_source_os(device_host, str(result["source_os"]))
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
                _persist_local_usbip_sources(
                    device_host,
                    reported_serials,
                    source_os=result.get("source_os") or "",
                )

            if request_data.get("manual_connect"):
                try:
                    from features.devices.reconnect import (
                        clear_usbip_reconnect_suppression,
                        resume_usbip_reconnect,
                    )

                    clear_usbip_reconnect_suppression(
                        device_host, reported_serials
                    )
                    # 对称 resume：pause 按 host + device_ids 双键记录，
                    # 这里手动重连成功后必须同时清 host 键，否则 firmware
                    # ownership 的 host 级 pause 会拦住后续 reconnect 调度。
                    resume_usbip_reconnect(
                        device_host=device_host, device_ids=reported_serials
                    )
                except Exception as e:
                    logger.warning(
                        "[USB/IP] Failed to clear reconnect suppression/pause "
                        "for devices %s: %s",
                        reported_serials,
                        e,
                    )

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
                        "source_os": result.get("source_os") or "windows",
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

                try:
                    if update_usbip_source_records(updates=source_updates):
                        logger.info(f"[USB/IP Start] Persisted device sources for {len(source_updates)} devices")
                except Exception as e:
                    logger.warning(f"[USB/IP Start] Failed to persist device sources: {e}")

        return JSONResponse(content=result)

    except HTTPException:
        raise
    except Exception:
        # 未知异常的 str(e) 可能内嵌主机名/SSH 命令/路径，不得回显客户端；
        # 操作性失败（凭据缺失、usbipd 未装等）已由上方 manager result 分支
        # 携带策划文案返回，走到这里的都是意外错误。
        message = record_internal_error(logger, "启动 USB/IP", "Error starting USB/IP")
        return error_response(message, status_code=500)


@router.post("/api/usbip/disconnect")
@serialize_usbip_operation
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
        from foundation.cluster_port import get_cluster_service
        from foundation.cluster_port import run_worker_command as _run_worker_command

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
                            # ADR 0010: canonical resource-owner accessor.
                            owner_id=_elevated.resource_owner_id,
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
                _mark_usbip_detach_unknown(
                    req.device_host, req.busids, req.worker_id,
                    disconnect_generation,
                )
                if claim_source and exc.status_code != 504:
                    cluster.repository.claims.release(
                        claim_source, status="failed"
                    )
                raise
            except Exception:
                _mark_usbip_detach_unknown(
                    req.device_host, req.busids, req.worker_id,
                    disconnect_generation,
                )
                if claim_source:
                    cluster.repository.claims.release(
                        claim_source, status="failed"
                    )
                raise
            already_detached = bool(result.get("already_detached"))
            if not result.get("detached_ports") and not already_detached:
                _mark_usbip_detach_unknown(
                    req.device_host, req.busids, req.worker_id,
                    disconnect_generation,
                )
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
    has_remaining_assignments = False

    try:
        from features.devices.reconnect import (
            stop_usbip_reconnect_for_host,
            suppress_usbip_reconnect,
        )

        devices_to_remove = _usbip_devices_for_host(config["device_host"])
        selected_busids = list(req.busids) if req and req.busids else []
        assignments_before_disconnect = _usbip_assignments()
        if selected_busids:
            devices_to_remove = selected_usbip_serials(
                assignments_before_disconnect,
                config["device_host"],
                selected_busids,
            )
        has_remaining_assignments = has_remaining_usbip_assignments(
            assignments_before_disconnect,
            config["device_host"],
            selected_busids,
        ) if selected_busids else False
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
                # 同步 SSH 往返 + settle sleep，放线程池避免冻结事件循环
                # （同函数下方 _detach_source_bindings 同理）。
                detach_result = await asyncio.to_thread(
                    _detach_ubuntu_usbip_for_devices,
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
            logger.info("[USB/IP Stop] Public mode keeps source-side usbipd bindings; only local attach is detached")
            await asyncio.sleep(1)
            _clear_usbip_device_sources(config["device_host"], devices_to_remove)
        else:
            # usbipd detach/unbind 是同步 SSH 调用，放线程池避免
            # 冻结事件循环（DeviceSSHConnection 的建立本身也是阻塞的）。
            def _detach_source_bindings():
                with DeviceSSHConnection(config) as source_ssh:
                    if source_ssh is None:
                        return
                    if usbip_manager._detect_source_os(source_ssh) == "linux":
                        if selected_busids and has_remaining_assignments:
                            logger.info(
                                "[USB/IP Stop] Ubuntu source %s still exports devices for other assignments; usbipd kept running",
                                config["device_host"],
                            )
                            return
                        stop_result = stop_ubuntu_usbip_server(
                            runtime.ssh_manager, source_ssh,
                        )
                        if not stop_result.get("success"):
                            logger.warning(
                                "[USB/IP Stop] Failed to stop Ubuntu usbipd on %s: %s",
                                config["device_host"],
                                stop_result.get("detail"),
                            )
                        return
                    if selected_busids:
                        for busid in selected_busids:
                            runtime.ssh_manager.execute_command(
                                source_ssh,
                                f"usbipd detach --busid {shlex.quote(busid)}",
                                timeout=10,
                            )
                    else:
                        runtime.ssh_manager.execute_command(
                            source_ssh, "usbipd unbind --all", timeout=10
                        )

            await asyncio.to_thread(_detach_source_bindings)
            await asyncio.sleep(2)

            _clear_usbip_device_sources(config["device_host"], devices_to_remove)
            if remaining_devices_after_detach:
                verification_ssh = runtime.ssh_manager.get_connection(config)
                if verification_ssh:
                    try:
                        remaining_devices_after_detach = sorted(
                            # ADB 轮询等待含 sleep 循环 + SSH 往返，放线程池。
                            await asyncio.to_thread(
                                _wait_for_adb_devices_removed,
                                verification_ssh,
                                set(remaining_devices_after_detach),
                            )
                        )
                    finally:
                        runtime.ssh_manager.return_connection(verification_ssh)

        _mark_usbip_source_disconnected(
            config["device_host"],
            has_remaining_assignments=has_remaining_assignments,
        )

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

        _mark_usbip_source_disconnected(
            config["device_host"],
            has_remaining_assignments=has_remaining_assignments,
        )

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


__all__ = [
    "_attached_usbip_serials",
    "_is_usbip_export_busy",
    "_is_usbip_recoverable_attach_error",
    "_preserve_usbip_assignment_after_error",
    "_rollback_local_usbip_attach",
    "_usbip_worker_command_timeout",
    "start_usbip",
    "stop_usbip",
]
