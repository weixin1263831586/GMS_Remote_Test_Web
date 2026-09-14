"""Runtime Evidence Gate：用真实 tool trace 校验晨报取证要求。

审核意见（P1）：history_checked 等字段目前主要靠模型自报
（bool("false") == True 的静默转换更是直接放水）。本模块从 kkagent
stream-json 轨迹里统计**真实发生**的 MCP 调用，把 evidence 判定从
Prompt Policy 升级成 Runtime Policy：

- issue_fetched / journals_checked：对应工具必须真实成功调用过；
- attachments_checked：快照里带附件时必须列出附件；
- history_search_count / history_checked：强制 2 次以上历史检索；
- history_checked 字段由运行时覆写，模型自报值被丢弃。
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
    attachment_count = 0
    try:
        attachment_count = int(entry.get("attachment_count") or 0)
    except (TypeError, ValueError):
        attachment_count = 0
    return {
        "issue_fetched": any(
            "redmine_issue_fetch" in name or "redmine_issue" in name
            for name in successful
        ),
        "journals_checked": any("redmine_journals" in name for name in successful),
        "attachments_checked": (
            attachment_count == 0
            or any("redmine_attachments" in name for name in successful)
        ),
        "history_search_count": history_count,
        "history_checked": history_count >= MIN_HISTORY_SEARCHES,
        "session_id": trace.session_id,
        "tool_call_count": len(trace.tool_calls),
    }


def gate_errors(gate: dict[str, Any]) -> list[str]:
    """Gate 未满足的可读错误列表（作为 resume 修复的输入）。"""
    errors: list[str] = []
    if not gate.get("issue_fetched"):
        errors.append("issue was not fetched (gms_rt_redmine_issue_fetch missing)")
    if not gate.get("journals_checked"):
        errors.append("journals were not checked (gms_rt_redmine_journals missing)")
    if not gate.get("attachments_checked"):
        errors.append(
            "issue has attachments but gms_rt_redmine_attachments was never called"
        )
    count = int(gate.get("history_search_count") or 0)
    if count < MIN_HISTORY_SEARCHES:
        errors.append(
            f"history search only executed {count} time(s); "
            f"at least {MIN_HISTORY_SEARCHES} distinct history searches are required"
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
    return gate, gate_errors(gate)


__all__ = [
    "EVIDENCE_GATE_JSON_KEY",
    "MIN_HISTORY_SEARCHES",
    "apply_gate",
    "evaluate_evidence_gate",
    "gate_and_errors",
    "gate_errors",
]
