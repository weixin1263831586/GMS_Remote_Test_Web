"""Controller-side verification of remote Worker USB/IP assignments.

本地（Controller）的 unknown 分配由 ``_reconcile_local_usbip_status`` 通过
``usbip port`` 探测自动升级/触发重连；远端 Worker 之前只有心跳里的 ADB
序列号核对（``reconcile_cluster_usbip_heartbeat``）。当传输仍挂着但 ADB
看不到序列号（fastboot/loader/unauthorized 等协议态），或 Worker 自身
恢复任务未覆盖到时，远端分配会长期停留在 ``unknown``，UI 只能显示
“状态待确认”。

本模块对远端 unknown 分配下发一次幂等的 ``usbip_attach`` 核对命令：
- Worker 上端口仍在 → Rust 执行器走 already_attached 路径，原样返回
  ``attached``，Controller 把分配升级回 ``attached``；
- Worker 上端口已丢 → 执行器按原 BUSID 重新接入（真正的“核对并重连”）；
- 失败（源不可达/BUSID 变化等）→ 分配保持 ``unknown``，交由用户诊断。

核对在后台任务中执行，不阻塞 ``/api/usbip/status`` 响应；同一
``device_host|worker_id`` 组合有节流窗口，避免状态轮询打爆 Worker。
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any


logger = logging.getLogger(__name__)

# 状态接口会被前端在弹窗打开期间高频轮询；同一来源+Worker 的远端核对
# 至少间隔这么久才再次下发，成功后立即解除节流。
REMOTE_VERIFY_THROTTLE_SECONDS = 45.0
# 单个核对命令的等待上限：already_attached 路径只做端口查询 + ADB 探测，
# 重新接入路径含最长 ~15s 的枚举等待，90s 已足够并留有余量。
REMOTE_VERIFY_COMMAND_TIMEOUT = 90

_verify_throttle: dict[str, float] = {}
_throttle_lock = asyncio.Lock()
_running_verify_tasks: set[asyncio.Task] = set()


def _normalize_host(host: str) -> str:
    host = str(host or "").strip()
    return host.split("@", 1)[-1] if "@" in host else host


def _throttle_key(device_host: str, worker_id: str) -> str:
    return f"{device_host or ''!s}|{worker_id or ''!s}"


async def _acquire_verify_slot(device_host: str, worker_id: str) -> bool:
    async with _throttle_lock:
        key = _throttle_key(device_host, worker_id)
        last = _verify_throttle.get(key)
        if last is not None and time.monotonic() - last < REMOTE_VERIFY_THROTTLE_SECONDS:
            return False
        _verify_throttle[key] = time.monotonic()
        return True


def _release_verify_slot(device_host: str, worker_id: str) -> None:
    _verify_throttle.pop(_throttle_key(device_host, worker_id), None)


def pending_remote_verify_targets(assignments: list[dict]) -> bool:
    """Return True when at least one remote assignment sits in ``unknown``."""
    from .integrations_api import _local_worker_id

    local_worker_id = _local_worker_id()
    return any(
        str(item.get("worker_id") or "") not in {"", local_worker_id}
        and str(item.get("status") or "") == "unknown"
        for item in assignments or []
    )


async def verify_remote_usbip_assignments(
    device_host: str,
    assignments: list[dict],
) -> None:
    """Re-verify remote ``unknown`` assignments through idempotent attach."""
    from foundation.cluster_port import get_cluster_service, run_worker_command

    from .integrations_api import (
        _local_worker_id,
        _save_usbip_assignments,
        _usbip_assignment_key,
        _usbip_assignment_lock,
        _usbip_assignments,
    )

    remote_unknown = [
        item for item in assignments or []
        if str(item.get("status") or "") == "unknown"
        and str(item.get("busid") or "").strip()
        and str(item.get("worker_id") or "") not in {"", _local_worker_id()}
    ]
    if not remote_unknown:
        return

    try:
        cluster = get_cluster_service()
    except (RuntimeError, AttributeError):
        return

    by_worker: dict[str, list[dict]] = {}
    for item in remote_unknown:
        by_worker.setdefault(str(item.get("worker_id")), []).append(item)

    for worker_id, items in by_worker.items():
        if not await _acquire_verify_slot(device_host, worker_id):
            continue
        try:
            await _verify_worker_assignments(
                cluster, run_worker_command, device_host, worker_id, items,
                save_assignments=_save_usbip_assignments,
                assignment_key=_usbip_assignment_key,
                assignment_lock=_usbip_assignment_lock,
                load_assignments=_usbip_assignments,
            )
            # 核对成功：解除节流，让紧随其后的状态轮询能立即反映结果。
            _release_verify_slot(device_host, worker_id)
        except Exception as exc:
            # 核对失败：保留节流时间戳，按窗口重试，避免轮询打爆 Worker。
            logger.warning(
                "[USB/IP Verify] remote verify failed for %s -> %s: %s",
                device_host, worker_id, exc,
            )


async def _verify_worker_assignments(
    cluster: Any,
    run_worker_command: Any,
    device_host: str,
    worker_id: str,
    items: list[dict],
    *,
    save_assignments,
    assignment_key,
    assignment_lock,
    load_assignments,
) -> None:
    worker = cluster.repository.get_worker(worker_id) or {}
    if str(worker.get("status") or "") not in {"online", "busy"}:
        logger.info(
            "[USB/IP Verify] skip offline worker %s for %s", worker_id, device_host,
        )
        return

    # 同一 Worker 上按 (source_host, generation) 分组下发，一次命令覆盖
    # 该组的全部 BUSID；generation 沿用分配自身，避免与更新操作互相覆盖。
    groups: dict[tuple[str, int], list[dict]] = {}
    for item in items:
        key = (
            _normalize_host(str(item.get("source_host") or "")),
            int(item.get("generation") or 0),
        )
        groups.setdefault(key, []).append(item)

    for (source_host, generation), group in groups.items():
        busids = [
            str(item.get("busid") or "").strip() for item in group
        ]
        payload = {
            "device_host": device_host,
            "source_host": source_host,
            "busids": busids,
            "generation": generation,
            "operation_id": f"usbip-verify-{uuid.uuid4().hex}",
        }
        result = await run_worker_command(
            worker_id, "usbip_attach", payload,
            timeout=REMOTE_VERIFY_COMMAND_TIMEOUT,
        )
        devices = result.get("devices")
        if isinstance(devices, list):
            try:
                cluster.repository.refresh_worker_devices(worker_id, devices)
            except Exception as exc:
                logger.warning(
                    "[USB/IP Verify] refresh devices for %s failed: %s",
                    worker_id, exc,
                )
        with assignment_lock:
            assignments = load_assignments()
            for item in group:
                busid = str(item.get("busid") or "").strip()
                key = assignment_key(device_host, busid)
                current = assignments.get(key) or {}
                if (
                    str(current.get("worker_id") or "") != worker_id
                    or int(current.get("generation") or 0) != generation
                ):
                    continue
                current.update({"status": "attached", "timestamp": time.time()})
                assignments[key] = current
            save_assignments(assignments)
        logger.info(
            "[USB/IP Verify] %s -> %s busids %s verified as attached",
            device_host, worker_id, ",".join(busids),
        )


def schedule_remote_usbip_verify(
    device_host: str,
    assignments: list[dict],
) -> None:
    """Fire-and-forget remote verification; never blocks the status response."""
    if not pending_remote_verify_targets(assignments):
        return
    try:
        task = asyncio.get_running_loop().create_task(
            verify_remote_usbip_assignments(device_host, assignments)
        )
    except RuntimeError:
        return
    _running_verify_tasks.add(task)
    task.add_done_callback(_running_verify_tasks.discard)
