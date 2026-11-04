"""AOA 回放过程控制注册表（暂停 / 继续 / 终止）。

回放线程在 ``run_aoa_sequence`` 中按 control_id 注册一个进程内控制点，
每条命令执行前向控制点询问当前动作：继续 / 已暂停（分段 sleep 等待，
期间仍响应终止）/ 终止（抛出 :class:`AoaPlaybackCancelledError`，由既有
异常路径收敛命令终态并完成 USB 清理）。

控制请求可能先于回放注册到达（命令排队、Accessory 配置阶段）：
未知 control_id 首次收到请求时自动登记并立即生效，语义与
"暂停/终止在第一步生效"一致。条目带 TTL 兜底，防止异常路径泄漏。
"""
from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

from foundation.aoa_profile import MAX_AOA_PAUSE_SECONDS


# 控制条目 TTL：远超一次回放的最长预算（MAX_TOTAL_RUNTIME_MS + 暂停），
# 只兜底"创建后从未被执行"的泄漏条目。
_CONTROL_TTL_SECONDS = 4 * 3600
# 注册表互斥：request 与 register 并发；gate 读走无锁快照。
_LOCK = threading.Lock()
_CONTROLS: dict[str, _PlaybackControl] = {}


class AoaPlaybackCancelledError(RuntimeError):
    cancelled = True

    """用户请求终止 AOA 回放（在暂停等待中被唤醒后抛出）。"""

    def __init__(self, index: int, total: int):
        super().__init__(
            f"AOA playback cancelled by user at step {index}/{total}")
        self.index = index
        self.total = total


class _PlaybackControl:
    __slots__ = ("cancelled", "created_at", "pause_event", "paused_seconds", "revision")

    def __init__(self) -> None:
        # set = 已请求暂停；cancel 时清除以便 gate 立即退出等待。
        self.pause_event = threading.Event()
        self.cancelled = False
        self.paused_seconds = 0.0
        self.created_at = time.monotonic()
        self.revision = 0


def _purge_expired_locked() -> None:
    now = time.monotonic()
    stale = [key for key, item in _CONTROLS.items()
             if now - item.created_at > _CONTROL_TTL_SECONDS]
    for key in stale:
        _CONTROLS.pop(key, None)


def _ensure_control(control_id: str) -> _PlaybackControl:
    with _LOCK:
        _purge_expired_locked()
        control = _CONTROLS.get(control_id)
        if control is None:
            control = _PlaybackControl()
            _CONTROLS[control_id] = control
        return control


def register(control_id: str) -> None:
    """回放线程入口登记控制点（幂等；保留已到达的预置请求）。"""
    _ensure_control(control_id)


def unregister(control_id: str) -> None:
    with _LOCK:
        _CONTROLS.pop(control_id, None)


def request(control_id: str, action: str, revision: int | None = None) -> dict[str, Any]:
    """Apply ordered requests; a stale delivery cannot overwrite a newer resume."""
    if action not in {"pause", "resume", "cancel"}:
        raise ValueError(f"unsupported playback control action: {action}")
    control = _ensure_control(control_id)
    with _LOCK:
        incoming = control.revision + 1 if revision is None else revision
        if incoming > control.revision and not control.cancelled:
            control.revision = incoming
            if action == "pause":
                control.pause_event.set()
            elif action == "resume":
                control.pause_event.clear()
            else:
                control.cancelled = True
                control.pause_event.clear()
        return {"paused": control.pause_event.is_set(), "cancelled": control.cancelled}


def status(control_id: str) -> dict[str, Any] | None:
    with _LOCK:
        _purge_expired_locked()
        control = _CONTROLS.get(control_id)
    if control is None:
        return None
    return {"paused": control.pause_event.is_set(),
            "cancelled": control.cancelled}


def wait_if_paused(
    control_id: str,
    *,
    index: int,
    total: int,
    sleep: Callable[[float], None] = time.sleep,
    poll_seconds: float = 0.2,
    on_event: Callable[[dict[str, Any]], None] | None = None,
    on_tick: Callable[[], None] | None = None,
    read_source: Callable[[], dict] | None = None,
) -> float:
    """命令执行前的控制门：暂停则阻塞等待，终止则抛异常。

    返回本次在门内停留的暂停秒数——调用方应把该时长加回回放时间预算
    （deadline），保证长暂停不会把剩余命令判成超时。``on_event`` 在
    状态切换沿（paused/resumed/cancelled）各回调一次，供上报事件。
    """
    def refresh():
        if on_tick:
            on_tick()
        if read_source:
            state = read_source()
            if state.get("action"):
                request(control_id, state["action"], int(state.get("revision") or 0))

    refresh()
    control = _ensure_control(control_id)
    if control.cancelled:
        raise AoaPlaybackCancelledError(index, total)
    if not control.pause_event.is_set():
        return 0.0
    _safe_event(on_event, {"event_type": "paused", "index": index, "total": total})
    started = time.monotonic()
    last_report = started
    while control.pause_event.is_set() and not control.cancelled:
        if control.paused_seconds + time.monotonic() - started >= MAX_AOA_PAUSE_SECONDS:
            raise RuntimeError("AOA pause budget exceeded; inspect the original operation")
        sleep(poll_seconds)
        refresh()
        if control.pause_event.is_set() and time.monotonic() - last_report >= 1:
            _safe_event(on_event, {"event_type": "paused", "index": index, "total": total})
            last_report = time.monotonic()
    paused_for = time.monotonic() - started
    control.paused_seconds += paused_for
    if control.cancelled:
        _safe_event(on_event, {"event_type": "cancelled", "index": index, "total": total})
        raise AoaPlaybackCancelledError(index, total)
    _safe_event(on_event, {"event_type": "resumed", "index": index, "total": total})
    return paused_for


def _safe_event(on_event: Callable[[dict[str, Any]], None],
                event: dict[str, Any]) -> None:
    if on_event is None:
        return
    try:
        on_event(event)
    except Exception:  # 事件上报失败绝不影响回放控制本身
        pass
