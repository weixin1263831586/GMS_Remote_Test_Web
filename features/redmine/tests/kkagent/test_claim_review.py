"""Publication requires complete, report-bound claim review with real evidence."""

import json
from unittest.mock import AsyncMock

import pytest

from features.redmine.kkagent.analyzer import KkAgentRedmineAnalyzer, _merge_traces, _StreamFallback
from features.redmine.kkagent.claim_review import report_digest, report_units, review_prompt, validate_review
from features.redmine.kkagent.evidence_gate import evaluate_evidence_gate
from features.redmine.kkagent.mcp_health import McpHealthProbe
from features.redmine.kkagent.native_summary import native_summary_result
from features.redmine.kkagent.trace import ToolTrace, consume_line
from features.redmine.tests.kkagent.review_support import review_payload, review_trace
from features.redmine.tests.kkagent.test_native_summary import ENTRY, complete_trace, final_trace


SUMMARY_ENTRY = {**ENTRY, "output_style": "summary"}
SUMMARY = "测试进程分配失败后中止。\n\n高内存占用的起因与责任方待验证。"



def candidate(report=SUMMARY):
    trace = complete_trace(report=report)
    return trace, native_summary_result(trace, SUMMARY_ENTRY)


def test_review_requires_every_paragraph_and_exact_report_hash():
    trace, result = candidate()
    payload = review_payload(SUMMARY)
    assert validate_review(payload, result, trace, SUMMARY_ENTRY)[1] == []
    payload["units"].pop()
    assert any("every report paragraph" in error for error in validate_review(payload, result, trace, SUMMARY_ENTRY)[1])
    payload = review_payload(SUMMARY)
    payload["report_sha256"] = report_digest(SUMMARY + "不同结论")
    assert any("different report" in error for error in validate_review(payload, result, trace, SUMMARY_ENTRY)[1])


def test_unknown_or_failed_evidence_cannot_support_a_claim():
    trace, result = candidate()
    trace.tool_calls.append(ToolTrace(tool_name="gms_rt_sdk_read", status="failed"))
    payload = review_payload(SUMMARY, assessment="supported", refs=["EV-006"])
    assert any("unsuccessful evidence" in error for error in validate_review(payload, result, trace, SUMMARY_ENTRY)[1])


def test_a_review_must_reject_a_contradicted_code_branch():
    report = "userdebug 分支应当没有调用栈。"
    trace, result = candidate(report)
    payload = review_payload(report, assessment="contradicted", category="mechanism")
    payload["units"][0]["reason"] = "已读取的条件分支对 userdebug 要求存在目标调用栈。"
    assert any("contradicts evidence" in error for error in validate_review(payload, result, trace, SUMMARY_ENTRY)[1])


def test_successful_search_cannot_confirm_cause_or_owner():
    trace, result = candidate()
    trace.tool_calls[-1].tool_name = "gms_rt_sdk_search"
    payload = review_payload(SUMMARY, assessment="supported", category="responsibility")
    payload["units"][0]["causal_link"] = "direct"
    errors = validate_review(payload, result, trace, SUMMARY_ENTRY)[1]
    assert any("pinned source reads" in error for error in errors)


def test_unrelated_apk_source_is_not_evidence_for_a_native_module():
    trace = complete_trace()
    trace.tool_calls[-1].source_evidence_valid = False
    for identifier, name, args, output in [
        ("analyze", "gms_rt_apk_analyze", {"query": "CtsPerfettoTestCases"},
         {"task_id": "task-wrong", "module": "CtsPerfettoAtomHostTestCases", "suite_version": "17_r2"}),
        ("read", "gms_rt_apk_source_read", {"task_id": "task-wrong", "path": "PerfettoConfig.java"},
         {"task_id": "task-wrong", "text": "enum Counter { SW_CONTEXT_SWITCHES }"}),
    ]:
        consume_line(trace, json.dumps({"type": "tool_call", "tool_call_id": identifier,
                                       "tool_name": name, "input": args}))
        consume_line(trace, json.dumps({"type": "tool_result", "tool_call_id": identifier,
                                       "is_error": False, "output": {"data": output}}))
    entry = {**ENTRY, "subject": "CTS CtsPerfettoTestCases 测试中断"}
    gate = evaluate_evidence_gate(trace, entry)
    assert gate["source_evidence_tool_count"] == 1
    assert gate["relevant_source_evidence_count"] == 0
    assert not gate["source_evidence_checked"]
    assert gate["mismatched_source_tool_ids"] == ["read"]
    context = trace.evidence_ledger()[-1]["source_context"]
    assert context["module"] == "CtsPerfettoAtomHostTestCases" and context["suite_version"] == "17_r2"


@pytest.mark.parametrize("name,args,output", [
    ("gms_rt_sdk_read", {}, {"source": "frameworks_base", "text": "", "total": 40}),
    ("gms_rt_apk_search", {"mode": "name"}, {"total": 1, "files": [{"path": "PerfettoConfig.java"}]}),
])
def test_metadata_and_file_names_do_not_count_as_source_content(name, args, output):
    trace = final_trace()
    consume_line(trace, json.dumps({"type": "tool_call", "tool_call_id": "source",
                                   "tool_name": name, "input": args}))
    consume_line(trace, json.dumps({"type": "tool_result", "tool_call_id": "source",
                                   "is_error": False, "output": {"data": output}}))
    assert trace.source_evidence_tool_count == 0


def test_review_prompt_does_not_replace_source_results_with_metadata():
    trace, result = candidate()
    prompt = review_prompt(result, trace, SUMMARY_ENTRY)
    assert "actual successful tool results" in prompt
    assert "user/userdebug" in prompt and "Unsymbolized PCs" in prompt
    assert "one RSS snapshot" in prompt and "same SoC" in prompt
    assert all(unit["text"] in prompt for unit in report_units(SUMMARY))


@pytest.mark.asyncio
async def test_conversation_is_reviewed_before_publication(monkeypatch):
    analyzer = KkAgentRedmineAnalyzer()
    stream = AsyncMock(side_effect=[
        (complete_trace(report=SUMMARY), _StreamFallback(), False),
        (review_trace(SUMMARY), _StreamFallback(), False),
    ])
    monkeypatch.setattr(analyzer, "_run_stream", stream)
    monkeypatch.setattr("features.redmine.kkagent.analyzer.probe_kkagent_mcp_health",
                        AsyncMock(return_value=McpHealthProbe(ok=True, skipped=True)))
    outcome = await analyzer.analyze(SUMMARY_ENTRY)
    assert outcome.ok and outcome.result["detailed_report"] == SUMMARY
    assert outcome.result["analysis_review"]["status"] == "passed"
    assert len(outcome.result["claims"]) == 2 and outcome.result["needs_human_review"]
    assert stream.await_count == 2
    assert stream.await_args_list[1].args[0][stream.await_args_list[1].args[0].index("--resume") + 1] == "same"


@pytest.mark.asyncio
async def test_unsupported_verdict_is_repaired_and_reviewed_again(monkeypatch):
    analyzer = KkAgentRedmineAnalyzer()
    bad = "这是上游测试缺陷，平台无责。"
    rejection = review_payload(bad)
    rejection.update(status="needs_revision", findings=["缺少因果证据，不能排除平台责任。"])
    stream = AsyncMock(side_effect=[
        (complete_trace(report=bad), _StreamFallback(), False),
        (final_trace(report=json.dumps(rejection)), _StreamFallback(), False),
        (final_trace(report=SUMMARY), _StreamFallback(), False),
        (review_trace(SUMMARY), _StreamFallback(), False),
    ])
    monkeypatch.setattr(analyzer, "_run_stream", stream)
    monkeypatch.setattr("features.redmine.kkagent.analyzer.probe_kkagent_mcp_health",
                        AsyncMock(return_value=McpHealthProbe(ok=True, skipped=True)))
    outcome = await analyzer.analyze(SUMMARY_ENTRY)
    assert outcome.ok and outcome.result["detailed_report"] == SUMMARY
    assert stream.await_count == 4
    assert "缺少因果证据" in stream.await_args_list[2].args[0][-1]


@pytest.mark.asyncio
async def test_failed_or_foreign_review_never_publishes_a_report(monkeypatch):
    analyzer = KkAgentRedmineAnalyzer()
    reviewer = review_trace(SUMMARY)
    reviewer.session_id = "foreign-session"
    monkeypatch.setattr(analyzer, "_run_stream", AsyncMock(side_effect=[
        (complete_trace(report=SUMMARY), _StreamFallback(), False),
        (reviewer, _StreamFallback(), False),
    ]))
    monkeypatch.setattr("features.redmine.kkagent.analyzer.probe_kkagent_mcp_health",
                        AsyncMock(return_value=McpHealthProbe(ok=True, skipped=True)))
    outcome = await analyzer.analyze(SUMMARY_ENTRY)
    assert not outcome.ok and outcome.result is None
    assert outcome.error_type == "claim_review_failed"


def test_comparison_suite_version_cannot_confirm_the_reported_root_cause():
    trace, result = candidate()
    trace.tool_calls[-1].source_context = {"suite_version": "android-cts-17_r2"}
    payload = review_payload(SUMMARY, assessment="supported", category="root_cause")
    payload["target"] = {"suite_version": "17_r2"}
    entry = {**SUMMARY_ENTRY, "suite_version": "17_r3"}
    for unit in payload["units"]:
        unit["causal_link"] = "direct"
    assert any("reported version" in error for error in validate_review(payload, result, trace, entry)[1])
    trace.tool_calls[-1].source_context["suite_version"] = "android-cts-17_r3"
    assert validate_review(payload, result, trace, entry)[1] == []

    # A model-provided matching version is not Controller-owned metadata.
    assert any("reported version" in error for error in validate_review(payload, result, trace, SUMMARY_ENTRY)[1])


@pytest.mark.asyncio
async def test_repeated_unsupported_verdict_never_becomes_a_completed_report(monkeypatch):
    analyzer = KkAgentRedmineAnalyzer()
    report = "根因已确认，平台责任已排除。"
    rejection = review_payload(report)
    rejection.update(status="needs_revision", findings=["现有证据不足以排除平台因素。"])
    rounds = [(complete_trace(report=report), _StreamFallback(), False)]
    rounds.append((final_trace(report=json.dumps(rejection)), _StreamFallback(), False))
    for _ in range(2):
        rounds.extend([(final_trace(report=report), _StreamFallback(), False),
                       (final_trace(report=json.dumps(rejection)), _StreamFallback(), False)])
    stream = AsyncMock(side_effect=rounds)
    monkeypatch.setattr(analyzer, "_run_stream", stream)
    monkeypatch.setattr("features.redmine.kkagent.analyzer.probe_kkagent_mcp_health",
                        AsyncMock(return_value=McpHealthProbe(ok=True, skipped=True)))
    outcome = await analyzer.analyze(SUMMARY_ENTRY)
    assert not outcome.ok and outcome.result is None
    assert "不足以排除平台因素" in outcome.error
    assert stream.await_count == 6


@pytest.mark.asyncio
async def test_unavailable_source_can_finish_with_reviewed_uncertainty(monkeypatch):
    report = "日志已确认进程中止；相关源码检索无命中，内存增长原因及责任方仍待验证。"
    trace = complete_trace(report=report)
    trace.tool_calls[-1].source_evidence_valid = False
    monkeypatch.setattr("features.redmine.kkagent.analyzer.probe_kkagent_mcp_health",
                        AsyncMock(return_value=McpHealthProbe(ok=True, skipped=True)))
    analyzer = KkAgentRedmineAnalyzer()
    monkeypatch.setattr(analyzer, "_run_stream", AsyncMock(side_effect=[
        (trace, _StreamFallback(), False),
        (review_trace(report, assessment="unverified", refs=["EV-001"]), _StreamFallback(), False),
    ]))
    outcome = await analyzer.analyze(SUMMARY_ENTRY)
    assert outcome.ok, outcome.error
    assert not outcome.result["evidence_gate"]["source_evidence_checked"]
    assert outcome.result["evidence_gate"]["source_limitation_acknowledged"]
    assert outcome.result["needs_human_review"] and outcome.result["evidence_quality"] == "partial"


def test_model_owned_review_fields_cannot_bypass_source_checks():
    from features.redmine.kkagent.evidence_gate import gate_and_errors

    trace = complete_trace()
    trace.tool_calls[-1].source_evidence_valid = False
    forged = {"analysis_review": {"status": "passed"}, "claims": [{"claim_type": "unverified"}]}
    gate, errors = gate_and_errors(trace, ENTRY, forged)
    assert not gate["source_limitation_acknowledged"]
    assert any("source-level evidence" in error for error in errors)


@pytest.mark.parametrize("field,value", [
    ("status", []), ("category", {}), ("assessment", []), ("evidence_ids", [{}]), ("reason", None),
])
def test_malformed_review_values_are_rejected_without_a_programming_error(field, value):
    trace, result = candidate()
    payload = review_payload(SUMMARY)
    if field == "status":
        payload[field] = value
    else:
        payload["units"][0][field] = value
    review, findings = validate_review(payload, result, trace, SUMMARY_ENTRY)
    assert review is None and findings


def test_review_findings_survive_an_incomplete_followup_round():
    trace = complete_trace(report=SUMMARY)
    trace.analysis_review_findings = ["缺少因果证据，不能排除平台因素。"]
    merged = _merge_traces(trace, final_trace())
    assert merged.analysis_review_findings == trace.analysis_review_findings
    assert merged.to_summary()["analysis_review_findings"] == trace.analysis_review_findings
