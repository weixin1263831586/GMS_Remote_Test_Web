"""Preserve the final diagnostic answer without a model-owned JSON schema."""

from __future__ import annotations

from typing import Any

from .evidence_gate import evaluate_evidence_gate, gate_errors
from .output import envelope_result
from .trace import KkAgentTrace


def native_summary_result(
    trace: KkAgentTrace, entry: dict[str, Any]
) -> dict[str, Any] | None:
    """Only a successful final envelope is an answer, never intermediate text."""
    event = trace.final_event
    if not isinstance(event, dict) or event.get("subtype") != "success":
        return None
    if event.get("exit_code", 0) != 0:
        return None
    structured = envelope_result(event)
    report = structured.get("detailed_report") if structured else event.get("message")
    if not isinstance(report, str) or not report.strip():
        return None
    gate = evaluate_evidence_gate(trace, entry)
    return {
        "result_format": "kkagent_markdown",
        # 独立键：native 摘要不是 IssueResult schema 的 v3（字段集完全
        # 不同），不复用同名版本字段误导消费方（审核意见 P3）。
        "native_summary_version": 1,
        "problem_summary": str(entry.get("subject") or ""),
        "detailed_report": report,
        "evidence_gate": gate,
        "history_checked": bool(gate.get("history_checked")),
        "needs_human_review": bool(gate_errors(gate)),
    }
