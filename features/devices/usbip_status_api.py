"""USB/IP status route group.

从 integrations_api.py 拆出（2026-09 大文件收敛）。
包含本地传输验证 helper 与 source-os / source-devices /
assignments / status 四个只读路由。"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from features.auth import require_elevated_admin
from foundation.responses import error_response

from . import reconnect, runtime
from .adb_forward_api import (
    start_adb_forward as start_adb_forward,
)
from .adb_forward_api import (
    stop_adb_forward as stop_adb_forward,
)
from .manager import device_manager
from .usbip import usbip_manager
from .usbip_access import enforce_usbip_host_access
from .usbip_assignments import (
    load_usbip_assignments as _usbip_assignments,
)
from .usbip_assignments import (
    reconcile_usbip_assignment_serials as _reconcile_usbip_assignment_serials,
)
from .usbip_assignments import (
    save_usbip_assignments as _save_usbip_assignments,
)
from .usbip_operations import (
    usbip_error_fields as _usbip_error_fields,
)
from .usbip_persistence import (
    lookup_usbip_source_os as _lookup_usbip_source_os,
)
from .usbip_persistence import (
    record_usbip_source_os as _record_usbip_source_os,
)
from .usbip_persistence import (
    usbip_assignment_lock as _usbip_assignment_lock,
)
from .usbip_support import _local_worker_id as _local_worker_id
from .usbip_support import _resolve_usbip_device_host as _resolve_usbip_device_host
from .usbip_transport_probe import probe_existing_local_usbip_transport


logger = logging.getLogger(__name__)


router = APIRouter()

def _verify_local_usbip_transport(
    assignments: dict[str, dict],
) -> dict[str, dict[str, object]]:
    """Cross-check persisted assignments against ``usbip port``.

    持久化 assignment 是"已记录分配"，与本机实际 attach 状态可能脱节
    （Controller crash、worker reboot、手工 usbip detach）。本函数用
    ``usbip port`` 的实时输出对本地 Worker 名下的分配逐条核对，返回
    ``{(device_host, busid): {"transport_state": ..., "checked_at": ...}}``；
    远端 Worker 的分配不核对（transport_state 留空），由既有
    schedule_remote_usbip_verify 后台机制负责。

    host identity：``usbip attach`` 用的是 source/attach 地址（可能是
    Tailscale IP 等非 SSH 地址），``usbip port`` 显示的 host 是 attach 时
    的 remote host。因此核对必须用 assignment 的 ``source_host``（优先）
    或归一化后的 ``device_host``，直接拿 SSH 形式的 device_host 会在
    双地址主机上误报 detached。
    """
    from .usbip_transaction import USBIP_PORT_COMMAND, parse_usbip_port_entries

    local_worker_id = _local_worker_id()
    targets: dict[tuple[str, str, str], tuple[str, str]] = {}
    for item in assignments.values():
        if not isinstance(item, dict):
            continue
        if str(item.get("worker_id") or "") != local_worker_id:
            continue
        if str(item.get("status") or "") not in {
            "attaching", "attached", "unknown", "cleanup_required",
        }:
            continue
        device_host = str(item.get("device_host") or "").strip()
        busid = str(item.get("busid") or "").strip()
        if not device_host or not busid:
            continue
        device_key = device_host.split("@", 1)[-1]
        # usbip attach 的 remote host：source_host（如 Tailscale attach
        # 地址）优先，否则用去用户名的 device_host。
        source_host = str(item.get("source_host") or "").strip().split("@", 1)[-1]
        verify_hosts = {
            host for host in (source_host, device_key) if host
        }
        targets[(device_host, busid)] = (frozenset(verify_hosts), busid)
    if not targets:
        return {}

    config = runtime.config_manager.load_config()
    ssh = runtime.ssh_manager.get_connection(config)
    if not ssh:
        return {}
    try:
        port_result = runtime.ssh_manager.execute_command(
            ssh, USBIP_PORT_COMMAND, timeout=10,
        )
        if not port_result.ok:
            return {}
        attached = {
            (str(entry.get("host") or "").strip(), str(entry.get("busid") or "").strip())
            for entry in parse_usbip_port_entries(port_result.stdout or "")
        }
    except Exception as exc:
        logger.warning(
            "[USB/IP Assignments] transport verification failed: %s", exc,
        )
        return {}
    finally:
        runtime.ssh_manager.return_connection(ssh)

    checked_at = time.time()
    result: dict[str, dict[str, object]] = {}
    for (device_host, busid), (verify_hosts, _) in targets.items():
        attached_now = any((host, busid) in attached for host in verify_hosts)
        result[f"{device_host}|{busid}"] = {
            "transport_state": "attached" if attached_now else "detached",
            "checked_at": checked_at,
        }
    return result

# ==================== USB/IP Status ====================

async def _retire_assignments_for_physically_local_devices(
    device_host: str,
    local_assignments: list[dict],
    expected_devices: set[str],
    physically_local: dict[str, str],
) -> tuple[dict, bool]:
    """清除已被物理挪插到本机的设备的过期 USB/IP 分配。

    只移除"序列号全部物理直连在本机"的分配；同一源主机上其他仍有效
    的分配保持不动。内存中的设备来源记录（含 usbip_manager 缓存）一并
    清理，让固件烧写路由立即回落到本地传统链路，无需重启服务。
    """
    retired_serials: set[str] = set()
    stale_busids: list[str] = []
    for item in local_assignments:
        serials = {
            str(serial or "").strip()
            for serial in item.get("device_serials") or []
            if str(serial or "").strip()
        }
        if serials and all(
            physically_local.get(serial) == "physical" for serial in serials
        ):
            stale_busids.append(str(item.get("busid") or "").strip())
            retired_serials |= serials

    if stale_busids:
        # 读改写点须持锁（与其他 assignment 写点一致）；save 契约是
        # "失败抛 RuntimeError"，这里降级为记录后继续（fail-open）。
        with _usbip_assignment_lock:
            assignments_map = _usbip_assignments()
            for busid in stale_busids:
                assignments_map.pop(f"{device_host}|{busid}", None)
            try:
                _save_usbip_assignments(assignments_map)
            except RuntimeError:
                logger.error("[USB/IP Status] failed to persist removal of stale assignments for %s: %s", device_host, stale_busids)

    with runtime.global_state.usbip_devices_source_lock:
        for serial in retired_serials:
            runtime.global_state.usbip_devices_source.pop(serial, None)
    for serial in retired_serials:
        with contextlib.suppress(Exception):
            usbip_manager.device_sources.pop(serial, None)

    retired_state = {
        "connected": False,
        "timestamp": time.time(),
        "transport_connected": False,
        "adb_ready": False,
        "reconnecting": False,
        "expected_devices": sorted(expected_devices),
        "reason": (
            "devices physically attached to the controller; stale USB/IP "
            f"assignment retired ({device_host}|{'/'.join(stale_busids)})"
        ),
        "protocol_status": {"mode": "disconnected"},
    }
    with runtime.global_state.usbip_states_lock:
        runtime.global_state.usbip_states[device_host] = retired_state
    logger.warning(
        "[USB/IP Status] stale assignment for %s retired: devices %s are "
        "physically attached to the controller (non-vhci); removed "
        "assignments %s",
        device_host,
        sorted(retired_serials),
        [f"{device_host}|{busid}" for busid in stale_busids],
    )
    return retired_state, False

async def _reconcile_local_usbip_status(
    device_host: str,
    assignments: list[dict],
    state_info: dict,
    config: dict,
) -> tuple[dict, bool]:
    """Replace persisted local attach claims with the exact current transport."""
    local_worker_id = _local_worker_id()
    local_assignments = [
        item for item in assignments
        if str(item.get("worker_id") or "") == local_worker_id
        and str(item.get("status") or "") in {
            "attaching", "attached", "unknown", "cleanup_required",
        }
    ]
    if not local_assignments:
        return dict(state_info), False

    expected_devices = {
        str(serial or "").strip()
        for item in local_assignments
        for serial in item.get("device_serials") or []
        if str(serial or "").strip()
    }
    observed = await asyncio.to_thread(
        probe_existing_local_usbip_transport,
        device_host,
        expected_devices,
        config,
        local_worker_id=local_worker_id,
    )
    if observed is not None:
        device_list = list(observed.get("device_list") or [])
        refreshed = {
            "connected": True,
            "timestamp": time.time(),
            "transport_connected": True,
            "adb_ready": bool(device_list),
            "reconnecting": False,
            "protocol_status": observed.get("protocol_status") or {},
        }
        with runtime.global_state.usbip_states_lock:
            runtime.global_state.usbip_states[device_host] = refreshed
        return refreshed, False

    # 期望设备全部物理直连在本机（sysfs realpath 不含 vhci）时，说明
    # USB/IP 分配已过期（设备从源主机挪插到了 Controller）：清除持久化
    # 分配与内存来源记录，状态置为最终 disconnected，不再调度重连，
    # 避免固件烧写被误路由到 Windows Source Agent。
    physically_local = await asyncio.to_thread(
        reconnect.classify_local_usb_attachment,
        expected_devices,
    )
    if expected_devices and all(
        physically_local.get(serial) == "physical"
        for serial in expected_devices
    ):
        return await _retire_assignments_for_physically_local_devices(
            device_host,
            local_assignments,
            expected_devices,
            physically_local,
        )

    stale = {
        "connected": True,
        "timestamp": time.time(),
        "transport_connected": False,
        "adb_ready": False,
        "reconnecting": True,
        "expected_devices": sorted(expected_devices),
        "reason": "persisted USB/IP assignment missing from local usbip port",
        "protocol_status": {"mode": "reconnecting"},
    }
    with runtime.global_state.usbip_states_lock:
        runtime.global_state.usbip_states[device_host] = stale
    with runtime.global_state.device_cache_lock:
        runtime.global_state.device_cache = {"devices": [], "timestamp": 0}

    if expected_devices:
        reconnect.schedule_usbip_reconnect(
            device_host,
            reason="USB/IP status detected stale local attached assignment",
            expected_devices=sorted(expected_devices),
        )
    logger.warning(
        "[USB/IP Status] stale local assignment detected for %s; "
        "expected_devices=%s",
        device_host,
        sorted(expected_devices),
    )
    return stale, True

@router.get("/api/usbip/source-os")
async def get_usbip_source_os(
    request: Request,
    hosts: str = "",
    _elevated=Depends(require_elevated_admin),
):
    """Resolve Windows/Ubuntu labels for source hosts.

    先读持久化缓存（无网络开销），未知来源并行 SSH 探测后写回缓存，
    供"设备来源"下拉框在未选择主机时即可显示系统标识。
    """
    requested = list(dict.fromkeys(
        str(item or "").strip()
        for item in (hosts or "").split(",")
        if str(item or "").strip()
    ))
    if not requested:
        config = runtime.config_manager.load_config()
        resolved = _resolve_usbip_device_host(request, config)
        requested = [resolved] if resolved else []

    results: dict[str, dict] = {}
    to_probe: list[str] = []
    for host in requested:
        enforce_usbip_host_access(request, host, host)
        cached_os = _lookup_usbip_source_os(host)
        if cached_os:
            results[host] = {
                "source_os": "windows" if cached_os == "windows" else "ubuntu",
                "probed": False,
                "error": "",
            }
        else:
            to_probe.append(host)

    async def _probe_one(host: str) -> tuple[str, dict]:
        probe = await asyncio.to_thread(usbip_manager.probe_source_os, host)
        source_os = str(probe.get("source_os") or "").strip()
        if source_os:
            _record_usbip_source_os(host, source_os)
            return host, {
                "source_os": (
                    "windows" if source_os == "windows" else "ubuntu"
                ),
                "probed": True,
                "error": "",
            }
        return host, {
            "source_os": "",
            "probed": True,
            "error": str(probe.get("error") or ""),
        }

    if to_probe:
        probed = await asyncio.gather(*(_probe_one(host) for host in to_probe))
        results.update(dict(probed))

    return JSONResponse(content={
        "success": True,
        "sources": {host: results.get(host, {"source_os": "", "error": ""}) for host in requested},
    })

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
        error_fields = _usbip_error_fields(message)
        if result.get("install_guide"):
            error_fields["install_guide"] = str(result["install_guide"])
        return error_response(
            message,
            status_code=500,
            **error_fields,
        )
    _reconcile_usbip_assignment_serials(
        resolved,
        result.get("devices") or [],
        source_os=result.get("source_os") or "",
    )
    if result.get("source_os"):
        _record_usbip_source_os(resolved, str(result["source_os"]))
    return JSONResponse(content=result)

@router.get("/api/usbip/assignments")
async def list_usbip_assignments(
    verify: bool = False,
    _elevated=Depends(require_elevated_admin),
):
    """List all USB/IP assignments grouped by source host (read-only).

    供接入弹框"显示全部"视图使用：一次返回所有来源主机的当前接入
    （按 device_host + worker 分组），纯读持久化分配，不做 SSH 枚举，
    可被弹框轮询安全调用。断开仍走既有 /api/usbip/disconnect。

    ``?verify=true``：对本地 Worker 名下的分配额外执行一次
    ``usbip port`` 实时核对，响应中带 ``transport_state_by_busid``
    （attached/detached/unknown）与 ``verified: true``。持久化分配
    表示"已记录分配"，实时核对才是"当前已连接"；两者可能因
    Controller/Worker 重启或手工 detach 而短暂不一致。
    """
    grouped: dict[tuple[str, str, str], dict[str, object]] = {}
    for item in _usbip_assignments().values():
        if not isinstance(item, dict):
            continue
        device_host = str(item.get("device_host") or "").strip()
        if not device_host:
            continue
        key = (
            device_host,
            str(item.get("worker_id") or ""),
            str(item.get("source_host") or ""),
        )
        group = grouped.setdefault(key, {
            "device_host": device_host,
            "worker_id": key[1],
            "source_host": key[2],
            "busids": [],
            "device_serials": [],
            "device_serials_by_busid": {},
            "statuses_by_busid": {},
            "generations_by_busid": {},
            "network_quality_by_busid": {},
            "source_os": "",
            "status": item.get("status") or "",
        })
        if not group["source_os"]:
            group["source_os"] = str(item.get("source_os") or "").strip()
        busid = str(item.get("busid") or "")
        serials = list(dict.fromkeys(
            str(serial or "").strip()
            for serial in item.get("device_serials") or []
            if str(serial or "").strip()
        ))
        if busid:
            group["busids"].append(busid)
            group["device_serials_by_busid"][busid] = serials
            group["statuses_by_busid"][busid] = str(item.get("status") or "unknown")
            group["generations_by_busid"][busid] = int(item.get("generation") or 0)
            group["network_quality_by_busid"][busid] = item.get("network_quality") or {}
        for serial in serials:
            if serial not in group["device_serials"]:
                group["device_serials"].append(serial)

    selections = [
        {
            **group,
            "busids": sorted(group["busids"]),
        }
        for group in grouped.values()
    ]
    for group in selections:
        # 序列号跟随排序后的 busid 顺序，保证输出稳定可读。
        ordered = list(dict.fromkeys(
            serial
            for busid in group["busids"]
            for serial in group["device_serials_by_busid"].get(busid) or []
        ))
        group["device_serials"] = ordered
    selections.sort(key=lambda group: (group["device_host"], group["worker_id"]))

    transport_state_by_busid: dict[str, dict[str, str]] = {}
    verified = False
    if verify:
        verified_states = await asyncio.to_thread(
            _verify_local_usbip_transport,
            _usbip_assignments(),
        )
        verified = True
        for group in selections:
            group_states: dict[str, str] = {}
            for busid in group["busids"]:
                key = f"{group['device_host']}|{busid}"
                state = verified_states.get(key) or {}
                group_states[busid] = str(state.get("transport_state") or "unknown")
            group["transport_state_by_busid"] = group_states
        for key, state in verified_states.items():
            device_host, _sep, busid = key.rpartition("|")
            transport_state_by_busid.setdefault(device_host, {})[busid] = (
                str(state.get("transport_state") or "unknown")
            )
    return JSONResponse(content={
        "success": True,
        "cluster_selections": selections,
        "total": len(selections),
        "verified": verified,
        "transport_state_by_busid": transport_state_by_busid,
    })

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

    assignments = [
        item for item in _usbip_assignments().values()
        if str(item.get("device_host") or "") == client_id
    ]
    state_info, local_transport_mismatch = await _reconcile_local_usbip_status(
        client_id,
        assignments,
        state_info,
        config,
    )
    connected = bool(state_info.get("connected", False))

    # 远端 Worker 的 unknown 分配在这里触发后台核对（幂等 attach），
    # 与本地 `_reconcile_local_usbip_status` 的自动探测语义对齐；核对
    # 不阻塞本次响应，下一次轮询即可看到升级结果。
    from .usbip_status_reconcile import schedule_remote_usbip_verify

    schedule_remote_usbip_verify(client_id, assignments)

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

    status_assignments = [
        {
            **item,
            "status": "unknown",
        }
        if (
            local_transport_mismatch
            and str(item.get("worker_id") or "") == _local_worker_id()
        )
        else item
        for item in assignments
    ]
    cluster_selections = []
    if status_assignments:
        connected = True
        grouped: dict[tuple[str, str], dict[str, object]] = {}
        for item in status_assignments:
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
                "source_os": "",
            })
            if not grouped_item["source_os"]:
                grouped_item["source_os"] = str(item.get("source_os") or "").strip()
            busid = str(item.get("busid") or "")
            serials = list(dict.fromkeys(
                str(serial or "").strip()
                for serial in item.get("device_serials") or []
                if str(serial or "").strip()
            ))
            if busid:
                grouped_item["busids"].append(busid)
                grouped_item["device_serials_by_busid"][busid] = serials
                assignment_status = str(item.get("status") or "unknown")
                if (
                    assignment_status == "unknown"
                    and state_info.get("transport_connected", False)
                    and str(item.get("worker_id") or "") == _local_worker_id()
                ):
                    # The heartbeat reports protocol readiness, while the
                    # exact local ``usbip port`` probe reports transport
                    # readiness.  Keep the persisted diagnostic state but do
                    # not present a confirmed local transport as disconnected.
                    assignment_status = "attached"
                grouped_item["statuses_by_busid"][busid] = assignment_status
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
                "source_os": grouped_item["source_os"],
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
        str(item.get("status") or "unknown") for item in status_assignments
    }
    protocol_state = str((state_info.get("protocol_status") or {}).get("mode") or "unknown")
    transport_state = (
        "degraded" if local_transport_mismatch
        else "cleanup_required" if "cleanup_required" in assignment_states
        else "detaching" if "detaching" in assignment_states
        else "attaching" if "attaching" in assignment_states
        else "degraded" if (
            "unknown" in assignment_states
            and not state_info.get("transport_connected", False)
        )
        else "attached" if connected
        else "disconnected"
    )
    readiness = (
        "not_ready" if local_transport_mismatch
        else "test_ready" if protocol_state in {"adb", "fastboot", "recovery"}
        else "protocol_ready" if protocol_state not in {
            "unknown", "offline", "unauthorized", "reconnecting",
        }
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
        "transport_mismatch": local_transport_mismatch,
        "protocol_status": state_info.get("protocol_status") or {},
        "transport_state": transport_state,
        "protocol_state": protocol_state,
        "readiness": readiness,
        "cluster_selection": cluster_selections[0] if cluster_selections else None,
        "cluster_selections": cluster_selections,
        "transport_records": build_transport_records(
            usbip_assignments=[
                {**item, "protocol_state": protocol_state}
                for item in status_assignments
            ]
        ),
        "network_quality_history": quality_history,
    })


__all__ = [
    "_reconcile_local_usbip_status",
    "_retire_assignments_for_physically_local_devices",
    "_verify_local_usbip_transport",
    "get_usbip_source_os",
    "get_usbip_status",
    "list_usbip_assignments",
    "list_usbip_source_devices",
]
