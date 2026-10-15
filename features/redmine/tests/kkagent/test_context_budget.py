"""Provider context ceilings are applied without rewriting installed config."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from features.redmine.kkagent.analyzer import KkAgentRedmineAnalyzer, _StreamFallback
from features.redmine.kkagent.context_budget import context_limited_config, tomllib
from features.redmine.kkagent.errors import classify_failure, context_length_limit
from features.redmine.kkagent.mcp_health import McpHealthProbe
from features.redmine.tests.kkagent.test_native_summary import (
    ENTRY,
    complete_trace,
    final_trace,
)


OVERFLOW = 'The input (64005 tokens) is longer than the model\'s context length (16384 tokens).'
CONFIG = '''default_model = "test"
[models.test]
provider = "gateway"
model = "test"
max_context_size = 500000
max_output_size = 16000
[providers.gateway]
api_key = "synthetic-test-key"
[sandbox]
enabled = true
[mcp_servers.gms]
command = "gms-mcp"
[loop_control]
auto_compact = false
max_steps_per_turn = 0
'''


def config_home(tmp_path):
    root = tmp_path / ".kkagent"
    root.mkdir()
    path = root / "config.toml"
    path.write_text(CONFIG)
    return path


def test_context_overflow_is_classified_before_generic_failure_or_step_limit():
    kind, message = classify_failure(1, "", OVERFLOW, envelope_subtype="max_turns")
    assert kind == "context_limit_exceeded"
    assert "16384" in message
    assert context_length_limit(message) == 16384
    assert context_length_limit('maximum context length is 32768 tokens') == 32768
    assert context_length_limit('input uses 64005 tokens') is None
    assert context_length_limit('模型输入超过服务端上下文上限（16384 tokens）。') == 16384


def test_overlay_is_private_preserves_security_and_cleans_up_on_error(tmp_path):
    original = config_home(tmp_path)
    with pytest.raises(RuntimeError), context_limited_config("test", 16384, {"HOME": str(tmp_path)}) as path:
        assert path.stat().st_mode & 0o777 == 0o600
        assert path.parent.stat().st_mode & 0o777 == 0o700
        config = tomllib.loads(path.read_text())
        assert config['models']['test']['max_context_size'] == 16384
        assert config['models']['test']['max_output_size'] == 16000
        assert config['providers']['gateway']['api_key'] == 'synthetic-test-key'
        assert config['sandbox']['enabled']
        assert config['mcp_servers']['gms']['command'] == 'gms-mcp'
        assert config['loop_control']['auto_compact']
        assert config['loop_control']['max_steps_per_turn'] == 0
        raise RuntimeError("cancelled analysis")
    assert not path.exists()
    assert original.read_text() == CONFIG


def test_input_ceiling_does_not_invent_an_output_budget(tmp_path):
    original = config_home(tmp_path)
    original.write_text(CONFIG.replace('max_output_size = 16000\n', ''))
    with context_limited_config("test", 16384, {"HOME": str(tmp_path)}) as path:
        assert 'max_output_size' not in tomllib.loads(path.read_text())['models']['test']


@pytest.mark.parametrize("second_failure", [False, True])
def test_context_recovery_resumes_once_and_preserves_prior_evidence(tmp_path, second_failure):
    original = config_home(tmp_path)
    analyzer = KkAgentRedmineAnalyzer(model="test", env_extra={"HOME": str(tmp_path)})
    paths = []
    commands = []

    async def stream(command, *_args, **_kwargs):
        commands.append(command)
        if len(commands) == 1 or second_failure:
            trace = complete_trace(session="exact-session")
            trace.exit_code = 1
            trace.input_tokens = 100
            raw = _StreamFallback()
            raw.feed_stderr(OVERFLOW)
            if len(commands) > 1:
                paths.append(Path(command[command.index('--config') + 1]))
            return trace, raw, False
        path = Path(command[command.index('--config') + 1])
        paths.append(path)
        assert tomllib.loads(path.read_text())['models']['test']['max_context_size'] == 16384
        trace = final_trace(session="exact-session")
        trace.input_tokens = 20
        return trace, _StreamFallback(), False

    with patch.object(analyzer, '_run_stream', side_effect=stream), patch(
        'features.redmine.kkagent.analyzer.probe_kkagent_mcp_health',
        AsyncMock(return_value=McpHealthProbe(ok=True, skipped=True)),
    ):
        outcome = asyncio.run(analyzer.analyze(ENTRY))
    assert len(commands) == 2
    assert '--resume' not in commands[0]
    assert commands[1][commands[1].index('--resume') + 1] == 'exact-session'
    assert len(commands[1][-1]) < 1000
    assert "DIAGNOSTIC REASONING REQUIREMENTS" not in commands[1][-1]
    assert outcome.session_id == 'exact-session'
    assert outcome.ok is not second_failure
    if second_failure:
        assert outcome.error_type == 'context_limit_exceeded'
    else:
        assert outcome.trace['input_tokens'] == 120
        assert outcome.result['evidence_gate']['source_evidence_checked']
    assert all(not path.exists() for path in paths)
    assert original.read_text() == CONFIG


@pytest.mark.parametrize("exit_code,stderr", [
    (-15, ""),
    (1, "LLM stream error: operation timed out"),
    (1, "LLM stream error: The request queue is full."),
])
def test_context_recovery_retains_new_evidence_across_transient_retries(tmp_path, exit_code, stderr):
    original = config_home(tmp_path)
    analyzer = KkAgentRedmineAnalyzer(model="test", env_extra={"HOME": str(tmp_path)})
    first = complete_trace()
    source = first.tool_calls.pop()
    first.exit_code, first.input_tokens = 1, 100
    overflow = _StreamFallback()
    overflow.feed_stderr(OVERFLOW)
    interrupted = final_trace()
    interrupted.tool_calls = [source]
    interrupted.exit_code, interrupted.input_tokens = exit_code, 20
    interrupted_raw = _StreamFallback()
    interrupted_raw.feed_stderr(stderr)
    final = final_trace()
    final.input_tokens = 5
    stream = AsyncMock(side_effect=[
        (first, overflow, False), (interrupted, interrupted_raw, False),
        (final, _StreamFallback(), False),
        *[(final_trace(), _StreamFallback(), False) for _ in range(2)],
    ])
    with patch.object(analyzer, "_run_stream", stream), patch(
        "features.redmine.kkagent.analyzer.probe_kkagent_mcp_health",
        AsyncMock(return_value=McpHealthProbe(ok=True, skipped=True)),
    ):
        outcome = asyncio.run(analyzer.analyze(ENTRY))
    assert outcome.ok, outcome.error
    assert stream.await_count == 3
    assert outcome.trace["input_tokens"] == 125
    assert len(outcome.trace["tools"]) == 5
    assert outcome.result["evidence_gate"]["source_evidence_checked"]
    assert outcome.result["claim_binding_errors"] == []
    assert outcome.session_id == "same"
    for call in stream.await_args_list[1:]:
        command = call.args[0]
        assert command[command.index("--resume") + 1] == "same"
        assert not Path(command[command.index("--config") + 1]).exists()
    assert original.read_text() == CONFIG


def test_learned_ceiling_is_reused_for_later_issues(tmp_path):
    config_home(tmp_path)
    analyzer = KkAgentRedmineAnalyzer(model="test", env_extra={"HOME": str(tmp_path)})
    analyzer._observed_context_limit = 16384
    seen = []

    async def stream(command, *_args, **_kwargs):
        path = Path(command[command.index('--config') + 1])
        assert path.exists()
        seen.append(path)
        return complete_trace(), _StreamFallback(), False

    with patch.object(analyzer, '_run_stream', side_effect=stream), patch(
        'features.redmine.kkagent.analyzer.probe_kkagent_mcp_health',
        AsyncMock(return_value=McpHealthProbe(ok=True, skipped=True)),
    ):
        assert asyncio.run(analyzer.analyze(ENTRY)).ok
    assert all(not path.exists() for path in seen)


def test_overlay_uses_inherited_model_selection(tmp_path, monkeypatch):
    config_home(tmp_path)
    monkeypatch.setenv("KKAGENT_DEFAULT_MODEL", "test")
    with context_limited_config("", 16384, {"HOME": str(tmp_path)}) as path:
        assert tomllib.loads(path.read_text())['models']['test']['max_context_size'] == 16384


@pytest.mark.parametrize("cancelled", [False, True])
def test_context_recovery_rejects_other_sessions_and_cleans_up_on_cancellation(tmp_path, cancelled):
    original = config_home(tmp_path)
    analyzer = KkAgentRedmineAnalyzer(model="test", env_extra={"HOME": str(tmp_path)})
    paths = []

    async def stream(command, *_args, **_kwargs):
        if '--config' not in command:
            trace = complete_trace(session="original-session")
            trace.exit_code = 1
            raw = _StreamFallback()
            raw.feed_stderr(OVERFLOW)
            return trace, raw, False
        paths.append(Path(command[command.index('--config') + 1]))
        if cancelled:
            raise asyncio.CancelledError()
        return complete_trace(session="other-session"), _StreamFallback(), False

    with patch.object(analyzer, '_run_stream', side_effect=stream), patch(
        'features.redmine.kkagent.analyzer.probe_kkagent_mcp_health',
        AsyncMock(return_value=McpHealthProbe(ok=True, skipped=True)),
    ):
        if cancelled:
            with pytest.raises(asyncio.CancelledError):
                asyncio.run(analyzer.analyze(ENTRY))
        else:
            outcome = asyncio.run(analyzer.analyze(ENTRY))
            assert not outcome.ok
            assert outcome.error_type == 'repair_failed'
            assert outcome.session_id == 'original-session'
    assert paths and all(not path.exists() for path in paths)
    assert original.read_text() == CONFIG
