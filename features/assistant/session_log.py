"""Agent session transcript: messages, steps, and redacted parameter text."""

from __future__ import annotations

import uuid
from typing import Any

from features.assistant.payloads import display_record as _display_record
from features.assistant.payloads import jsonable as _jsonable
from features.assistant.session_registry import _now_iso
from foundation.redaction import redact_sensitive_data


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


def _append_message(session: dict[str, Any], role: str, content: str, kind: str = "text", data: dict[str, Any] | None = None, *, private_params=None) -> dict[str, Any]:
    msg = _new_message(role, content, kind=kind, data=data)
    msg = _display_record(session, msg, private_params)
    session.setdefault("messages", []).append(msg)
    session["updated_at"] = _now_iso()
    return msg


def _format_params(params: dict[str, Any]) -> str:
    """格式化参数为可读文本。"""
    if not params:
        return "无"
    display = redact_sensitive_data(_jsonable(params))
    lines = [f"- {k}: {v}" for k, v in display.items() if v]
    return "\n".join(lines) if lines else "无"
