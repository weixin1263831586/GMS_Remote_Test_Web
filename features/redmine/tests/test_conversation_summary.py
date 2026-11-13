"""Conversation summaries survive the queue without the diagnostic layout."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from features.redmine import daily_brief_service
from features.redmine.kkagent import KkAgentAnalysisResult
from features.redmine.kkagent.analyzer import KkAgentRedmineAnalyzer, _StreamFallback
from features.redmine.kkagent.mcp_health import McpHealthProbe
from features.redmine.kkagent.native_summary import native_summary_result
from features.redmine.tests.kkagent.review_support import review_trace
from features.redmine.tests.kkagent.test_native_summary import ENTRY, complete_trace, final_trace
from features.redmine.tests.test_daily_brief_service import make_service


SUMMARY = "当前证据指向构建签名配置。请先核对签名并复测；尚未验证修复效果。"
SUMMARY_ENTRY = {**ENTRY, "output_style": "summary"}


def test_summary_prompt_keeps_evidence_rules_without_report_chapters():
    analyzer = KkAgentRedmineAnalyzer()
    prompt = analyzer.build_prompt(SUMMARY_ENTRY)
    assert "own final summary directly" in prompt
    assert "at least two distinct history" in prompt
    assert "READ-ONLY ANALYSIS BOUNDARY" in prompt
    assert "untrusted evidence" in prompt
    assert "## 关键结论" not in prompt
    assert "AUDITABLE CLAIM CONTRACT" not in prompt
    assert "## 工单概况" not in prompt
    assert "## 问题概况" in analyzer.build_prompt(ENTRY)


@pytest.mark.parametrize("summary", [
    SUMMARY, "先给结论：" + SUMMARY + "\n\n# 补充说明\n无需固定章节。", '{"结论":"核对构建签名"}',
])
def test_final_summary_is_preserved_without_headings_or_claim_bullets(summary):
    result = native_summary_result(complete_trace(report=summary), SUMMARY_ENTRY)
    assert result["detailed_report"] == summary
    assert result["output_style"] == "summary"
    assert result["claim_binding_errors"] == []
    # The existing page still requires its diagnostic report structure.
    assert native_summary_result(complete_trace(report=SUMMARY), ENTRY) is None


def test_summary_rejects_an_unobserved_or_unsuccessful_final_round():
    trace = complete_trace(report=SUMMARY)
    trace.last_round_visible_output = False
    assert native_summary_result(trace, SUMMARY_ENTRY) is None
    trace.last_round_visible_output = True
    trace.final_event["subtype"] = "error"
    assert native_summary_result(trace, SUMMARY_ENTRY) is None


async def test_summary_completes_without_a_format_repair(monkeypatch):
    analyzer = KkAgentRedmineAnalyzer()
    stream = AsyncMock(side_effect=[(complete_trace(report=SUMMARY), _StreamFallback(), False),
                                   (review_trace(SUMMARY), _StreamFallback(), False)])
    monkeypatch.setattr(analyzer, "_run_stream", stream)
    monkeypatch.setattr("features.redmine.kkagent.analyzer.probe_kkagent_mcp_health",
                        AsyncMock(return_value=McpHealthProbe(ok=True, skipped=True)))
    result = await analyzer.analyze(SUMMARY_ENTRY)
    assert result.ok, result.error
    assert result.result["detailed_report"] == SUMMARY
    assert result.result["evidence_gate"]["issue_fetched"]
    assert stream.await_count == 2


async def test_summary_repairs_do_not_restore_report_headings(monkeypatch):
    analyzer = KkAgentRedmineAnalyzer()
    first = complete_trace(report="正在读取日志")
    first.last_round_visible_output = False
    stream = AsyncMock(side_effect=[
        (first, _StreamFallback(), False),
        (final_trace(report=""), _StreamFallback(), False),
        (final_trace(report=SUMMARY), _StreamFallback(), False),
        (review_trace(SUMMARY), _StreamFallback(), False),
    ])
    monkeypatch.setattr(analyzer, "_run_stream", stream)
    monkeypatch.setattr("features.redmine.kkagent.analyzer.probe_kkagent_mcp_health",
                        AsyncMock(return_value=McpHealthProbe(ok=True, skipped=True)))
    result = await analyzer.analyze(SUMMARY_ENTRY)
    assert result.ok, result.error
    assert result.result["detailed_report"] == SUMMARY
    assert stream.await_count == 4
    for call in stream.await_args_list[1:]:
        command = call.args[0]
        assert command[command.index("--resume") + 1] == "same"
        assert "## 关键结论" not in command[-1]
        assert "`# ` title" not in command[-1]


async def test_summary_still_fails_when_required_evidence_is_missing(monkeypatch):
    analyzer = KkAgentRedmineAnalyzer()
    stream = AsyncMock(side_effect=[
        (final_trace(report=SUMMARY), _StreamFallback(), False) for _ in range(3)
    ])
    monkeypatch.setattr(analyzer, "_run_stream", stream)
    monkeypatch.setattr("features.redmine.kkagent.analyzer.probe_kkagent_mcp_health",
                        AsyncMock(return_value=McpHealthProbe(ok=True, skipped=True)))
    result = await analyzer.analyze(SUMMARY_ENTRY)
    assert not result.ok
    assert result.error_type == "mcp_evidence_unavailable"


def test_queue_keeps_summary_separate_from_page_analysis(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    monkeypatch.setattr(service, "_owner_is_eligible", lambda: True)
    page = service.start_issue_analysis(123)
    chat = service.start_issue_analysis(123, output_style="summary")
    assert page["run_id"] != chat["run_id"]
    assert service.repository.get_run(chat["run_id"]).output_style == "summary"
    assert service.start_issue_analysis(123)["run_id"] == page["run_id"]
    assert service.start_issue_analysis(123, output_style="summary")["run_id"] == chat["run_id"]
    full = service.start_issue_analysis(123, analysis_mode="full", output_style="summary")
    assert full["run_id"] not in {chat["run_id"], page["run_id"]}
    assert service.repository.get_run(full["run_id"]).output_style == "summary"
    assert service.repository.latest_issue_run("u1", 123, output_style="diagnostic").run_id == page["run_id"]


async def test_reloaded_summary_run_reaches_analyzer_and_persists_unchanged(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    monkeypatch.setattr(service, "_owner_is_eligible", lambda: True)
    queued = service.start_issue_analysis(123, output_style="summary")
    run = service.repository.get_run(queued["run_id"])
    analyzer = SimpleNamespace(analyze=AsyncMock(return_value=KkAgentAnalysisResult(
        ok=True, result={"detailed_report": SUMMARY, "output_style": "summary"},
    )))
    monkeypatch.setattr("features.redmine.api.get_redmine_service_for_owner", lambda _owner: SimpleNamespace(
        repository=SimpleNamespace(get_issue=lambda issue_id: {"issue_id": issue_id}),
    ))
    monkeypatch.setattr(daily_brief_service, "precollect_deep_evidence", AsyncMock())
    await service._analyze_one(run, 123, {"issue_id": 123}, analyzer, {})
    assert analyzer.analyze.await_args.args[0]["output_style"] == "summary"
    result = service.repository.get_issue(run.run_id, 123)
    assert result.status == "completed"
    assert result.result["detailed_report"] == SUMMARY
