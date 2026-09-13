"""USB/IP cluster assignment 状态存储层。

从 ``integrations_api.py`` 抽出的第一块 service(模块拆分债务收敛):
围绕 ``runtime_config["usbip_cluster_assignments"]`` 的读写原语——
加载/保存/清理 stale unknown/serial 回填/detach 标记/generation 分配。

路由(``integrations_api``)只编排;assignment 一致性规则集中在这里,
后续 transport 状态机(DISCOVERED→BOUND→ATTACHED→ADB_ONLINE)演进时
以本模块为唯一存储入口。
"""

from __future__ import annotations

import time

from foundation.cluster_port import get_local_worker_id

from . import runtime
from .usbip_persistence import (
    persist_local_usbip_sources,
    usbip_assignment_lock,
)


def load_usbip_assignments() -> dict[str, dict]:
    """读取当前集群 assignment 快照(浅拷贝,调用方修改不影响存储)。"""
    getter = getattr(runtime.config_manager, "get_runtime_config", None)
    runtime_config = getter() if callable(getter) else {}
    runtime_config = runtime_config or {}
    assignments = runtime_config.get("usbip_cluster_assignments") or {}
    return dict(assignments) if isinstance(assignments, dict) else {}


def save_usbip_assignments(assignments: dict[str, dict]) -> bool:
    """持久化 assignment 快照;失败抛 RuntimeError(调用方以真值判断)。"""
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
    # 成功路径必须显式返回 True，调用方以真值判断保存是否成功。
    return True


def usbip_assignment_key(device_host: str, busid: str) -> str:
    return f"{device_host}|{busid}"


def next_transport_generation(assignments: dict[str, dict]) -> int:
    """generation 单调递增:毫秒时间戳与现有最大值+1 的较大者。"""
    return max(
        int(time.time() * 1000),
        max(
            (int(item.get("generation") or 0) for item in assignments.values()),
            default=0,
        ) + 1,
    )


def prune_stale_unknown_usbip_assignments(
    device_host: str,
    current_busids: set[str],
) -> list[str]:
    """Remove degraded assignments whose Windows BUSID no longer exists."""
    with usbip_assignment_lock:
        assignments = load_usbip_assignments()
        stale_keys = [
            key
            for key, item in assignments.items()
            if str(item.get("device_host") or "") == device_host
            and str(item.get("status") or "") == "unknown"
            and str(item.get("busid") or "") not in current_busids
        ]
        if stale_keys:
            for key in stale_keys:
                assignments.pop(key, None)
            save_usbip_assignments(assignments)
    return stale_keys


def reconcile_usbip_assignment_serials(
    device_host: str,
    source_devices: list[dict],
    source_os: str = "",
) -> bool:
    """Backfill assignment serials from the authoritative source busid list."""
    serial_by_busid = {
        str(item.get("busid") or ""): str(item.get("serial") or "").strip()
        for item in source_devices
        if str(item.get("busid") or "") and str(item.get("serial") or "").strip()
    }
    if not serial_by_busid:
        return False

    resolved_os = str(source_os or "").strip()
    local_serials: list[str] = []
    changed = False
    with usbip_assignment_lock:
        assignments = load_usbip_assignments()
        for key, assignment in assignments.items():
            if str(assignment.get("device_host") or "") != device_host:
                continue
            serial = serial_by_busid.get(str(assignment.get("busid") or ""))
            if not serial:
                continue
            updated = {**assignment, "device_serials": [serial]}
            if resolved_os and not str(assignment.get("source_os") or "").strip():
                updated["source_os"] = resolved_os
            if updated != assignment:
                assignments[key] = updated
                changed = True
            if str(assignment.get("worker_id") or "") == get_local_worker_id():
                local_serials.append(serial)
        if changed:
            save_usbip_assignments(assignments)
    persist_local_usbip_sources(device_host, local_serials, source_os=resolved_os)
    return changed


def mark_usbip_detach_unknown(
    device_host: str,
    busids: list[str],
    worker_id: str,
    generation: int,
) -> None:
    """Detach 中途失败:把对应 assignment 标记为 unknown 等待 reconcile。

    以 (worker_id, generation) 做 CAS——旧 worker 的迟到 detach 失败
    不得覆盖新一代 assignment 状态。
    """
    with usbip_assignment_lock:
        assignments = load_usbip_assignments()
        for busid in busids:
            key = usbip_assignment_key(device_host, busid)
            current = assignments.get(key) or {}
            if (
                current.get("worker_id") == worker_id
                and int(current.get("generation") or 0) == generation
            ):
                current.update({"status": "unknown", "timestamp": time.time()})
                assignments[key] = current
        save_usbip_assignments(assignments)
