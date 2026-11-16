"""Official citations and factual revisions survive the publication pipeline."""

import json
from unittest.mock import AsyncMock

import pytest

from features.redmine.kkagent.analyzer import KkAgentRedmineAnalyzer, _StreamFallback
from features.redmine.kkagent.claim_review import review_prompt
from features.redmine.kkagent.evidence_gate import evaluate_evidence_gate, gate_errors
from features.redmine.kkagent.mcp_health import McpHealthProbe
from features.redmine.kkagent.native_summary import native_evidence_repair_prompt, native_summary_result
from features.redmine.kkagent.trace import ToolTrace
from features.redmine.reference_recall import reference_check_summary
from features.redmine.tests.kkagent.review_support import review_payload, review_trace
from features.redmine.tests.kkagent.test_native_summary import ENTRY, complete_trace, final_trace


REFERENCE = "ref:mainline:security_patch:observed"
OFFICIAL = {
    "snapshot_id": "snap-123", "issue_id": 123, "coverage_complete": True,
    "sources": {"security_patches": {"status": "matched", "items": [{
        "reference": REFERENCE, "cve_id": "CVE-2026-58835", "mainline_module": "Bluetooth",
        "version_note": "Fix ag/40830952 is included in the October Bluetooth module.",
        "source_url": "https://example.com/official-release",
    }]}},
}
SUMMARY_ENTRY = {**ENTRY, "output_style": "summary", "_official_reference_context": OFFICIAL}


def official_trace(report):
    trace = complete_trace(report=report)
    trace.tool_calls.append(ToolTrace(
        tool_name="gms_rt_redmine_references", tool_call_id="official", status="succeeded",
        tool_input={"snapshot_id": "snap-123"}, reference_lookup=reference_check_summary(OFFICIAL),
    ))
    return trace


def mock_stream(monkeypatch, analyzer, responses):
    stream = AsyncMock(side_effect=[(trace, _StreamFallback(), False) for trace in responses])
    monkeypatch.setattr(analyzer, "_run_stream", stream)
    monkeypatch.setattr("features.redmine.kkagent.analyzer.probe_kkagent_mcp_health",
                        AsyncMock(return_value=McpHealthProbe(ok=True, skipped=True)))
    return stream


def test_official_citation_accepts_the_actual_controller_evidence_id():
    report = "Mainline 发布记录列出 Bluetooth 修复；设备是否集成仍待验证（EV-006）。"
    trace = official_trace(report)
    result = native_summary_result(trace, SUMMARY_ENTRY)
    assert not any("omitted" in error for error in gate_errors(result["evidence_gate"], result))


@pytest.mark.parametrize("citation", ["EV-005", "EV-0060", "EV-999"])
def test_unrelated_or_invented_evidence_ids_do_not_satisfy_official_citations(citation):
    report = f"Mainline 已检查（{citation}）。"
    trace = official_trace(report)
    gate = evaluate_evidence_gate(trace, SUMMARY_ENTRY)
    assert any("omitted" in error for error in gate_errors(gate, {"detailed_report": report}))


def test_another_issues_official_call_does_not_satisfy_this_issues_citation():
    trace = official_trace("发布记录见 EV-007。")
    trace.tool_calls.append(ToolTrace(
        tool_name="gms_rt_redmine_references", status="succeeded", tool_call_id="other",
        tool_input={"snapshot_id": "snap-other"}, reference_lookup=reference_check_summary(OFFICIAL),
    ))
    gate = evaluate_evidence_gate(trace, SUMMARY_ENTRY)
    assert any("omitted" in error for error in gate_errors(gate, {"detailed_report": trace.final_event["message"]}))


@pytest.mark.asyncio
async def test_report_reference_gaps_are_repaired_before_claim_review(monkeypatch):
    analyzer = KkAgentRedmineAnalyzer()
    corrected = "官方发布记录列出修复，设备集成情况待验证（EV-006）。"
    stream = mock_stream(monkeypatch, analyzer, [
        official_trace("报告漏掉了官方资料。"), final_trace(report=corrected),
        review_trace(corrected, refs=["EV-006"]),
    ])
    outcome = await analyzer.analyze(SUMMARY_ENTRY)
    assert outcome.ok
    assert "skeptical evidence reviewer" not in stream.await_args_list[1].args[0][-1]
    assert "official" in stream.await_args_list[1].args[0][-1]
    assert "ag/40830952" in stream.await_args_list[1].args[0][-1]
    assert "skeptical evidence reviewer" in stream.await_args_list[2].args[0][-1]


def test_review_and_repair_receive_queried_content_not_only_reference_metadata():
    report = "是否已集成修复待验证（EV-006）。"
    trace = official_trace(report)
    result = native_summary_result(trace, SUMMARY_ENTRY)
    for prompt in (
        review_prompt(result, trace, SUMMARY_ENTRY),
        native_evidence_repair_prompt([], trace.evidence_ledger(), official_references=OFFICIAL),
    ):
        assert OFFICIAL["sources"]["security_patches"]["items"][0]["version_note"] in prompt
        assert "not proof" in prompt


@pytest.mark.asyncio
async def test_factual_revision_has_a_budget_after_output_repairs(monkeypatch):
    analyzer = KkAgentRedmineAnalyzer()
    bad = "SPL 声明保持 2026-10-01。"
    fixed = "实际 SPL 未知，先读取构建的 security_patch 属性再确定复测条件。"
    rejection = review_payload(bad)
    rejection.update(status="needs_revision", findings=["SPL 尚未读取，不能预设实际声明值。"])
    initial = complete_trace(report=bad)
    initial.last_round_visible_output = False
    empty = final_trace()
    empty.last_round_visible_output = False
    entry = {**ENTRY, "output_style": "summary"}
    stream = mock_stream(monkeypatch, analyzer, [
        initial, empty, final_trace(report=bad), final_trace(report=json.dumps(rejection)),
        final_trace(report=fixed), review_trace(fixed),
    ])
    outcome = await analyzer.analyze(entry)
    assert outcome.ok and outcome.result["detailed_report"] == fixed
    assert outcome.trace["analysis_review_findings"] == []
    assert "SPL 尚未读取" in stream.await_args_list[4].args[0][-1]
    assert stream.await_count == 6


@pytest.mark.asyncio
async def test_repeated_factual_revision_is_bounded_and_never_published(monkeypatch):
    analyzer = KkAgentRedmineAnalyzer()
    bad = "SPL 保持未核实的日期。"
    rejection = review_payload(bad)
    rejection.update(status="needs_revision", findings=["实际 SPL 未核实。"])
    responses = [complete_trace(report=bad), final_trace(report=json.dumps(rejection))]
    for _ in range(2):
        responses.extend([final_trace(report=bad), final_trace(report=json.dumps(rejection))])
    stream = mock_stream(monkeypatch, analyzer, responses)
    outcome = await analyzer.analyze({**ENTRY, "output_style": "summary"})
    assert not outcome.ok and "实际 SPL 未核实" in outcome.error
    assert stream.await_count == 6


@pytest.mark.asyncio
async def test_new_report_does_not_inherit_an_older_reports_review_findings(monkeypatch):
    analyzer = KkAgentRedmineAnalyzer()
    trace = official_trace("旧报告")
    trace.tool_calls = [call for call in trace.tool_calls if "sdk" not in call.tool_name]
    trace.analysis_review_findings = ["旧报告预设了 SPL。"]
    stream = AsyncMock(return_value=(final_trace(report="SPL 待核实，但未引用官方资料。"), _StreamFallback(), False))
    monkeypatch.setattr(analyzer, "_run_stream", stream)
    outcome, merged = await analyzer._repair({**SUMMARY_ENTRY, "subject": "BTS warning"}, trace,
                                             trace.analysis_review_findings)
    assert outcome is None
    assert merged.analysis_review_findings == []
    assert stream.await_count <= 4
