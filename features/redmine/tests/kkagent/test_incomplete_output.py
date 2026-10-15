"""A successful CLI envelope can still contain only stale progress text."""

import asyncio
from unittest.mock import AsyncMock, patch

from features.redmine.kkagent.analyzer import KkAgentRedmineAnalyzer, _StreamFallback
from features.redmine.kkagent.mcp_health import McpHealthProbe
from features.redmine.kkagent.native_summary import native_summary_result
from features.redmine.kkagent.trace import consume_event
from features.redmine.tests.kkagent.test_context_budget import OVERFLOW, config_home
from features.redmine.tests.kkagent.test_native_summary import ENTRY, REPORT, complete_trace


def silent_last_round(report="继续读取补丁正文，然后输出终版报告。"):
    trace = complete_trace(report=report)
    consume_event(trace, {"type": "turn_start"})
    consume_event(trace, {
        "type": "usage", "usage": {"input_tokens": 17962, "output_tokens": 4096},
    })
    consume_event(trace, {"type": "turn_end"})
    return trace


def health_probe():
    return patch(
        "features.redmine.kkagent.analyzer.probe_kkagent_mcp_health",
        AsyncMock(return_value=McpHealthProbe(ok=True, skipped=True)),
    )


def test_success_envelope_cannot_reuse_a_report_from_an_earlier_round():
    trace = silent_last_round(report=REPORT)
    assert trace.final_event["subtype"] == "success"
    assert native_summary_result(trace, ENTRY) is None
    assert trace.to_summary()["last_round_output_tokens"] == 4096
    assert trace.to_summary()["last_round_visible_output"] is False


def test_visible_progress_is_not_a_diagnostic_report():
    trace = complete_trace(report="继续读取附件，随后立即输出最终报告。")
    assert native_summary_result(trace, ENTRY) is None


def test_silent_last_round_is_repaired_in_the_same_session_without_repeating_evidence():
    analyzer = KkAgentRedmineAnalyzer()
    repaired = complete_trace()
    consume_event(repaired, {"type": "turn_start"})
    consume_event(repaired, {"type": "message", "role": "assistant", "content": REPORT})
    consume_event(repaired, {"type": "usage", "usage": {"output_tokens": 1500}})
    stream = AsyncMock(side_effect=[
        (silent_last_round(), _StreamFallback(), False),
        (repaired, _StreamFallback(), False),
    ])
    with patch.object(analyzer, "_run_stream", stream), health_probe():
        outcome = asyncio.run(analyzer.analyze(ENTRY))
    assert outcome.ok
    assert outcome.result["detailed_report"] == REPORT
    assert outcome.trace["last_round_output_tokens"] == 1500
    assert stream.await_count == 2
    command = stream.await_args_list[1].args[0]
    assert command[command.index("--resume") + 1] == "same"
    assert "last round output: 4096 tokens" in command[-1]
    assert "Do not repeat evidence calls that already succeeded" in command[-1]


def test_no_report_after_bounded_repairs_has_a_specific_failure_class():
    analyzer = KkAgentRedmineAnalyzer()
    stream = AsyncMock(side_effect=[
        (silent_last_round(), _StreamFallback(), False) for _ in range(3)
    ])
    with patch.object(analyzer, "_run_stream", stream), health_probe():
        outcome = asyncio.run(analyzer.analyze(ENTRY))
    assert not outcome.ok
    assert outcome.error_type == "incomplete_ai_output"
    assert outcome.session_id == "same"
    assert stream.await_count == 3


def test_context_overflow_during_report_repair_reaches_context_recovery(tmp_path):
    config_home(tmp_path)
    analyzer = KkAgentRedmineAnalyzer(model="test", env_extra={"HOME": str(tmp_path)})
    raw = _StreamFallback()
    raw.feed_stderr(OVERFLOW)
    overflow = complete_trace()
    overflow.exit_code = 1
    stream = AsyncMock(side_effect=[
        (silent_last_round(), _StreamFallback(), False),
        (overflow, raw, False),
        (complete_trace(), _StreamFallback(), False),
    ])
    with patch.object(analyzer, "_run_stream", stream), health_probe():
        outcome = asyncio.run(analyzer.analyze(ENTRY))
    assert outcome.ok
    assert stream.await_count == 3
    assert "--config" in stream.await_args_list[2].args[0]
    assert stream.await_args_list[2].args[0][-1].count("DIAGNOSTIC REASONING REQUIREMENTS") == 0


def test_last_round_usage_survives_authoritative_cumulative_usage():
    trace = silent_last_round()
    consume_event(trace, {
        "type": "result", "subtype": "success", "message": "继续读取附件。",
        "usage": {"input_tokens": 107120, "output_tokens": 5100},
    })
    assert trace.output_tokens == 5100
    assert trace.last_round_output_tokens == 4096
    assert trace.last_round_visible_output is False
