"""Crash-recovery关联表：持久化对话 Agent 会话的最小恢复锚点。

外部深度评审报告项：会话状态此前只存在于进程内存，Controller 重启后
``session_id`` 与 ``cluster_job_id`` / ``attempt_id`` / 执行阶段的关联
丢失，监控无法恢复。这里只持久化恢复锚点（会话元数据 + 活跃运行关联），
不持久化消息正文、asyncio.Task 或凭据；完整 UI 状态仍是可丢弃的。

存储为单个 JSON 文件（原子替换），量级 = 会话数（每会话 < 2KB），
与 ``data/security_audit.json`` 等既有运行时文件同级。
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any


_LOCK = threading.Lock()
_MAX_RECORDS = 256

# 会话字段里参与持久化恢复锚点的白名单；其余字段（消息、步骤、
# pending_plan 等）重启后不还原，由 UI 按「过期」处理。
_SESSION_ANCHOR_FIELDS = (
    "session_id",
    "client_id",
    "owner_id",
    "status",
    "created_at",
    "updated_at",
    "active_run",
    "workspace_context",
)


def default_sessions_path() -> Path:
    from foundation.config import PROJECT_ROOT

    data_dir = Path(PROJECT_ROOT) / "data" / "assistant"
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir / "agent_sessions.json"


def _normalize_active_run(active_run: Any) -> dict[str, Any] | None:
    if not isinstance(active_run, dict):
        return None
    # 恢复锚点只需要关联 ID 与阶段；plan 里可能带 pre_actions 密码，
    # 明确剥离，不落盘。
    return {
        key: active_run.get(key)
        for key in (
            "cluster_job_id",
            "attempt_id",
            "worker_id",
            "baseline_report_timestamp",
            "started_at",
        )
        if active_run.get(key)
    } or None


def session_anchor(session: dict[str, Any]) -> dict[str, Any]:
    """Extract the persistence anchor from a live session dict."""
    anchor: dict[str, Any] = {}
    for key in _SESSION_ANCHOR_FIELDS:
        if key == "active_run":
            anchor[key] = _normalize_active_run(session.get("active_run"))
        elif key == "workspace_context":
            workspace = session.get("workspace_context")
            anchor[key] = {k: v for k, v in (workspace or {}).items() if isinstance(v, (str, int, float, bool))}
        elif session.get(key):
            anchor[key] = session[key]
    return anchor


def load_anchors(path: Path | str | None = None) -> dict[str, dict[str, Any]]:
    """Load persisted anchors; tolerate truncation/corruption (best effort)."""
    target = Path(path) if path else default_sessions_path()
    anchors = _read_store_unlocked(target)
    return {
        str(sid): anchor
        for sid, anchor in anchors.items()
        if isinstance(anchor, dict) and anchor.get("session_id")
    }


def save_anchor(session: dict[str, Any], path: Path | str | None = None) -> None:
    """Merge one session's anchor into the store atomically."""
    anchor = session_anchor(session)
    if not anchor.get("session_id"):
        return
    _write_store({str(anchor["session_id"]): anchor}, path, replace_all=False)


def drop_anchor(session_id: str, path: Path | str | None = None) -> None:
    """Remove an anchor after the session is cancelled/expired cleanly."""
    target = Path(path) if path else default_sessions_path()
    with _LOCK:
        anchors = _read_store_unlocked(target)
        if session_id not in anchors:
            return
        anchors.pop(session_id, None)
        _write_store_unlocked(target, anchors)


def _write_store(
    update: dict[str, dict[str, Any]],
    path: Path | str | None,
    *,
    replace_all: bool,
) -> None:
    target = Path(path) if path else default_sessions_path()
    with _LOCK:
        anchors = {} if replace_all else _read_store_unlocked(target)
        anchors.update(update)
        _write_store_unlocked(target, anchors)


def _read_store_unlocked(target: Path | str) -> dict[str, dict[str, Any]]:
    target = Path(target)
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    sessions = raw.get("sessions") if isinstance(raw, dict) else None
    if not isinstance(sessions, dict):
        return {}
    return sessions
def _write_store_unlocked(target: Path, anchors: dict[str, dict[str, Any]]) -> None:
    # 超量时按 updated_at 淘汰最旧的锚点，防止长期运行无限增长。
    if len(anchors) > _MAX_RECORDS:
        ranked = sorted(
            anchors.items(),
            key=lambda item: str(item[1].get("updated_at") or ""),
            reverse=True,
        )
        anchors = dict(ranked[:_MAX_RECORDS])
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {"version": 1, "sessions": anchors},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    tmp_path = target.with_suffix(".json.tmp")
    # os.replace 保证原子性；与 security_audit.json 同款写法。
    with open(tmp_path, "w", encoding="utf-8") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp_path, target)
