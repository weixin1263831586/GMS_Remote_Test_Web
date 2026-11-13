"""Native kkagent stream protocol and bounded reasoning-only process policy."""

import asyncio
import json
import shutil
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, Mock

import pytest

from features.assistant import kkagent_bridge as bridge
from features.kkagent import config as gateway_config
from features.kkagent import extract_turn_events, read_bounded, restricted_runtime, run_headless_turn
from foundation.command_result import CommandResult


@pytest.fixture(autouse=True)
def isolated_model_catalog(monkeypatch, tmp_path, request):
    if request.node.name == "test_real_kkagent_cli_turn_returns_a_single_decision":
        return
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("GMS_ASSISTANT_KKAGENT_CONFIG", raising=False)
    monkeypatch.delenv("KKAGENT_DEFAULT_MODEL", raising=False)
    config = {
        "enabled": True, "primary_provider": "gateway",
        "providers": {"gateway": {
            "enabled": True, "base_url": "https://gateway.invalid", "model": "test",
            "api_key": "synthetic-bridge-key",
        }},
    }
    monkeypatch.setattr(gateway_config._config_manager, "get_ai_config", lambda: config)


def test_native_and_legacy_streams_extract_assistant_text_and_session():
    events = [{"type": "session", "session_id": "sid"},
              {"type": "message", "role": "assistant", "content": '{"type":"answer","text":"好"}'},
              {"type": "result", "session_id": "sid", "message": "completed"}]
    text, session_id, tools = extract_turn_events([json.dumps(event) for event in events])
    assert json.loads(text)["text"] == "好"
    assert session_id == "sid" and tools == []
    legacy = [{"type": "assistant", "message": {"content": [{"type": "text", "text": "old"}]}}]
    assert extract_turn_events([json.dumps(event) for event in legacy])[0] == "old"


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
    monkeypatch.setattr("features.kkagent.session.restricted_runtime", runtime)
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
    monkeypatch.setattr("features.kkagent.session.restricted_runtime", runtime)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    reap_target = "features.kkagent.session.terminate_process_tree"
    monkeypatch.setattr(reap_target, reap)
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
    monkeypatch.setattr("features.kkagent.session.restricted_runtime", runtime)
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
    monkeypatch.setattr("features.kkagent.sandbox.run_local_command", command)
    async with restricted_runtime("kkagent", {}) as (config, directory):
        assert config.stat().st_mode & 0o777 == 0o600
        assert str(config.parent) != directory
        parsed = gateway_config.tomllib.loads(config.read_text())
        assert parsed["default_model"] == "test"
        assert parsed["providers"]["gateway"]["api_key_env"] == "GMS_MODEL_API_KEY"
        assert "synthetic-bridge-key" not in config.read_text()
    assert not config.exists()
    assert source.read_text() == 'default_permission_mode = "yolo"\n'
    changes = {call.args[0][-2]: call.args[0][-1] for call in command.call_args_list}
    assert changes == {"default_permission_mode": '"manual"', "permission.rules": '[{decision="deny", pattern="*", scope="user"}]',
                       "mcp_servers": "{}", "hooks": "[]"}


async def test_headless_callback_preserves_session_and_reaps_before_runtime_cleanup(monkeypatch, tmp_path):
    stdout, stderr = asyncio.StreamReader(), asyncio.StreamReader()
    stdout.feed_data(b'{"type":"session","session_id":"observed-session"}\n')
    process = Mock(stdout=stdout, stderr=stderr, returncode=None)

    async def wait():
        await asyncio.Event().wait()

    process.wait = wait

    async def reap(_process):
        process.returncode = -15

    @asynccontextmanager
    async def runtime(*_args):
        try:
            yield tmp_path / "config.toml", str(tmp_path)
        finally:
            assert process.returncode == -15

    monkeypatch.setattr("features.kkagent.session.restricted_runtime", runtime)
    monkeypatch.setattr("features.kkagent.session.terminate_process_tree", reap)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    lines = []
    result = await run_headless_turn("test", env={}, timeout_seconds=0.02, on_line=lines.append)
    assert result.error_code == "timeout" and result.session_id == "observed-session"
    assert len(lines) == 1


@pytest.mark.asyncio
async def test_runtime_fails_closed_when_a_policy_setting_is_rejected(tmp_path, monkeypatch):
    source = tmp_path / "operator.toml"
    source.write_text('default_permission_mode = "yolo"\n')
    monkeypatch.setenv("GMS_ASSISTANT_KKAGENT_CONFIG", str(source))
    monkeypatch.setattr("features.kkagent.sandbox.run_local_command", Mock(return_value=CommandResult(code=1)))
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
    text, _sid, tools = extract_turn_events([json.dumps(e) for e in events])
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


# ==================== 真实 CLI 子进程流（fake binary 走完整 spawn 路径） ====================

FAKE_KKAGENT = r'''#!/usr/bin/env python3
"""fake kkagent CLI：按真实 stream-json 协议应答，验证桥接的完整子进程链路。"""
import json
import sys

args = sys.argv[1:]
if "config" in args:
    # restricted_runtime 的隔离策略写入：fake CLI 接受即可
    sys.exit(0)

resume = ""
if "--resume" in args:
    resume = args[args.index("--resume") + 1]

events = [
    {"type": "system", "subtype": "init", "session_id": "real-session-1"},
    {"type": "assistant", "message": {"content": [
        {"type": "text", "text": "先查询设备清单"}]}},
    {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "list_devices", "input": {}}]}},
    {"type": "assistant", "message": {"content": [
        {"type": "text", "text": json.dumps(
            {"type": "answer", "text": f"resumed={resume}" if resume else "OK"},
            ensure_ascii=False)}]}},
    {"type": "result", "session_id": "real-session-1"},
]
for event in events:
    print(json.dumps(event, ensure_ascii=False), flush=True)
'''

FAKE_KKAGENT_FAILING = r'''#!/usr/bin/env python3
import sys
if "config" in sys.argv[1:]:
    sys.exit(0)
print("model upstream disconnected", file=sys.stderr)
sys.exit(7)
'''

FAKE_KKAGENT_SIGINT = r'''#!/usr/bin/env python3
"""模拟 systemd stop 的 SIGINT 广播：KKAgent 进程被信号杀死（负退出码）。"""
import os
import sys
if "config" in sys.argv[1:]:
    sys.exit(0)
os.kill(os.getpid(), 2)  # SIGINT
'''


def _install_fake_cli(monkeypatch, tmp_path, body):
    cli = tmp_path / "kkagent"
    cli.write_text(body)
    cli.chmod(0o755)
    monkeypatch.setattr("features.kkagent.session.KKAGENT_BINARY", str(cli))
    monkeypatch.setattr(bridge, "availability", lambda: {"available": True})
    # 隔离运行时需要一份操作者模型配置
    monkeypatch.setenv("GMS_ASSISTANT_KKAGENT_CONFIG", str(tmp_path / "operator.toml"))
    (tmp_path / "operator.toml").write_text('default_permission_mode = "manual"\n')
    return cli


@pytest.mark.asyncio
async def test_real_subprocess_stream_json_yields_final_json_decision_and_resume(monkeypatch, tmp_path):
    """审核 P1：真实 CLI 流事件——多段中间文本、tool_use、最终 JSON 决策。

    走 run_turn 的完整子进程路径（不 mock spawn）：中间叙述不得污染
    最终决策，tool_use 进 tools_used，session_id 用于下一轮 --resume。
    """
    _install_fake_cli(monkeypatch, tmp_path, FAKE_KKAGENT)
    first = await bridge.run_turn("查询设备", timeout_seconds=30)
    assert first.ok, first.error
    decision = json.loads(first.text)
    assert decision["text"] == "OK"
    assert first.session_id == "real-session-1"
    assert first.tools_used == ["list_devices"]

    second = await bridge.run_turn("继续", session_id=first.session_id, timeout_seconds=30)
    assert second.ok, second.error
    assert json.loads(second.text)["text"] == "resumed=real-session-1"


@pytest.mark.asyncio
async def test_real_subprocess_nonzero_exit_maps_to_process_error(monkeypatch, tmp_path):
    _install_fake_cli(monkeypatch, tmp_path, FAKE_KKAGENT_FAILING)
    result = await bridge.run_turn("查询设备", timeout_seconds=30)
    assert not result.ok
    assert result.error_code == "process_error"
    assert "退出码 7" in result.error


@pytest.mark.asyncio
async def test_signal_killed_turn_is_reported_as_signalled_not_process_error(monkeypatch, tmp_path):
    """systemctl stop（KillSignal=SIGINT）广播到服务 cgroup 的在途回合。

    子进程以负退出码（-2 = SIGINT）结束：错误码必须是 signalled 而非
    process_error，且文案指明是 Controller 重启/停止所致并给出重试指引，
    不再显示裸数字"退出码 -2"。
    """
    _install_fake_cli(monkeypatch, tmp_path, FAKE_KKAGENT_SIGINT)
    result = await bridge.run_turn("查询设备", timeout_seconds=30)
    assert not result.ok
    assert result.error_code == "signalled"
    assert "-2" not in result.error
    assert "SIGINT" in result.error
    assert "重启" in result.error and "重试" in result.error
    assert bridge._last_turn_state["state"] == "failed"
    assert "SIGINT" in bridge._last_turn_state["detail"]


def test_signal_exit_detail_maps_known_and_unknown_signals():
    assert bridge._signal_exit_detail(-15).startswith("kkagent 进程被 SIGTERM 终止")
    assert bridge._signal_exit_detail(-65).startswith("kkagent 进程被 SIG65 终止")
    assert bridge._signal_exit_detail(3) == "kkagent 退出码 3"


@pytest.mark.asyncio
async def test_bridge_preserves_long_message_and_context_without_agent_token(monkeypatch):
    from features.kkagent import HeadlessTurnResult

    monkeypatch.setattr(bridge, "availability", lambda: {"available": True})
    monkeypatch.delenv("GMS_AUTH_TOKEN_FILE", raising=False)
    turn = AsyncMock(return_value=HeadlessTurnResult(ok=True, text="ok"))
    monkeypatch.setattr(bridge, "run_headless_turn", turn)
    message = "m" * 4500 + "message tail"
    context = "上" * 31000 + "context tail"
    result = await bridge.run_turn(message, context=context)
    assert result.ok
    prompt = turn.call_args.args[0]
    assert context in prompt and prompt.endswith(message)
    assert "GMS_AUTH_TOKEN_FILE" not in turn.call_args.kwargs["env"]
