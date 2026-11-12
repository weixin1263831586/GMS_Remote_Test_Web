"""Browser workspace defaults and tool-parameter checks for the Agent."""

from __future__ import annotations

import re
from typing import Any

from features.assistant.context import _parse_chinese_number
from features.assistant.session_log import _append_message


_AGENT_WORKSPACE_FIELDS = {
    "scope_mode", "worker_id", "device_ids", "suite_key", "suite_path",
    "cluster_job_id", "attempt_id", "automation_run_id", "report_id",
    "report_timestamp", "artifact_id", "gerrit_change_id", "gerrit_patchset",
    "redmine_issue_id", "origin_page",
}


def _local_worker_id() -> str:
    from foundation.cluster_port import get_local_worker_id

    return get_local_worker_id()


def _is_local_worker_id(worker_id: str | None) -> bool:
    return not worker_id or worker_id == _local_worker_id()


def _normalize_workspace_context(raw: dict[str, Any] | None) -> dict[str, Any]:
    """Keep only bounded navigation state supplied by the authenticated browser."""
    if not raw:
        return {"scope_mode": "single", "worker_id": _local_worker_id(), "device_ids": []}
    context: dict[str, Any] = {}
    for key, value in (raw or {}).items():
        if key not in _AGENT_WORKSPACE_FIELDS:
            continue
        if key == "device_ids":
            context[key] = [str(item)[:384] for item in (value or [])[:32] if str(item).strip()]
        elif value is not None:
            context[key] = str(value)[:4096]
    if "scope_mode" in raw:
        context["scope_mode"] = "cluster" if context.get("scope_mode") == "cluster" else "single"
    elif context.get("worker_id") and not _is_local_worker_id(context["worker_id"]):
        context["scope_mode"] = "cluster"
    if context.get("scope_mode") == "single":
        context["worker_id"] = _local_worker_id()
    elif context.get("scope_mode") == "cluster":
        context["worker_id"] = context.get("worker_id") or _local_worker_id()
    return context


def _extract_device_count(text: str) -> int:
    if re.search(r"全部|所有|all", text, re.IGNORECASE):
        return 0
    match = re.search(r"(?<![A-Za-z0-9])(\d+)\s*(?:台|个)?\s*(?:设备|device)", text, re.IGNORECASE)
    if match:
        return max(1, min(8, int(match.group(1))))
    match = re.search(r"(一|两|二|三|四|五)\s*台", text)
    if match:
        return _parse_chinese_number(match.group(1))
    return 1


def _apply_workspace_tool_params(
    tool_name: str, params: dict[str, Any], workspace: dict[str, Any]
) -> dict[str, Any]:
    """Fill omitted tool parameters from the visible browser workspace."""
    result = dict(params or {})
    worker_id = workspace.get("worker_id") or _local_worker_id()
    if tool_name == "reports_list" and worker_id:
        result.setdefault("worker_id", worker_id)
    if tool_name == "test_start":
        result.setdefault("worker_id", worker_id)
        result.setdefault("devices", workspace.get("device_ids") or [])
        result.setdefault("test_suite", workspace.get("suite_path") or "")
    if tool_name in {"reports_analyze", "reports_download", "reports_delete"}:
        timestamp = workspace.get("report_timestamp") or workspace.get("report_id") or ""
        if timestamp:
            result.setdefault("report_timestamp", timestamp)
            result.setdefault("timestamp", timestamp)
    return result


def _missing_required_params(tool: Any, params: dict[str, Any]) -> list[dict[str, Any]]:
    """Return required tool parameters that still have no usable value."""
    if not tool:
        return []
    missing: list[dict[str, Any]] = []
    for definition in tool.params:
        if not definition.get("required"):
            continue
        name = str(definition.get("name") or "").strip()
        if not name:
            continue
        value = params.get(name)
        if value is None or (isinstance(value, str) and not value.strip()) or value == []:
            missing.append(definition)
    return missing


def _request_missing_params(session: dict[str, Any], missing: list[dict[str, Any]]) -> None:
    labels = [str(item.get("desc") or item.get("name") or "参数") for item in missing]
    _append_message(
        session,
        "assistant",
        "还缺少必要参数：" + "、".join(labels) + "。请补充后再执行。",
    )
    session["status"] = "idle"
    session["pending_plan"] = None
