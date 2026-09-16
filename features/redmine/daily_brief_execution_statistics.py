"""Aggregate sanitized Daily Brief AI execution traces for the UI."""

from __future__ import annotations

from typing import Any


DEFAULT_MODEL_LABEL = "kkagent 默认模型（未记录具体名称）"


def _number(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def summarize_execution_statistics(
    executions: list[dict[str, Any]],
    *,
    fallback_model: str = "",
) -> dict[str, Any]:
    """Summarize persisted trace metadata without exposing tool inputs/output."""
    fallback = str(fallback_model or "").strip() or DEFAULT_MODEL_LABEL
    tokens = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,
        "total_tokens": 0,
    }
    timing = {
        "total_duration_ms": 0,
        "average_duration_ms": 0,
        "measured_execution_count": 0,
    }
    models: dict[str, dict[str, Any]] = {}
    tools: dict[str, dict[str, Any]] = {}
    issue_ids: set[int] = set()

    for execution in executions:
        if not isinstance(execution, dict):
            continue
        issue_id = _number(execution.get("issue_id"))
        if issue_id:
            issue_ids.add(issue_id)
        model_name = str(execution.get("model_name") or fallback).strip() or fallback
        model = models.setdefault(model_name, {
            "model_name": model_name,
            "execution_count": 0,
            "issue_ids": set(),
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_tokens": 0,
            "cache_creation_tokens": 0,
        })
        model["execution_count"] += 1
        if issue_id:
            model["issue_ids"].add(issue_id)

        duration_keys = ("wall_duration_ms", "duration_ms")
        if any(key in execution for key in duration_keys):
            timing["measured_execution_count"] += 1
            timing["total_duration_ms"] += _number(
                execution.get("wall_duration_ms") or execution.get("duration_ms")
            )

        for key in (
            "input_tokens", "output_tokens", "cache_read_tokens",
            "cache_creation_tokens",
        ):
            value = _number(execution.get(key))
            tokens[key] += value
            model[key] += value

        for tool in execution.get("tools") or []:
            if not isinstance(tool, dict):
                continue
            tool_name = str(tool.get("tool_name") or "").strip()
            if not tool_name.startswith("gms_rt_"):
                continue
            row = tools.setdefault(tool_name, {
                "tool_name": tool_name,
                "call_count": 0,
                "succeeded_count": 0,
                "failed_count": 0,
                "pending_count": 0,
                "issue_ids": set(),
            })
            row["call_count"] += 1
            if issue_id:
                row["issue_ids"].add(issue_id)
            status = str(tool.get("status") or "").lower()
            if status == "succeeded" or (
                not status and not tool.get("is_error")
                and bool(tool.get("output_sha256"))
            ):
                row["succeeded_count"] += 1
            elif status == "failed" or tool.get("is_error"):
                row["failed_count"] += 1
            else:
                row["pending_count"] += 1

    tokens["total_tokens"] = tokens["input_tokens"] + tokens["output_tokens"]
    if timing["measured_execution_count"]:
        timing["average_duration_ms"] = round(
            timing["total_duration_ms"] / timing["measured_execution_count"]
        )
    model_rows = []
    for model in models.values():
        row = {key: value for key, value in model.items() if key != "issue_ids"}
        row["issue_count"] = len(model["issue_ids"])
        row["total_tokens"] = row["input_tokens"] + row["output_tokens"]
        model_rows.append(row)
    model_rows.sort(key=lambda row: (-row["execution_count"], row["model_name"]))

    tool_rows = []
    recommendations = []
    for tool in tools.values():
        row = {key: value for key, value in tool.items() if key != "issue_ids"}
        row["issue_count"] = len(tool["issue_ids"])
        row["failure_rate"] = (
            round(row["failed_count"] / row["call_count"], 4)
            if row["call_count"] else 0
        )
        tool_rows.append(row)
        if row["failed_count"]:
            recommendations.append({
                "tool_name": row["tool_name"],
                "kind": "reliability",
                "message": (
                    f"{row['failed_count']}/{row['call_count']} 次调用失败；"
                    "优先补充失败码、参数校验和重试指引。"
                ),
            })
        elif row["call_count"] >= max(3, row["issue_count"] * 2):
            recommendations.append({
                "tool_name": row["tool_name"],
                "kind": "efficiency",
                "message": (
                    f"覆盖 {row['issue_count']} 个单号却调用 {row['call_count']} 次；"
                    "评估批量查询、结果缓存或提示词去重。"
                ),
            })
    tool_rows.sort(key=lambda row: (-row["call_count"], row["tool_name"]))
    recommendations.sort(key=lambda row: (row["kind"] != "reliability", row["tool_name"]))

    return {
        "execution_count": len(executions),
        "issue_count": len(issue_ids),
        "tokens": tokens,
        "timing": timing,
        "models": model_rows,
        "gms_tool_call_count": sum(row["call_count"] for row in tool_rows),
        "gms_tools": tool_rows,
        "tool_improvement_recommendations": recommendations[:8],
    }


__all__ = ["DEFAULT_MODEL_LABEL", "summarize_execution_statistics"]
