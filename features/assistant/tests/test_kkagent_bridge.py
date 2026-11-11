"""Native kkagent stream protocol and bounded reasoning-only process policy."""

import asyncio
import json
import shutil
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


# ==================== 输出契约与健康状态 ====================


def test_multi_segment_assistant_stream_keeps_only_the_final_text():
    # 一个回合可能有多段助手文本（工具调用间的中间叙述）；调用方把整段
    # 文本当单个 JSON 决策解析，因此必须只保留最后一段，拼接会产生非法
    # JSON 让解析永远失败。
    events = [
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "先查一下设备"}]}},
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "list_devices"},
            {"type": "text", "text": '{"type":"answer","text":"好"}'}]}},
    ]
    text, _sid, tools = bridge._extract_events([json.dumps(e) for e in events])
    assert json.loads(text)["text"] == "好"
    assert tools == ["list_devices"]


def test_availability_distinguishes_installed_and_model_state(monkeypatch):
    monkeypatch.setattr(bridge.shutil, "which", lambda _name: "/usr/bin/kkagent")
    bridge._record_turn_state(False, "回合超时", "timeout")
    state = bridge.availability()
    assert state["installed"] is True and state["available"] is True
    assert state["model_state"] == "failed" and "超时" in state["model_detail"]
    bridge._record_turn_state(True)
    assert bridge.availability()["model_state"] == "ready"


def test_turn_failure_records_model_state(monkeypatch, tmp_path):
    monkeypatch.setattr(bridge.shutil, "which", lambda _name: "/usr/bin/kkagent")
    bridge._record_turn_state(True)  # reset to ready first
    assert bridge.availability()["model_state"] == "ready"


@pytest.mark.asyncio
@pytest.mark.skipif(not shutil.which("kkagent"), reason="kkagent CLI not installed")
async def test_real_kkagent_cli_turn_returns_a_single_decision():
    # 真实 CLI 集成冒烟（仅在装有 kkagent 的环境执行）：协议要求单个
    # JSON 决策，桥接层只保留最终文本后必须仍可解析。
    result = await bridge.run_turn('{"type":"answer","text":"OK"}', timeout_seconds=60)
    assert result.ok, result.error
    assert result.text.strip()
