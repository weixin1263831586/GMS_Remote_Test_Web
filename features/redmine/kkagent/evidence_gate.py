"""Runtime Evidence Gate：用真实 tool trace 校验晨报取证要求。

审核意见（P1）：history_checked 等字段目前主要靠模型自报
（bool("false") == True 的静默转换更是直接放水）。本模块从 kkagent
stream-json 轨迹里统计**真实发生**的 MCP 调用，把 evidence 判定从
Prompt Policy 升级成 Runtime Policy：

- issue_fetched / journals_checked：对应工具必须真实成功调用过；
- attachments_checked：快照里带附件时必须列出附件；
- distinct_history_search_count / history_checked：强制 2 个不同历史查询；
- history_checked 字段由运行时覆写，模型自报值被丢弃。
- similar_issues：每个 ID 必须来自成功的历史检索或 issue fetch 结果。
"""

from __future__ import annotations

from typing import Any

from .trace import KkAgentTrace


# Prompt 要求 2-4 次独立历史检索；运行时下限 2 次。
MIN_HISTORY_SEARCHES = 2

EVIDENCE_GATE_JSON_KEY = "evidence_gate"


def evaluate_evidence_gate(
    trace: KkAgentTrace, entry: dict[str, Any]
) -> dict[str, Any]:
    """从轨迹推导取证事实；绝不读取模型自报的 history_checked。"""
    successful = trace.successful_tool_names()
    history_count = trace.history_search_count
    distinct_history_count = trace.distinct_history_search_count
    attachment_count = 0
    try:
        attachment_count = int(entry.get("attachment_count") or 0)
    except (TypeError, ValueError):
        attachment_count = 0
    attachment_calls = [
        call
        for call in trace.tool_calls
        if call.succeeded and "redmine_attachments" in call.tool_name
    ]
    attachment_manifest_parsed = any(
        call.attachment_manifest_parsed for call in attachment_calls
    )
    listed_attachment_count = max(
        [call.attachment_count for call in attachment_calls] + [0]
    )
    unread_text_artifact_ids = sorted(
        trace.listed_text_artifact_ids() - trace.read_artifact_ids()
    )
    attachments_checked = attachment_count == 0 or (
        bool(attachment_calls)
        and attachment_manifest_parsed
        and listed_attachment_count >= attachment_count
        and not unread_text_artifact_ids
    )
    return {
        "issue_fetched": any(
            "redmine_issue_fetch" in name or "redmine_issue" in name
            for name in successful
        ),
        "journals_checked": any("redmine_journals" in name for name in successful),
        "attachments_listed": bool(attachment_calls),
        "attachment_manifest_parsed": attachment_manifest_parsed,
        "listed_attachment_count": listed_attachment_count,
        "unread_text_artifact_ids": unread_text_artifact_ids,
        "attachments_checked": attachments_checked,
        "history_search_count": history_count,
        "distinct_history_search_count": distinct_history_count,
        "history_checked": distinct_history_count >= MIN_HISTORY_SEARCHES,
        "allowed_similar_issue_ids": sorted(
            issue_id
            for issue_id in trace.evidenced_issue_ids()
            if issue_id != _positive_int(entry.get("issue_id"))
        ),
        "session_id": trace.session_id,
        "tool_call_count": len(trace.tool_calls),
    }


def _positive_int(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return parsed if parsed > 0 else 0


def gate_errors(
    gate: dict[str, Any], result: dict[str, Any] | None = None
) -> list[str]:
    """Gate 未满足的可读错误列表（作为 resume 修复的输入）。"""
    errors: list[str] = []
    if not gate.get("issue_fetched"):
        errors.append("issue was not fetched (gms_rt_redmine_issue_fetch missing)")
    if not gate.get("journals_checked"):
        errors.append("journals were not checked (gms_rt_redmine_journals missing)")
    if not gate.get("attachments_checked"):
        if not gate.get("attachments_listed"):
            errors.append(
                "issue has attachments but gms_rt_redmine_attachments was never called"
            )
        elif not gate.get("attachment_manifest_parsed"):
            errors.append("attachment manifest result was not structured JSON")
        elif gate.get("unread_text_artifact_ids"):
            errors.append(
                "text attachments were listed but not read: "
                + ", ".join(str(value) for value in gate["unread_text_artifact_ids"])
            )
        else:
            errors.append("attachment manifest contained fewer items than the issue snapshot")
    count = int(gate.get("distinct_history_search_count") or 0)
    if count < MIN_HISTORY_SEARCHES:
        errors.append(
            f"history search only used {count} distinct successful query/queries; "
            f"at least {MIN_HISTORY_SEARCHES} distinct history searches are required"
        )
    if isinstance(result, dict):
        allowed = {
            _positive_int(value)
            for value in gate.get("allowed_similar_issue_ids") or []
        }
        claimed = {
            _positive_int(item.get("issue_id"))
            for item in result.get("similar_issues") or []
            if isinstance(item, dict)
        }
        unsupported = sorted(value for value in claimed if value and value not in allowed)
        if unsupported:
            errors.append(
                "similar issue IDs are not present in successful history/fetch evidence: "
                + ", ".join(str(value) for value in unsupported)
            )
    return errors


def apply_gate(result: dict[str, Any], gate: dict[str, Any]) -> None:
    """把运行时判定写回结果：history_checked 覆写 + gate 存档。"""
    result["history_checked"] = bool(gate.get("history_checked"))
    result[EVIDENCE_GATE_JSON_KEY] = gate


def gate_and_errors(
    trace: KkAgentTrace, entry: dict[str, Any], result: dict[str, Any]
) -> tuple[dict[str, Any], list[str]]:
    """一次性：评估 gate → 写回 result → 返回 (gate, errors)。"""
    gate = evaluate_evidence_gate(trace, entry)
    apply_gate(result, gate)
    return gate, gate_errors(gate, result)


__all__ = [
    "EVIDENCE_GATE_JSON_KEY",
    "MIN_HISTORY_SEARCHES",
    "apply_gate",
    "evaluate_evidence_gate",
    "gate_and_errors",
    "gate_errors",
]
