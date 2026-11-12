"""Owner-scoped session history, directory entries and deletion."""

from __future__ import annotations

from typing import Any

from foundation.responses import error_response, success_response

from . import session_store
from .session_registry import _agent_monitor_tasks, _agent_sessions, restore_session


def _restore_session_from_transcript(
    session_id: str, client_id: str, owner_id: str
) -> dict[str, Any] | None:
    """从落盘对话记录重建只读会话（无活跃运行的重启恢复）。

    消息/步骤原样回放（落盘前已脱敏）；pending_plan 不落盘，重启后
    待确认计划失效，用户需要重新描述或重新规划。
    """
    if not client_id and not owner_id:
        return None
    try:
        record = session_store.load_transcript(session_id)
    except Exception:
        return None
    if not record:
        return None
    if record.get("client_id") != client_id and not (
        owner_id and record.get("owner_id") == owner_id
    ):
        return None
    session = {
        "session_id": session_id,
        "client_id": client_id or str(record.get("client_id") or ""),
        "owner_id": owner_id or str(record.get("owner_id") or ""),
        "title": record.get("title") or "",
        "status": record.get("status") or "idle",
        "messages": list(record.get("messages") or []),
        "steps": list(record.get("steps") or []),
        "pending_plan": None,
        "active_run": None,
        "workspace_context": dict(record.get("workspace_context") or {}),
        "created_at": record.get("created_at") or "",
        "updated_at": record.get("updated_at") or "",
        "recovered": True,
    }
    if session["status"] in {"running", "monitoring", "thinking"}:
        # 记录里的运行状态已无人续接，不能伪装成进行中。
        session["status"] = "idle"
    _agent_sessions[session_id] = session
    return session


def _session_directory_entry(session_id: str, anchor: dict[str, Any]) -> dict[str, Any]:
    live = _agent_sessions.get(session_id)
    source = live or anchor
    return {
        "session_id": session_id,
        "title": str(source.get("title") or "未命名会话"),
        "status": str(source.get("status") or "idle"),
        "created_at": source.get("created_at") or "",
        "updated_at": source.get("updated_at") or "",
        "active": bool(live and (live.get("active_run") or live.get("pending_plan"))),
    }


def _session_directory(client_id: str, owner_id: str) -> list[dict[str, Any]]:
    """合并内存会话、持久锚点与落盘记录，返回该客户端的会话目录（新→旧）。

    内存会话与同 id 锚点/记录合并去重；仅保留 client_id 或 owner_id
    命中的会话（与恢复属主判定同语义），记录文件携带重启前的对话
    元数据，保证强刷/重启后目录不丢会话。
    """
    entries: dict[str, dict[str, Any]] = {}
    for session_id, session in _agent_sessions.items():
        if session.get("client_id") == client_id or (
            owner_id and session.get("owner_id") == owner_id
        ):
            entries[session_id] = _session_directory_entry(session_id, {})
    try:
        anchors = session_store.load_anchors()
    except Exception:
        anchors = {}
    for session_id, anchor in anchors.items():
        matched = anchor.get("client_id") == client_id or (
            owner_id and anchor.get("owner_id") == owner_id
        )
        if matched and session_id not in entries:
            entries[session_id] = _session_directory_entry(session_id, anchor)
    try:
        transcripts = session_store.load_transcripts()
    except Exception:
        transcripts = {}
    for session_id, record in transcripts.items():
        matched = record.get("client_id") == client_id or (
            owner_id and record.get("owner_id") == owner_id
        )
        if matched and session_id not in entries:
            entries[session_id] = {
                "session_id": session_id,
                "title": str(record.get("title") or "未命名会话"),
                "status": str(record.get("status") or "idle"),
                "created_at": record.get("created_at") or "",
                "updated_at": record.get("updated_at") or "",
                "active": False,
            }
    return sorted(entries.values(), key=lambda item: item.get("updated_at") or "", reverse=True)


def delete_session(session_id: str, client_id: str, owner_id: str):
    """Discard one Agent session: drop live state and its recovery anchor."""
    session = _agent_sessions.get(session_id)
    if not session or session.get("client_id") != client_id:
        session = restore_session(session_id, client_id, owner_id)
    if not session or session.get("client_id") != client_id:
        return error_response("Agent session not found", 404)
    task = _agent_monitor_tasks.pop(session_id, None)
    if task and not task.done():
        task.cancel()
    from features.assistant.project_agent import cancel_project_turn

    cancel_project_turn(session_id)
    _agent_sessions.pop(session_id, None)
    try:
        session_store.drop_anchor(session_id)
    except Exception:
        pass
    try:
        session_store.drop_transcript(session_id)
    except Exception:
        pass
    return success_response({"session_id": session_id}, "Agent session deleted")
