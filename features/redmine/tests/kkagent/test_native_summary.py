"""Unbudgeted diagnosis preserves final Markdown answers."""

import asyncio
from unittest.mock import AsyncMock, patch

from features.redmine.kkagent.analyzer import KkAgentRedmineAnalyzer, _StreamFallback
from features.redmine.kkagent.native_summary import native_summary_result
from features.redmine.kkagent.trace import KkAgentTrace


ENTRY = {"issue_id": 123, "subject": "CTS fail", "analysis_mode": "diagnostic"}
REPORT = "## 分析结论\n\n当前证据不足以确认根因，请补充失败日志。\n"


def final_trace(session="same", report=REPORT):
    return KkAgentTrace(session_id=session, exit_code=0, final_event={
        "type": "result", "subtype": "success", "exit_code": 0,
        "session_id": session, "message": report,
    })


def test_native_report_is_preserved_without_invented_labels():
    result = native_summary_result(final_trace(), ENTRY)
    assert result["detailed_report"] == REPORT
    assert result["needs_human_review"]
    assert "confidence" not in result
    assert "root_cause_type" not in result


def test_partial_or_failed_answer_is_not_a_summary():
    trace = final_trace()
    trace.final_event["subtype"] = "max_turns"
    assert native_summary_result(trace, ENTRY) is None
    assert native_summary_result(KkAgentTrace(), ENTRY) is None


def test_diagnostic_prompt_requests_native_answer_and_correct_cli_signatures():
    prompt = KkAgentRedmineAnalyzer().build_prompt(ENTRY)
    assert "no JSON" in prompt
    assert "gms-rt-redmine-journals <snapshot_id>" in prompt
    assert "There is no step" in prompt
    assert "matching this schema" not in prompt


def test_diagnostic_prompt_sets_read_only_boundary_and_cli_fallback_stop():
    """#653167 nightly 复盘：MCP 缺失时模型曾执行 gms-agent install 并把
    轮次烧在诊断环境上。prompt 必须钉死只读边界与 CLI 兜底快停策略。"""
    prompt = KkAgentRedmineAnalyzer().build_prompt(ENTRY)
    assert "READ-ONLY ANALYSIS BOUNDARY" in prompt
    assert "gms-agent install" in prompt
    assert "a finding, not something to fix in-session" in prompt
    assert "GMS MCP/CLI 取证不可用" in prompt


def test_native_summary_needs_no_schema_or_evidence_repair():
    analyzer = KkAgentRedmineAnalyzer()
    stream = AsyncMock(return_value=(final_trace(), _StreamFallback(), False))
    with patch.object(analyzer, "_run_stream", stream):
        outcome = asyncio.run(analyzer.analyze(ENTRY))
    assert outcome.ok
    assert outcome.result["detailed_report"] == REPORT
    assert stream.await_count == 1


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
