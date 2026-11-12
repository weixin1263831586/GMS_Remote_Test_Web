"""Agent session transcript: messages, steps, and redacted parameter text."""

from __future__ import annotations

import logging
import uuid
from typing import Any

from features.assistant import session_store
from features.assistant.payloads import display_record as _display_record
from features.assistant.payloads import jsonable as _jsonable
from features.assistant.session_registry import _now_iso
from foundation.redaction import redact_sensitive_data


_logger = logging.getLogger(__name__)


def _persist_transcript(session: dict[str, Any]) -> None:
    """会话记录落盘（尽力而为）：Controller 重启后可恢复对话正文。

    消息/步骤在进入会话时已经 ``display_record`` 脱敏，这里只是转存；
    落盘失败只记日志，不阻断对话。
    """
    try:
        session_store.save_transcript(session)
    except Exception:
        _logger.warning(
            "保存 Agent 会话记录失败，Controller 重启后该会话不可恢复",
            exc_info=True,
        )


def _new_message(role: str, content: str, kind: str = "text", data: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "id": str(uuid.uuid4()),
        "role": role,
        "kind": kind,
        "content": content,
        "data": data or {},
        "created_at": _now_iso(),
    }


def _append_step(session: dict[str, Any], title: str, status: str = "done", detail: str = "", data: dict[str, Any] | None = None) -> None:
    step = {
        "id": str(uuid.uuid4()),
        "title": title,
        "status": status,
        "detail": detail,
        "data": data or {},
        "created_at": _now_iso(),
    }
    safe = _display_record(session, step)
    session.setdefault("steps", []).append(safe)
    session["updated_at"] = _now_iso()
    _persist_transcript(session)


def _derive_session_title(session: dict[str, Any], content: str) -> None:
    """首次用户输入派生会话标题（会话目录展示用），只取首个非空行。"""
    if session.get("title"):
        return
    first_line = next((line.strip() for line in content.splitlines() if line.strip()), "")
    if first_line:
        session["title"] = first_line[:40]


def _append_message(session: dict[str, Any], role: str, content: str, kind: str = "text", data: dict[str, Any] | None = None, *, private_params=None) -> dict[str, Any]:
    if role == "user":
        _derive_session_title(session, content)
    msg = _new_message(role, content, kind=kind, data=data)
    msg = _display_record(session, msg, private_params)
    session.setdefault("messages", []).append(msg)
    session["updated_at"] = _now_iso()
    _persist_transcript(session)
    return msg


def _format_params(params: dict[str, Any]) -> str:
    """格式化参数为可读文本。"""
    if not params:
        return "无"
    display = redact_sensitive_data(_jsonable(params))
    lines = [f"- {k}: {v}" for k, v in display.items() if v]
    return "\n".join(lines) if lines else "无"
