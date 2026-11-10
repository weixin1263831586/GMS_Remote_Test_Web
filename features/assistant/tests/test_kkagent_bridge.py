"""Native kkagent stream protocol and bounded reasoning-only process policy."""

import asyncio
import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, Mock

import pytest

from features.assistant import kkagent_bridge as bridge
from features.assistant.kkagent_runtime import read_bounded, restricted_runtime
from foundation.command_result import CommandResult


def test_native_and_legacy_streams_extract_assistant_text_and_session():
    events = [{"type": "session", "session_id": "sid"},
              {"type": "message", "role": "assistant", "content": '{"type":"answer","text":"好"}'},
              {"type": "result", "session_id": "sid", "message": "completed"}]
    text, session_id, tools = bridge._extract_events([json.dumps(event) for event in events])
    assert json.loads(text)["text"] == "好"
    assert session_id == "sid" and tools == []
    legacy = [{"type": "assistant", "message": {"content": [{"type": "text", "text": "old"}]}}]
    assert bridge._extract_events([json.dumps(event) for event in legacy])[0] == "old"


@pytest.mark.asyncio
async def test_bridge_uses_isolated_config_plan_mode_and_an_empty_workspace(monkeypatch, tmp_path):
    @asynccontextmanager
    async def runtime(*args):
        yield tmp_path / "restricted.toml", str(tmp_path)

    stdout = asyncio.StreamReader()
    stdout.feed_data(b'{"type":"result","session_id":"sid","message":"ok"}\n')
    stdout.feed_eof()
    stderr = asyncio.StreamReader()
    stderr.feed_eof()
    process = type("Process", (), {"stdout": stdout, "stderr": stderr, "returncode": 0,
                                  "wait": AsyncMock(return_value=0)})()
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr(bridge, "availability", lambda: {"available": True})
    monkeypatch.setattr(bridge, "restricted_runtime", runtime)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    result = await bridge.run_turn("测试", context="只查询")
    assert result.ok and result.text == "ok"
    command = spawn.call_args.args
    assert "--plan" in command and "--config" in command
    assert "--auto" not in command and "--yolo" not in command
    assert spawn.call_args.kwargs["cwd"] == str(tmp_path)


@pytest.mark.asyncio
async def test_model_output_is_bounded():
    stream = asyncio.StreamReader()
    stream.feed_data(b"x" * 100)
    stream.feed_eof()
    with pytest.raises(RuntimeError, match="大小限制"):
        await read_bounded(stream, limit=50)


@pytest.mark.asyncio
async def test_timeout_retains_the_streamed_session_and_reaps_the_process(monkeypatch, tmp_path):
    @asynccontextmanager
    async def runtime(*args):
        yield tmp_path / "restricted.toml", str(tmp_path)

    stdout, stderr = asyncio.StreamReader(), asyncio.StreamReader()
    stdout.feed_data(b'{"type":"session","session_id":"first-session"}\n')
    process = type("Process", (), {"stdout": stdout, "stderr": stderr, "returncode": None})()
    async def wait():
        await asyncio.Event().wait()

    process.wait = wait
    reap = AsyncMock()
    monkeypatch.setattr(bridge, "availability", lambda: {"available": True})
    monkeypatch.setattr(bridge, "restricted_runtime", runtime)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    monkeypatch.setattr(bridge, "_terminate_bridge_process", reap)
    result = await bridge.run_turn("烧写固件", timeout_seconds=0.02)
    assert not result.ok and result.error_code == "timeout"
    assert result.session_id == "first-session"
    reap.assert_awaited_once_with(process)


@pytest.mark.asyncio
async def test_bridge_deadline_also_bounds_policy_preparation(monkeypatch):
    @asynccontextmanager
    async def runtime(*args):
        await asyncio.Event().wait()
        yield None, ""

    spawn = AsyncMock()
    monkeypatch.setattr(bridge, "availability", lambda: {"available": True})
    monkeypatch.setattr(bridge, "restricted_runtime", runtime)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    result = await bridge.run_turn("介绍下自己", timeout_seconds=0.01)
    assert not result.ok and result.error_code == "timeout"
    spawn.assert_not_awaited()


@pytest.mark.asyncio
async def test_runtime_denies_native_tools_mcp_and_hooks_without_editing_host_config(tmp_path, monkeypatch):
    source = tmp_path / "operator.toml"
    source.write_text('default_permission_mode = "yolo"\n')
    monkeypatch.setenv("GMS_ASSISTANT_KKAGENT_CONFIG", str(source))
    command = Mock(return_value=CommandResult(code=0))
    monkeypatch.setattr("features.assistant.kkagent_runtime.run_local_command", command)
    async with restricted_runtime("kkagent", {}) as (config, directory):
        assert config.stat().st_mode & 0o777 == 0o600
        assert str(config.parent) != directory
    assert not config.exists()
    assert source.read_text() == 'default_permission_mode = "yolo"\n'
    changes = {call.args[0][-2]: call.args[0][-1] for call in command.call_args_list}
    assert changes == {"default_permission_mode": '"manual"', "permission.rules": '[{decision="deny", pattern="*", scope="user"}]',
                       "mcp_servers": "{}", "hooks": "[]"}


@pytest.mark.asyncio
async def test_runtime_fails_closed_when_a_policy_setting_is_rejected(tmp_path, monkeypatch):
    source = tmp_path / "operator.toml"
    source.write_text('default_permission_mode = "yolo"\n')
    monkeypatch.setenv("GMS_ASSISTANT_KKAGENT_CONFIG", str(source))
    monkeypatch.setattr("features.assistant.kkagent_runtime.run_local_command", Mock(return_value=CommandResult(code=1)))
    with pytest.raises(RuntimeError, match="隔离工具策略"):
        async with restricted_runtime("kkagent", {}):
            pytest.fail("must not run the model with an incomplete policy")
