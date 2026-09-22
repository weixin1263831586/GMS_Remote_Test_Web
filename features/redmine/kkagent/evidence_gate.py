"""Runtime Evidence Gate：用真实 tool trace 校验晨报取证要求。

history_checked 等字段目前主要靠模型自报
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

import re
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


def result_analysis_mode(result: Any) -> str:
    """历史结果 → "triage" / "diagnostic"。

    优先读取持久化的 evidence_gate.analysis_mode；无 gate 的历史结果按
    triage 的特征标记（root_cause 为空/占位且 root_cause_type 为 unknown）
    判定，其余视为 diagnostic。
    """
    if not isinstance(result, dict):
        return "triage"
    gate = result.get(EVIDENCE_GATE_JSON_KEY)
    if isinstance(gate, dict) and gate.get("analysis_mode"):
        return "triage" if gate.get("analysis_mode") == "triage" else "diagnostic"
    root_cause = str(result.get("root_cause") or "").strip()
    if root_cause in {"", "未进行深度诊断"} and result.get("root_cause_type", "unknown") == "unknown":
        return "triage"
    return "diagnostic"


def is_test_failure_subject(entry: dict[str, Any]) -> bool:
    """快照 subject 是否命中测试套件类失败（决定源码取证门禁是否生效）。"""
    subject = str(entry.get("subject") or "").upper()
    return any(keyword in subject for keyword in TEST_FAILURE_SUBJECT_KEYWORDS)


def bind_claims(
    result: dict[str, Any], ledger: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """把模型声称的 evidence 条目绑定回真实证据 ID。

    每条声称的 evidence 用 source/reference/fact 拼接成可匹配文本，与 ledger
    每条证据的引用 token（issue/artifact/snapshot id、路径、查询词）做包含
    匹配，返回每条 claim 绑定的 evidence_id 列表。空 refs 的 ledger 条目
    不参与匹配，避免"匹配一切"。
    """
    bindings: list[dict[str, Any]] = []
    for item in result.get("evidence") or []:
        if not isinstance(item, dict):
            continue
        haystack = " ".join(
            str(item.get(key) or "") for key in ("source", "reference", "fact")
        ).lower()
        bound: list[str] = []
        for entry in ledger:
            tokens = [str(ref).lower() for ref in entry.get("refs") or [] if str(ref)]
            matched = False
            for token in tokens:
                if not token:
                    continue
                if token.isdigit() or token.lstrip("#").isdigit():
                    # 数字引用按数字边界匹配：#1000 不得被子串匹配误绑到
                    # #100 的证据（伪造/笔误引用不应算「已证实」）；不带
                    # # 的裸数字引用也按同样边界匹配，防止反向漏绑。
                    digits = token.lstrip("#")
                    if re.search(rf"(?<!\d){digits}(?!\d)", haystack):
                        matched = True
                        break
                elif token in haystack:
                    matched = True
                    break
            if matched:
                bound.append(str(entry["evidence_id"]))
        bindings.append({
            "reference": str(item.get("reference") or "")[:120],
            "evidence_ids": bound,
        })
    return bindings


def evaluate_evidence_gate(
    trace: KkAgentTrace, entry: dict[str, Any]
) -> dict[str, Any]:
    """从轨迹推导取证事实；绝不读取模型自报的 history_checked。

    所有取证判定都按 ``target_issue_id`` 归属——分析
    #100 时，对相似工单 #200 的 journals/attachments 调用不能当作 #100
    自己已取证。归属依据为调用输入的 issue_id/snapshot_id 与返回结构里
    的 snapshot_id/artifact_id 关联（见 ``_targets_current``）。
    """
    target_issue_id = _positive_int(entry.get("issue_id"))
    successful = trace.successful_tool_names()
    history_count = trace.history_search_count
    distinct_history_count = trace.distinct_history_search_count
    attachment_count = 0
    try:
        attachment_count = int(entry.get("attachment_count") or 0)
    except (TypeError, ValueError):
        attachment_count = 0

    def _targets_current(call: Any) -> bool:
        """单次调用是否针对当前 issue（逐条判断，不做全局归属兜底）。

        归属依据（按优先级）：
        1. 输入显式带 issue_id；
        2. 输入/输出携带当前 issue 的 snapshot_id（issue_fetch 返回过）；
        3. artifact_read 的 artifact_id 由针对当前 issue 的 attachments
           调用列出过。
        """
        if not target_issue_id:
            return True  # 无目标 issue 时保持旧行为
        for raw in (call.tool_input.get("issue_id"), call.tool_input.get("issue")):
            if _positive_int(raw) == target_issue_id:
                return True
        snapshots = _snapshot_issue_ids(trace, target_issue_id)
        if snapshots:
            if str(call.tool_input.get("snapshot_id") or "") in snapshots:
                return True
            if any(sid in snapshots for sid in call.snapshot_ids):
                return True
        artifact_id = str(call.tool_input.get("artifact_id") or "").strip()
        if artifact_id and artifact_id in current_artifact_ids:
            return True
        return False

    current_artifact_ids: set[str] = set()
    if target_issue_id:
        snapshots = _snapshot_issue_ids(trace, target_issue_id)
        for call in trace.tool_calls:
            if not (call.succeeded and "redmine_attachments" in call.tool_name):
                continue
            if str(call.tool_input.get("snapshot_id") or "") in snapshots or any(
                _positive_int(call.tool_input.get(key)) == target_issue_id
                for key in ("issue_id", "issue")
            ):
                current_artifact_ids.update(call.all_artifact_ids)
    attachment_calls = [
        call
        for call in trace.tool_calls
        if call.succeeded and "redmine_attachments" in call.tool_name
        and _targets_current(call)
    ]
    attachment_manifest_parsed = any(
        call.attachment_manifest_parsed for call in attachment_calls
    )
    listed_attachment_count = max(
        [call.attachment_count for call in attachment_calls] + [0]
    )
    unread_text_artifact_ids = sorted(
        trace.listed_text_artifact_ids(attachment_calls)
        - trace.read_artifact_ids()
    )
    attachments_checked = attachment_count == 0 or (
        bool(attachment_calls)
        and attachment_manifest_parsed
        and listed_attachment_count >= attachment_count
        and not unread_text_artifact_ids
    )
    test_failure_subject = is_test_failure_subject(entry)
    source_evidence_tool_count = trace.source_evidence_tool_count
    reproducible_source_evidence_count = trace.reproducible_source_evidence_count
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
            ("redmine_issue_fetch" in name or "redmine_issue" in name)
            for name in successful
        ) and (
            not target_issue_id
            # issue_fetch 必须针对当前 issue（相似单的 fetch 不算数）：
            # 命中输入 issue_id，或返回结构里出现当前 issue_id。
            or any(
                call.succeeded
                and "redmine_issue_fetch" in call.tool_name
                and (
                    any(
                        _positive_int(call.tool_input.get(key)) == target_issue_id
                        for key in ("issue_id", "issue")
                    )
                    or target_issue_id in set(call.evidence_issue_ids)
                )
                for call in trace.tool_calls
            )
        ),
        "journals_checked": any(
            "redmine_journals" in name for name in successful
        ) and (
            not target_issue_id
            or any(
                call.succeeded
                and "redmine_journals" in call.tool_name
                and _targets_current(call)
                for call in trace.tool_calls
            )
        ),
        "test_failure_subject": test_failure_subject,
        "source_evidence_required": source_evidence_required,
        "source_evidence_tool_count": source_evidence_tool_count,
        "reproducible_source_evidence_count": reproducible_source_evidence_count,
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
        "evidence_ledger": [
            {
                "evidence_id": item["evidence_id"],
                "kind": item["kind"],
                "reproducible": item["reproducible"],
            }
            for item in trace.evidence_ledger()
        ],
        "session_id": trace.session_id,
        "tool_call_count": len(trace.tool_calls),
    }


def _positive_int(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return parsed if parsed > 0 else 0


def _snapshot_issue_ids(trace: KkAgentTrace, target_issue_id: int) -> set[str]:
    """当前 issue 的 issue_fetch 调用返回过的 snapshot_id 集合。"""
    target = _positive_int(target_issue_id)
    if not target:
        return set()
    ids: set[str] = set()
    for call in trace.tool_calls:
        if not (call.succeeded and "redmine_issue_fetch" in call.tool_name):
            continue
        if any(
            _positive_int(call.tool_input.get(key)) == target
            for key in ("issue_id", "issue")
        ):
            ids.update(call.snapshot_ids)
    return ids


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
            # artifact_id 来自不可信 Redmine 附件清单；拼接前清洗换行/控
            # 制字符，防止把 findings 渲染成新的 prompt 指令行（注入面）。
            cleaned = ", ".join(
                "".join(
                    ch for ch in str(value) if ch.isprintable() and ch not in "\n\r\t"
                )[:80]
                for value in gate["unread_text_artifact_ids"]
            )
            errors.append("text attachments were listed but not read: " + cleaned)
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
        if (
            result.get("root_cause_type") == "confirmed"
            and not gate.get("reproducible_source_evidence_count")
        ):
            errors.append(
                "root_cause_type=confirmed requires at least one reproducible "
                "source-level evidence call (local-git provider, "
                "reproducible=true); dynamic code-search index evidence "
                "(reproducible=false) cannot alone confirm a root cause — "
                "downgrade to 'likely' or obtain pinned-commit evidence"
            )
        elif result.get("root_cause_type") == "confirmed":
            # Claim 级绑定：轨迹里存在可复现源码证据还不够，
            # 模型声称的 evidence 必须真的引用了其中一条——否则"源码证据 A
            # + 无关根因 B"仍会被全局计数误判为已证实。
            reproducible_ids = {
                str(item.get("evidence_id"))
                for item in gate.get("evidence_ledger") or []
                if item.get("reproducible") is True
            }
            bindings = gate.get("claim_bindings") or []
            bound_to_reproducible = any(
                set(binding.get("evidence_ids") or []) & reproducible_ids
                for binding in bindings
            )
            if reproducible_ids and bindings and not bound_to_reproducible:
                errors.append(
                    "root_cause_type=confirmed claims evidence but none of the "
                    "cited evidence references the reproducible source call; "
                    "cite the source path/query of the reproducible evidence "
                    "in evidence[].reference, or downgrade to 'likely'"
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
    """一次性：评估 gate → 绑定 claim → 写回 result → 返回 (gate, errors)。"""
    gate = evaluate_evidence_gate(trace, entry)
    # Claim ↔ Evidence Ledger 绑定：把模型声称的 evidence
    # 绑定回稳定证据 ID，随 gate 一起存档；confirmed 级结论的引用必须
    # 命中可复现源码证据，而不是仅"轨迹里存在过一次"（gate_errors）。
    gate["claim_bindings"] = bind_claims(result, trace.evidence_ledger())
    apply_gate(result, gate)
    return gate, gate_errors(gate, result)


__all__ = [
    "EVIDENCE_GATE_JSON_KEY",
    "MIN_HISTORY_SEARCHES",
    "TEST_FAILURE_SUBJECT_KEYWORDS",
    "apply_gate",
    "bind_claims",
    "evaluate_evidence_gate",
    "gate_and_errors",
    "gate_errors",
    "is_test_failure_subject",
    "result_analysis_mode",
]
