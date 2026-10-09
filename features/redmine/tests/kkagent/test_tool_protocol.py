"""Regression: textual GLM tool syntax must not masquerade as missing MCP."""

import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest

from features.redmine.kkagent.analyzer import (
    KkAgentRedmineAnalyzer,
    _StreamFallback,
    classify_gate_failure,
)
from features.redmine.kkagent.errors import has_textual_gms_tool_call
from features.redmine.kkagent.mcp_health import McpHealthProbe
from features.redmine.kkagent.trace import KkAgentTrace, ToolTrace, consume_line


LEAKED_CALL = (
    "我先读取预采集的工单快照。"
    "mcp__gms__gms_rt_redmine_issue<arg_key>snapshot_id</arg_key>"
    "<arg_value>snap-123</arg_value></tool_call>"
)


def final_trace(message=LEAKED_CALL):
    trace = KkAgentTrace()
    consume_line(trace, json.dumps({
        "type": "result", "subtype": "success", "session_id": "same",
        "exit_code": 0, "message": message,
    }))
    return trace


@pytest.mark.parametrize("message", [
    LEAKED_CALL,
    LEAKED_CALL.replace("mcp__gms__", ""),
    LEAKED_CALL.replace("mcp__gms__", "mcp__gms_remote_test__"),
    LEAKED_CALL.split("snap-123")[0],
])
def test_leaked_tool_syntax_is_detected_without_native_calls(message):
    trace = final_trace(message)
    status, error_type, error = classify_gate_failure(trace, ["history missing"])
    assert status == error_type == "model_tool_protocol_error"
    assert "模型网关" in error
    assert "snap-123" not in error
    assert trace.tool_calls == []


@pytest.mark.parametrize("message", [
    None, {}, "请使用 gms_rt_redmine_issue 读取快照。",
    f"报告中的错误示例：`{LEAKED_CALL}`",
    f"报告中的错误示例：\n```text\n{LEAKED_CALL}\n```",
])
def test_prose_and_quoted_evidence_are_not_tool_protocol_failures(message):
    assert not has_textual_gms_tool_call(message)


def test_controller_preflight_does_not_mask_model_protocol_failure():
    trace = final_trace()
    trace.tool_calls = [ToolTrace(
        tool_call_id="preflight:issue", tool_name="gms_rt_redmine_issue_fetch",
        status="succeeded",
    )]
    assert classify_gate_failure(trace, ["history missing"])[1] == "model_tool_protocol_error"


@pytest.mark.parametrize("status", ["pending", "failed", "succeeded"])
def test_native_gms_calls_prevent_misclassifying_quoted_tool_syntax(status):
    trace = final_trace()
    trace.tool_calls = [ToolTrace(
        tool_call_id="native-issue", tool_name="mcp__gms__gms_rt_redmine_issue",
        status=status,
    )]
    assert classify_gate_failure(trace, ["history missing"])[1] != "model_tool_protocol_error"


@pytest.mark.parametrize("mode", ["diagnostic", "triage"])
def test_analysis_stops_without_resume_or_automatic_retry_on_protocol_failure(mode):
    analyzer = KkAgentRedmineAnalyzer()
    stream = AsyncMock(return_value=(final_trace(), _StreamFallback(), False))
    with patch.object(analyzer, "_run_stream", stream), patch(
        "features.redmine.kkagent.analyzer.probe_kkagent_mcp_health",
        AsyncMock(return_value=McpHealthProbe(ok=True)),
    ):
        outcome = asyncio.run(analyzer.analyze({"issue_id": 123, "analysis_mode": mode}))
    assert not outcome.ok
    assert outcome.result is None
    assert outcome.error_type == "model_tool_protocol_error"
    assert outcome.session_id == "same"
    assert outcome.trace["repair_attempts"] == 0
    stream.assert_awaited_once()


def test_repair_stops_after_first_protocol_failure_in_exact_session():
    analyzer = KkAgentRedmineAnalyzer()
    stream = AsyncMock(side_effect=[
        (final_trace("## 关键结论\n- [待验证] 尚无历史证据。"), _StreamFallback(), False),
        (final_trace(), _StreamFallback(), False),
    ])
    with patch.object(analyzer, "_run_stream", stream), patch(
        "features.redmine.kkagent.analyzer.probe_kkagent_mcp_health",
        AsyncMock(return_value=McpHealthProbe(ok=True)),
    ):
        outcome = asyncio.run(analyzer.analyze({"issue_id": 123, "analysis_mode": "diagnostic"}))
    assert not outcome.ok
    assert outcome.error_type == "model_tool_protocol_error"
    assert outcome.trace["repair_attempts"] == 1
    assert stream.await_count == 2
    command = stream.await_args_list[1].args[0]
    assert command[command.index("--resume") + 1] == "same"
