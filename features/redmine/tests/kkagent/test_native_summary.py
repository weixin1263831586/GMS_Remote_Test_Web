"""Unbudgeted diagnosis preserves final Markdown answers."""

import asyncio
from unittest.mock import AsyncMock, patch

from features.redmine.kkagent.analyzer import KkAgentRedmineAnalyzer, _StreamFallback
from features.redmine.kkagent.mcp_health import McpHealthProbe
from features.redmine.kkagent.native_summary import native_evidence_repair_prompt, native_summary_result
from features.redmine.kkagent.trace import KkAgentTrace, ToolTrace


ENTRY = {"issue_id": 123, "subject": "CTS fail", "analysis_mode": "diagnostic"}
REPORT = (
    "## 关键结论\n\n"
    "- [推断][file:PackageSignatureTest.java] 当前证据不足以确认根因，请补充失败日志。\n"
)


def final_trace(session="same", report=REPORT):
    return KkAgentTrace(session_id=session, exit_code=0, final_event={
        "type": "result", "subtype": "success", "exit_code": 0,
        "session_id": session, "message": report,
    })


def complete_trace(session="same", report=REPORT):
    trace = final_trace(session, report)
    trace.tool_calls = [
        ToolTrace(
            tool_call_id="issue", tool_name="gms_rt_redmine_issue_fetch",
            tool_input={"issue_id": 123}, status="succeeded",
            evidence_issue_ids=[123], snapshot_ids=["snap-123"],
        ),
        ToolTrace(
            tool_call_id="journals", tool_name="gms_rt_redmine_journals",
            tool_input={"snapshot_id": "snap-123"}, status="succeeded",
        ),
        ToolTrace(
            tool_call_id="history-1", tool_name="gms_rt_redmine_history_search",
            tool_input={"query": "PackageSignatureTest"}, status="succeeded",
        ),
        ToolTrace(
            tool_call_id="history-2", tool_name="gms_rt_redmine_history_search",
            tool_input={"query": "well known key"}, status="succeeded",
        ),
        ToolTrace(
            tool_call_id="source", tool_name="gms_rt_sdk_read",
            tool_input={"source_id": "test", "path": "PackageSignatureTest.java"}, status="succeeded",
            source_reproducible=True, source_evidence_valid=True,
        ),
    ]
    return trace


def test_native_report_is_preserved_without_invented_labels():
    result = native_summary_result(final_trace(), ENTRY)
    assert result["detailed_report"] == REPORT
    assert result["needs_human_review"]
    assert result["confidence"] == 0.45
    assert "root_cause_type" not in result


def test_native_claims_are_bound_to_controller_evidence_ids():
    result = native_summary_result(complete_trace(), ENTRY)
    assert result["claims"] == [{
        "claim_type": "inference",
        "text": "当前证据不足以确认根因，请补充失败日志。",
        "evidence_ids": ["EV-005"],
    }]
    assert result["claim_binding_errors"] == []
    assert result["execution_status"] == "completed"
    assert result["evidence_quality"] == "verified"


def test_observed_snapshot_and_artifact_references_resolve_to_existing_evidence():
    trace = complete_trace(report="## 关键结论\n\n- [事实][art_real,ev_real] 已读取日志。\n")
    trace.tool_calls[0].snapshot_ids = ["ev_real"]
    trace.tool_calls.append(ToolTrace(
        tool_name="gms_rt_redmine_artifact_read", tool_input={"artifact_id": "art_real"},
        status="succeeded",
    ))
    result = native_summary_result(trace, ENTRY)
    assert result["claim_binding_errors"] == []
    assert result["claims"][0]["evidence_ids"] == ["EV-006", "EV-001"]
    assert result["confidence_source"] == "controller_evidence_gate"


def test_failed_or_unobserved_references_are_never_accepted():
    trace = complete_trace(report="## 关键结论\n\n- [事实][art_failed,ev_unknown,EV-006] 错误引用。\n")
    trace.tool_calls.append(ToolTrace(
        tool_name="gms_rt_redmine_artifact_read", tool_input={"artifact_id": "art_failed"},
        status="failed",
    ))
    result = native_summary_result(trace, ENTRY)
    assert result["claim_binding_errors"]
    assert result["needs_human_review"]


def test_model_cannot_number_tools_and_accidentally_cite_an_existing_ev_id():
    trace = complete_trace(report="## 关键结论\n\n- [事实][EV-001] 测试源码已证明根因。\n")
    result = native_summary_result(trace, ENTRY)
    assert result['claim_binding_errors']
    assert result['needs_human_review']
    indexed = native_summary_result(trace, {**ENTRY, '_controller_evidence_ids': ['EV-001']})
    assert indexed['claim_binding_errors'] == []


def test_source_and_manifest_references_bind_to_the_actual_successful_tools():
    trace = complete_trace(report="## 关键结论\n\n- [事实][src:task:Test.java,apk:task:manifest] 已读取代码与清单。\n")
    trace.tool_calls.extend([
        ToolTrace(tool_name='gms_rt_apk_source_read', status='succeeded',
                  tool_input={'task_id': 'task', 'path': 'Test.java'}, source_evidence_valid=True),
        ToolTrace(tool_name='gms_rt_apk_manifest', status='succeeded', tool_input={'task_id': 'task'}),
    ])
    result = native_summary_result(trace, ENTRY)
    assert result['claim_binding_errors'] == []
    assert result['claims'][0]['evidence_ids'] == ['EV-006', 'EV-007']


def test_doctor_and_generic_tools_cannot_support_business_claims_even_with_an_ev_id():
    trace = complete_trace(report="## 关键结论\n\n- [事实][EV-006] 测试源码已验证。\n")
    trace.tool_calls.append(ToolTrace(tool_name='mcp_doctor', status='succeeded'))
    result = native_summary_result(trace, {**ENTRY, '_controller_evidence_ids': ['EV-006']})
    assert result['claim_binding_errors']
    prompt = native_evidence_repair_prompt([], trace.evidence_ledger())
    assert '"evidence_id": "EV-006"' not in prompt


def test_repair_accepts_controller_ids_only_after_providing_their_real_mapping():
    analyzer = KkAgentRedmineAnalyzer()
    trace = complete_trace(report="## 关键结论\n\n- [事实][EV-005] 已读取代码。\n")
    stream = AsyncMock(side_effect=[
        (trace, _StreamFallback(), False),
        (final_trace(report=trace.final_event['message']), _StreamFallback(), False),
    ])
    with patch.object(analyzer, '_run_stream', stream), patch(
        'features.redmine.kkagent.analyzer.probe_kkagent_mcp_health',
        AsyncMock(return_value=McpHealthProbe(ok=True, skipped=True)),
    ):
        outcome = asyncio.run(analyzer.analyze(ENTRY))
    assert outcome.ok
    assert stream.await_count == 2
    assert '"evidence_id": "EV-005"' in stream.await_args_list[1].args[0][-1]
    assert 'file:PackageSignatureTest.java' in stream.await_args_list[1].args[0][-1]


def test_repair_prompt_carries_a_bounded_successful_evidence_index():
    trace = complete_trace()
    trace.tool_calls[0].snapshot_ids = ["ev_real"]
    prompt = native_evidence_repair_prompt(["unknown EV-009"], trace.evidence_ledger())
    assert '"evidence_id": "EV-001"' in prompt
    assert '"ev_real"' in prompt
    assert '"evidence_id": "EV-009"' not in prompt
    assert "Never invent" in prompt


def test_partial_or_failed_answer_is_not_a_summary():
    trace = final_trace()
    trace.final_event["subtype"] = "max_turns"
    assert native_summary_result(trace, ENTRY) is None
    assert native_summary_result(KkAgentTrace(), ENTRY) is None


def test_diagnostic_prompt_requests_native_answer_and_correct_cli_signatures():
    prompt = KkAgentRedmineAnalyzer().build_prompt(ENTRY)
    assert "no JSON" in prompt
    assert "never run gms-rt CLI through Bash" in prompt
    assert "mcp__codesearch" in prompt
    assert "There is no step" in prompt
    assert "matching this schema" not in prompt


def test_diagnostic_prompt_sets_read_only_boundary_and_cli_fallback_stop():
    """#653167 nightly 复盘：MCP 缺失时模型曾执行 gms-agent install 并把
    轮次烧在诊断环境上。prompt 必须钉死只读边界与 CLI 兜底快停策略。"""
    prompt = KkAgentRedmineAnalyzer().build_prompt(ENTRY)
    assert "READ-ONLY ANALYSIS BOUNDARY" in prompt
    assert "gms-agent install" in prompt
    assert "a finding, not something to fix in-session" in prompt
    assert "GMS MCP 取证不可用" in prompt


def test_diagnostic_prompt_requires_causal_evidence_and_the_reported_suite_version():
    prompt = KkAgentRedmineAnalyzer().build_prompt(ENTRY)
    assert 'before the assertion' in prompt
    assert 'crash/kill stack' in prompt
    assert 'Resolve the EXACT reported suite version' in prompt
    assert 'not extracted from not present' in prompt
    assert 'number tools yourself' in prompt


def test_native_summary_passes_without_repair_when_evidence_gate_is_complete():
    analyzer = KkAgentRedmineAnalyzer()
    stream = AsyncMock(return_value=(complete_trace(), _StreamFallback(), False))
    with patch.object(analyzer, "_run_stream", stream), patch(
        "features.redmine.kkagent.analyzer.probe_kkagent_mcp_health",
        AsyncMock(return_value=McpHealthProbe(ok=True, skipped=True)),
    ):
        outcome = asyncio.run(analyzer.analyze(ENTRY))
    assert outcome.ok
    assert outcome.result["detailed_report"] == REPORT
    assert not outcome.result["needs_human_review"]
    assert outcome.result["confidence"] == 0.85
    assert stream.await_count == 1


def test_native_summary_repairs_missing_evidence_in_same_session():
    analyzer = KkAgentRedmineAnalyzer()
    repaired_report = "## 关键结论\n\n- [事实][file:PackageSignatureTest.java] 证据已补齐。\n"
    stream = AsyncMock(side_effect=[
        (final_trace(), _StreamFallback(), False),
        (complete_trace(report=repaired_report), _StreamFallback(), False),
    ])
    with patch.object(analyzer, "_run_stream", stream), patch(
        "features.redmine.kkagent.analyzer.probe_kkagent_mcp_health",
        AsyncMock(return_value=McpHealthProbe(ok=True, skipped=True)),
    ):
        outcome = asyncio.run(analyzer.analyze(ENTRY))
    assert outcome.ok
    assert outcome.result["detailed_report"] == repaired_report
    assert not outcome.result["needs_human_review"]
    assert outcome.result["confidence"] == 0.85
    assert stream.await_count == 2
    repair_command = stream.await_args_list[1].args[0]
    assert "--resume" in repair_command
    assert "native gms_rt_* MCP tools directly" in repair_command[-1]
    assert "no JSON" in repair_command[-1]


def test_native_summary_cannot_complete_when_gate_stays_open():
    analyzer = KkAgentRedmineAnalyzer()
    stream = AsyncMock(side_effect=[
        (final_trace(), _StreamFallback(), False),
        (final_trace(), _StreamFallback(), False),
        (final_trace(), _StreamFallback(), False),
    ])
    with patch.object(analyzer, "_run_stream", stream), patch(
        "features.redmine.kkagent.analyzer.probe_kkagent_mcp_health",
        AsyncMock(return_value=McpHealthProbe(ok=True, skipped=True)),
    ):
        outcome = asyncio.run(analyzer.analyze(ENTRY))
    assert not outcome.ok
    assert outcome.error_type == "mcp_evidence_unavailable"
    assert stream.await_count == 3


def test_native_summary_without_reproducible_source_requires_human_review():
    trace = complete_trace()
    source = next(call for call in trace.tool_calls if "sdk_read" in call.tool_name)
    source.source_reproducible = False

    result = native_summary_result(trace, ENTRY)

    assert result["evidence_gate"]["source_evidence_checked"]
    assert result["confidence"] == 0.58
    assert result["needs_human_review"]


def test_raw_audit_output_is_not_cut_to_twenty_thousand_characters():
    analyzer = KkAgentRedmineAnalyzer()
    raw = _StreamFallback()
    raw.feed_stdout("x" * 25_000)

    outcome = analyzer._success(
        {"detailed_report": REPORT}, complete_trace(), raw
    )

    assert len(outcome.raw_output) == 25_000


def test_analysis_and_repair_commands_have_no_step_limit():
    analyzer = KkAgentRedmineAnalyzer()
    assert analyzer.timeout_seconds == 0
    for command in (analyzer.build_command("analysis"), analyzer.build_repair_command("repair", "same")):
        assert "--max-turns" not in command


def test_failed_turn_is_not_forced_into_a_partial_summary():
    analyzer = KkAgentRedmineAnalyzer()
    exhausted = KkAgentTrace(session_id="same", exit_code=3, final_event={"subtype": "max_turns"})
    stream = AsyncMock(return_value=(exhausted, _StreamFallback(), False))
    with patch.object(analyzer, "_run_stream", stream):
        outcome = asyncio.run(analyzer.analyze(ENTRY))
    assert not outcome.ok
    assert outcome.error_type == "max_turns"
    assert outcome.session_id == "same"
    assert stream.await_count == 1


def test_cancellation_does_not_attempt_budget_recovery():
    analyzer = KkAgentRedmineAnalyzer()
    stream = AsyncMock(side_effect=asyncio.CancelledError)
    with patch.object(analyzer, "_run_stream", stream):
        try:
            asyncio.run(analyzer.analyze(ENTRY))
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("cancellation must propagate")
    assert stream.await_count == 1
