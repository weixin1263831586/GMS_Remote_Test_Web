"""Agent session transcript: messages, steps, and redacted parameter text."""

from __future__ import annotations

import logging
import uuid
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from typing import Any

from features.assistant import session_store
from features.assistant.session_registry import _now_iso
from foundation.redaction import redact_sensitive_data


_logger = logging.getLogger(__name__)


def jsonable(value: Any) -> Any:
    """Return a JSON-serializable copy of Agent session data."""
    if is_dataclass(value):
        return jsonable(asdict(value))
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [jsonable(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if hasattr(value, "model_dump"):
        return jsonable(value.model_dump())
    return str(value)


def session_payload(session: dict[str, Any]) -> dict[str, Any]:
    return redact_sensitive_data(jsonable(session))


def display_record(session: dict[str, Any], record: dict[str, Any], private_params=None) -> dict[str, Any]:
    """Sanitize public history without replacing the internal execution plan."""
    safe = session_payload({
        'record': record, 'messages': session.get('messages', []), 'steps': session.get('steps', []),
        'pending_plan': session.get('pending_plan'),
        'context': session.get('context'), 'private_params': private_params,
    })
    session['messages'] = safe['messages']
    session['steps'] = safe['steps']
    if 'context' in session:
        session['context'] = safe['context']
    return safe['record']


# 步骤终态集合：进入这些状态时记录 finished_at / duration_seconds。
_STEP_TERMINAL_STATUSES = ("done", "warning", "error")


def _settle_step(step: dict[str, Any]) -> None:
    """步骤进入终态时补记结束时刻与耗时（秒，1 位小数）。幂等。

    开始时刻取 ``started_at``（追加时刻）；历史数据缺字段时回退
    ``created_at``，解析失败按 0 秒兜底——耗时只是展示信息，
    任何异常都不应影响会话流转。
    """
    if step.get("status") not in _STEP_TERMINAL_STATUSES or step.get("finished_at"):
        return
    finished = _now_iso()
    step["finished_at"] = finished
    duration = 0.0
    try:
        started = datetime.fromisoformat(
            str(step.get("started_at") or step.get("created_at") or finished)
        )
        duration = max(
            0.0,
            (datetime.fromisoformat(finished) - started).total_seconds(),
        )
    except (TypeError, ValueError):
        pass
    step["duration_seconds"] = round(duration, 1)


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
    now = _now_iso()
    step = {
        "id": str(uuid.uuid4()),
        "title": title,
        "status": status,
        "detail": detail,
        "data": data or {},
        "created_at": now,
        # 耗时统计起点：与 created_at 同值，但语义独立（created_at 是
        # 排序/展示时间戳，started_at 专供 duration 计算）。
        "started_at": now,
    }
    safe = display_record(session, step)
    session.setdefault("steps", []).append(safe)
    _settle_step(safe)
    session["updated_at"] = _now_iso()
    _persist_transcript(session)


# This step is a transient monitor indicator, not the Redmine/ATS/test result.
# Earlier versions accumulated "会话已恢复"/done on each Controller restart.
RECOVERY_STEP_TITLE = "任务监控恢复"
_RECOVERY_TITLES = frozenset((RECOVERY_STEP_TITLE, "会话已恢复"))
_RUN_ID_KEYS = ("redmine_run_id", "automation_run_id", "cluster_job_id")


def _find_step(session: dict[str, Any], step_id: Any) -> dict[str, Any] | None:
    if not step_id:
        return None
    return next(
        (item for item in session.get("steps", []) if item.get("id") == step_id),
        None,
    )


def _step_timestamp(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _recovery_steps(session: dict[str, Any], active: dict[str, Any]) -> list[dict[str, Any]]:
    """Identify the current run's markers, including old untagged transcripts."""
    steps = session.get("steps") or []
    run_key = next((key for key in _RUN_ID_KEYS if active.get(key)), None)
    run_id = str(active.get(run_key)) if run_key else ""
    anchor_id = active.get("recovery_step_id")
    analysis_id = active.get("redmine_step_id")
    analysis_index = next(
        (index for index, item in enumerate(steps) if item.get("id") == analysis_id),
        None,
    ) if analysis_id else None
    started = _step_timestamp(active.get("started_at")) if analysis_index is None else None
    matching = []
    for index, step in enumerate(steps):
        if step.get("title") not in _RECOVERY_TITLES:
            continue
        if anchor_id and step.get("id") == anchor_id:
            matching.append(step)
            continue
        data = step.get("data") or {}
        # Explicit IDs take priority over timestamps; never touch another run.
        if any(data.get(key) for key in _RUN_ID_KEYS):
            if run_key and str(data.get(run_key) or "") == run_id:
                matching.append(step)
            continue
        if analysis_index is not None:
            if index > analysis_index:
                matching.append(step)
        elif started:
            created = _step_timestamp(step.get("created_at"))
            if created and created >= started:
                matching.append(step)
    return matching


def append_recovery_step(
    session: dict[str, Any], active: dict[str, Any],
    detail: str = "正在重新关联后台任务监控…",
) -> None:
    """One running recovery indicator per active run, including across restarts."""
    if not any(active.get(key) for key in _RUN_ID_KEYS):
        return
    matches = _recovery_steps(session, active)
    if matches:
        step = matches[0]
        duplicate_ids = {item.get("id") for item in matches[1:]}
        changed = bool(duplicate_ids)
        if duplicate_ids:
            session["steps"] = [
                item for item in session["steps"]
                if item.get("id") not in duplicate_ids
            ]
        if step.get("title") != RECOVERY_STEP_TITLE or step.get("status") != "running":
            step["title"] = RECOVERY_STEP_TITLE
            step["status"] = "running"
            step["detail"] = redact_sensitive_data(detail)
            step.pop("finished_at", None)
            step.pop("duration_seconds", None)
            changed = True
        payload = step.setdefault("data", {})
        if payload.get("kind") != "agent_recovery_monitor":
            payload["kind"] = "agent_recovery_monitor"
            changed = True
        if changed:
            session["updated_at"] = _now_iso()
            _persist_transcript(session)
    else:
        ids = {key: active[key] for key in _RUN_ID_KEYS if active.get(key)}
        ids["kind"] = "agent_recovery_monitor"
        _append_step(session, RECOVERY_STEP_TITLE, "running", detail, ids)
        step = session["steps"][-1]
    if active.get("recovery_step_id") != step["id"]:
        active["recovery_step_id"] = step["id"]
        # Persist the reference: old code only changed the in-memory dict.
        from .session_registry import save_session_anchor

        save_session_anchor(session)


def confirm_recovery_step(
    session: dict[str, Any], active: dict[str, Any],
    detail: str = "后台任务仍在执行，监控已恢复。",
) -> None:
    """First successful poll confirms connectivity, NOT task completion."""
    step = _find_step(session, active.get("recovery_step_id"))
    if step and step.get("status") == "running" and step.get("detail") != detail:
        step["detail"] = redact_sensitive_data(detail)
        session["updated_at"] = _now_iso()
        _persist_transcript(session)


def complete_recovery_step(
    session: dict[str, Any], active: dict[str, Any], *,
    status: str = "done", detail: str = "已重新关联后台任务监控。",
) -> None:
    """Drop transient recovery markers when the actual run leaves monitoring.

    The real analysis/test/ATS step owns final status and duration. A second
    green 'recovery completed' step is redundant and misleading.
    """
    _ = (status, detail)
    matches = _recovery_steps(session, active)
    if matches:
        ids = {step.get("id") for step in matches}
        session["steps"] = [
            step for step in session.get("steps", []) if step.get("id") not in ids
        ]
        session["updated_at"] = _now_iso()
        _persist_transcript(session)
    active.pop("recovery_step_id", None)


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
    msg = display_record(session, msg, private_params)
    session.setdefault("messages", []).append(msg)
    session["updated_at"] = _now_iso()
    _persist_transcript(session)
    return msg


def _format_params(params: dict[str, Any]) -> str:
    """格式化参数为可读文本。"""
    if not params:
        return "无"
    display = redact_sensitive_data(jsonable(params))
    lines = [f"- {k}: {v}" for k, v in display.items() if v]
    return "\n".join(lines) if lines else "无"
