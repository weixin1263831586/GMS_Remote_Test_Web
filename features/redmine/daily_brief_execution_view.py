"""Sanitized AI execution details exposed by the daily-brief UI."""

from __future__ import annotations

from typing import Any

from .daily_brief_models import DailyBriefIssue


def _tool_succeeded(tools: list[Any], name_fragment: str) -> bool:
    """Accept current traces and the previous persisted trace shape."""
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        name = str(tool.get("tool_name") or "")
        has_result = tool.get("status") == "succeeded" or (
            not tool.get("status")
            and not tool.get("is_error")
            and bool(tool.get("output_sha256"))
        )
        if has_result and name_fragment in name:
            return True
    return False


def issue_payload(
    issue: DailyBriefIssue, execution: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Build the browser payload without raw model or tool output."""
    payload = issue.to_row()
    payload.pop("raw_response", None)
    if not execution:
        return payload

    tools = execution.get("tools") or []
    gate = (issue.result or {}).get("evidence_gate") or {}
    failure_stage = str(
        execution.get("failure_stage") or execution.get("error_type") or ""
    )
    final_ok = bool(execution.get("final_ok"))
    schema_status = (
        "failed"
        if failure_stage == "schema_mismatch"
        else "passed"
        if final_ok or failure_stage == "evidence_gate_failed"
        else "unknown"
    )
    payload["ai_execution"] = {
        "session_id": execution.get("session_id") or "",
        "status": execution.get("status") or "",
        "failure_stage": failure_stage,
        "failure_message": execution.get("failure_message") or "",
        "schema_status": schema_status,
        "issue_fetched": _tool_succeeded(tools, "redmine_issue_fetch"),
        "journals_checked": _tool_succeeded(tools, "redmine_journals"),
        "attachments_checked": gate.get("attachments_checked") is True,
        "source_evidence_checked": bool(
            execution.get("source_evidence_checked")
        ) or _tool_succeeded(tools, "gms_rt_sdk_") or _tool_succeeded(
            tools, "gms_rt_apk_"
        ),
        "source_evidence_tool_count": int(
            execution.get("source_evidence_tool_count") or 0
        ),
        "history_search_count": int(execution.get("history_search_count") or 0),
        "distinct_history_search_count": int(
            execution.get("distinct_history_search_count") or 0
        ),
        "tool_call_count": int(execution.get("tool_call_count") or 0),
        "repair_attempts": int(execution.get("repair_attempts") or 0),
        "duration_ms": int(
            execution.get("wall_duration_ms")
            or execution.get("duration_ms")
            or 0
        ),
        "input_tokens": int(execution.get("input_tokens") or 0),
        "output_tokens": int(execution.get("output_tokens") or 0),
        "recorded_at": execution.get("recorded_at") or "",
    }
    return payload


__all__ = ["issue_payload"]
