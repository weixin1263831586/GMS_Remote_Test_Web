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

# 测试类失败（CTS/VTS/GTS/STS/LTP/ITS）subject 的关键词：命中即要求至少
# 一次成功的源码级取证调用（gms_rt_sdk_* / gms_rt_apk_*），否则"内核缺
# 补丁"式根因方向无法与"上游行为变更 + 测试期望过时"区分开。
TEST_FAILURE_SUBJECT_KEYWORDS = (
    "CTS", "VTS", "GTS", "STS", "LTP", "ITS", "MTBF",
)

EVIDENCE_GATE_JSON_KEY = "evidence_gate"


def is_test_failure_subject(entry: dict[str, Any]) -> bool:
    """快照 subject 是否命中测试套件类失败（决定源码取证门禁是否生效）。"""
    subject = str(entry.get("subject") or "").upper()
    return any(keyword in subject for keyword in TEST_FAILURE_SUBJECT_KEYWORDS)


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
    test_failure_subject = is_test_failure_subject(entry)
    source_evidence_tool_count = trace.source_evidence_tool_count
    # 部署未配置任何 SDK 源时降级放行：模型会尝试 sdk/apk 取证并收到
    # "source 未配置"的失败，此时强制 gate 只会系统性烧光轮次。降级依据
    # 是部署事实（entry 注入的 runtime hint），不是模型自报。hint 缺失
    # 按 fail-safe 处理（视为要求取证）；只有显式 False 才降级。
    triage = entry.get("analysis_mode") == "triage"
    source_evidence_required = not triage and test_failure_subject and (
        entry.get("sdk_sources_available") is not False
    )
    return {
        "analysis_mode": "triage" if triage else "diagnostic",
        "history_search_required": not triage,
        "issue_fetched": any(
            "redmine_issue_fetch" in name or "redmine_issue" in name
            for name in successful
        ),
        "journals_checked": any("redmine_journals" in name for name in successful),
        "test_failure_subject": test_failure_subject,
        "source_evidence_required": source_evidence_required,
        "source_evidence_tool_count": source_evidence_tool_count,
        "source_evidence_checked": (
            not source_evidence_required or source_evidence_tool_count >= 1
        ),
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
    if gate.get("history_search_required", True) and count < MIN_HISTORY_SEARCHES:
        errors.append(
            f"history search only used {count} distinct successful query/queries; "
            f"at least {MIN_HISTORY_SEARCHES} distinct history searches are required"
        )
    if gate.get("source_evidence_required") and not gate.get("source_evidence_checked"):
        errors.append(
            "test-suite failure: no successful source-level evidence call was made "
            "(gms_rt_sdk_* or gms_rt_apk_*); the kernel-missing-patch vs "
            "upstream-behavior-change directions must be distinguished with source "
            "evidence before claiming a root cause"
        )
    if isinstance(result, dict):
        if gate.get("analysis_mode") == "triage":
            if result.get("root_cause_type") != "unknown":
                errors.append("daily triage must leave root_cause_type unknown; request separate diagnosis")
            if result.get("similar_issues"):
                errors.append("daily triage must not include historical case diagnoses")
            if len(str(result.get("detailed_report") or "")) > 300:
                errors.append("daily triage detailed_report must be at most 300 characters")
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
    "TEST_FAILURE_SUBJECT_KEYWORDS",
    "apply_gate",
    "evaluate_evidence_gate",
    "gate_and_errors",
    "gate_errors",
    "is_test_failure_subject",
]
