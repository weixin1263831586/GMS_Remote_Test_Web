from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from worker_agent.adb_proxy import (
    _force_kill_adb_port,
    _restart_hub,
    _sanitize_log_line,
    _wait_adb_server,
    _wait_for_devices_gone,
    execute_adb_proxy_action,
    imported_device_for_serial,
    pair_code_from_grant,
    recover_managed_state,
)
from worker_agent.app import WorkerAgent
from worker_agent.config import WorkerConfig


def _config(tmp_path: Path) -> WorkerConfig:
    return WorkerConfig(
        worker_id="worker-source",
        controller_url="https://controller",
        token="worker-secret",
        data_root=tmp_path / "data",
        suite_roots=[tmp_path / "suites"],
    )


def test_pair_code_is_stable_per_grant_and_rotates_between_assignments():
    first = pair_code_from_grant("worker-secret", "grant-one")
    repeated = pair_code_from_grant("worker-secret", "grant-one")
    rotated = pair_code_from_grant("worker-secret", "grant-two")

    assert first == repeated
    assert first != rotated
    assert len(first) == 8
    assert set(first) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZ234567")


def test_proxy_log_lines_redact_credentials():
    line = "pair_code=ABCD2345 access_token:grant-value secret = hidden"
    sanitized = _sanitize_log_line(line)

    assert "ABCD2345" not in sanitized
    assert "grant-value" not in sanitized
    assert "hidden" not in sanitized


def test_imported_device_resolves_hub_prefixed_serial(tmp_path):
    (tmp_path / "target.json").write_text(
        (
            '{"imports":[{"source_worker_id":"worker-source",'
            '"source_address":"10.10.10.206","devices":["SERIAL"]}]}'
        ),
        encoding="utf-8",
    )
    with patch.dict(
        "os.environ",
        {"GMS_ADB_PROXY_STATE_ROOT": str(tmp_path)},
    ), patch(
        "worker_agent.adb_proxy._managed_running", return_value=True
    ):
        result = imported_device_for_serial("worker-source:SERIAL")

    assert result == {
        "source_worker_id": "worker-source",
        "source_address": "10.10.10.206",
        "source_serial": "SERIAL",
    }


def test_source_start_persists_selection_but_not_pair_code(tmp_path):
    process = MagicMock(pid=321)
    process.poll.return_value = None
    devices = [
        {"serial": "SELECTED", "state": "device"},
        {"serial": "HIDDEN", "state": "device"},
    ]
    with patch.dict(
        "os.environ",
        {"GMS_ADB_PROXY_STATE_ROOT": str(tmp_path)},
    ), patch(
        "worker_agent.adb_proxy._adb_devices", return_value=devices
    ), patch(
        "worker_agent.adb_proxy._binary", return_value="/bin/adb-proxy"
    ), patch(
        "worker_agent.adb_proxy._private_addresses",
        return_value={"10.10.10.207"},
    ), patch(
        "worker_agent.adb_proxy._stop_managed", return_value=False
    ), patch(
        "worker_agent.adb_proxy._wait_tcp", return_value=True
    ), patch(
        "worker_agent.adb_proxy.subprocess.Popen", return_value=process
    ) as popen:
        result = execute_adb_proxy_action(
            "source_start",
            {
                "devices": ["SELECTED"],
                "allowed_peer_address": "10.10.10.207",
                "access_token": "signed-grant",
            },
            pair_code="ABCD2345",
        )

    assert result["devices"] == ["SELECTED"]
    assert "access_token" not in result
    assert "ABCD2345" not in (tmp_path / "source.json").read_text()
    assert "signed-grant" in (tmp_path / "source.json").read_text()
    policy = (tmp_path / "proxy.toml").read_text()
    assert "HIDDEN" not in policy
    assert "SELECTED" in policy
    assert "enabled = true" in policy
    arguments = popen.call_args.args[0]
    assert arguments[arguments.index("--allow-peer") + 1] == "10.10.10.207"
    assert (tmp_path / "source.json").stat().st_mode & 0o077 == 0
    assert process.call_args is None


def test_source_start_requires_target_peer_allowlist(tmp_path):
    with patch.dict(
        "os.environ",
        {"GMS_ADB_PROXY_STATE_ROOT": str(tmp_path)},
    ), patch(
        "worker_agent.adb_proxy._adb_devices",
        return_value=[{"serial": "SELECTED", "state": "device"}],
    ), pytest.raises(ValueError, match="target address is empty"):
        execute_adb_proxy_action(
            "source_start",
            {
                "devices": ["SELECTED"],
                "access_token": "signed-grant",
            },
            pair_code="ABCD2345",
        )


def test_target_rejects_public_source_address(tmp_path):
    with patch.dict(
        "os.environ",
        {"GMS_ADB_PROXY_STATE_ROOT": str(tmp_path)},
    ), patch(
        "worker_agent.adb_proxy.socket.getaddrinfo",
        return_value=[(None, None, None, None, ("8.8.8.8", 0))],
    ), pytest.raises(ValueError, match="内网/VPN"):
        execute_adb_proxy_action(
            "target_connect",
            {
                "source_worker_id": "worker-source",
                "source_address": "public.example",
                "devices": ["SERIAL"],
            },
            pair_code="ABCD2345",
        )


def test_target_rolls_back_private_state_when_hub_start_fails(tmp_path):
    with patch.dict(
        "os.environ",
        {"GMS_ADB_PROXY_STATE_ROOT": str(tmp_path)},
    ), patch(
        "worker_agent.adb_proxy._private_bind_address",
        return_value="10.10.10.206",
    ), patch(
        "worker_agent.adb_proxy._restart_hub",
        side_effect=RuntimeError("hub failed"),
    ), patch(
        "worker_agent.adb_proxy._target_disconnect",
        return_value={"connected": False},
    ) as rollback, pytest.raises(RuntimeError, match="hub failed"):
        execute_adb_proxy_action(
            "target_connect",
            {
                "source_worker_id": "worker-source",
                "source_address": "10.10.10.206",
                "devices": ["SERIAL"],
            },
            pair_code="ABCD2345",
        )

    rollback.assert_called_once_with({"source_worker_id": "worker-source"})


def test_recovery_restarts_persisted_source_after_host_reboot(tmp_path):
    (tmp_path / "source.json").write_text(
        (
                '{"running":true,"devices":["SERIAL"],'
                '"listen_address":"10.10.10.206",'
                '"allowed_peer_address":"10.10.10.207",'
                '"access_token":"signed-grant"}'
        ),
        encoding="utf-8",
    )
    with patch.dict(
        "os.environ",
        {"GMS_ADB_PROXY_STATE_ROOT": str(tmp_path)},
    ), patch(
        "worker_agent.adb_proxy._managed_running", return_value=False
    ), patch(
        "worker_agent.adb_proxy._source_start",
        return_value={"running": True},
    ) as start:
        result = recover_managed_state(secret="worker-secret")

    assert result == {"recovered": ["source"], "errors": []}
    start.assert_called_once_with(
        {
            "devices": ["SERIAL"],
            "listen_address": "10.10.10.206",
            "allowed_peer_address": "10.10.10.207",
            "access_token": "signed-grant",
            "generation": 0,
        },
        pair_code_from_grant("worker-secret", "signed-grant"),
    )


def test_recovery_restarts_persisted_target_after_host_reboot(tmp_path):
    (tmp_path / "target.json").write_text(
        (
            '{"imports":[{"source_worker_id":"worker-source",'
            '"source_address":"10.10.10.206","devices":["SERIAL"]}]}'
        ),
        encoding="utf-8",
    )
    (tmp_path / "hub.toml").write_text(
        (
            '[[backend]]\nname = "gms-worker-source"\n'
            'addr = "10.10.10.206:5038"\npair_code = "ABCD2345"\n'
            'enabled = true\n'
        ),
        encoding="utf-8",
    )
    with patch.dict(
        "os.environ",
        {"GMS_ADB_PROXY_STATE_ROOT": str(tmp_path)},
    ), patch(
        "worker_agent.adb_proxy._managed_running", return_value=False
    ), patch(
        "worker_agent.adb_proxy._restart_hub"
    ) as restart:
        result = recover_managed_state(secret="unused")

    assert result == {"recovered": ["target"], "errors": []}
    restart.assert_called_once_with(tmp_path / "hub.toml")


def test_restart_hub_retries_once_on_bind_port_race(tmp_path):
    """Bind 失败（端口被并发拉起的 adb server 抢占）时清理端口重试一次。"""
    healthy = MagicMock(pid=101)
    healthy.poll.return_value = None
    crashed = MagicMock(pid=102)
    crashed.poll.return_value = 1
    processes = iter([crashed, healthy])
    with patch.dict(
        "os.environ",
        {"GMS_ADB_PROXY_STATE_ROOT": str(tmp_path)},
    ), patch(
        "worker_agent.adb_proxy._read_hub_config",
        return_value={"backend": [{"name": "b", "addr": "a", "pair_code": "p"}]},
    ), patch(
        "worker_agent.adb_proxy._stop_managed", return_value=False
    ), patch(
        "worker_agent.adb_proxy._force_kill_adb_port"
    ), patch(
        "worker_agent.adb_proxy._binary", return_value="/bin/adb-hub"
    ), patch(
        # Popen dup's the write handle parent-side; production closes it in a
        # finally, so the fake log handle must be a closeable mock.
        "worker_agent.adb_proxy._process_log", return_value=MagicMock()
    ), patch(
        "worker_agent.adb_proxy.subprocess.Popen",
        side_effect=lambda *a, **k: next(processes),
    ) as popen, patch(
        "worker_agent.adb_proxy._tail_log",
        return_value=["adb-hub error: failed to bind 127.0.0.1:5037: Address in use"],
    ), patch(
        "worker_agent.adb_proxy._wait_hub_listening", return_value=True
    ), patch(
        "worker_agent.adb_proxy.time.sleep"
    ), patch(
        "worker_agent.adb_proxy._wait_tcp", return_value=True
    ), patch(
        "worker_agent.adb_proxy._wait_adb_server", return_value=(True, "")
    ):
        _restart_hub(tmp_path / "hub.toml")

    assert popen.call_count == 2


def test_restart_hub_reports_bind_failure_without_long_wait(tmp_path):
    """非端口竞争的启动失败应立即报出，并携带 hub 日志中的真实错误。"""
    crashed = MagicMock(pid=103)
    crashed.poll.return_value = 1
    with patch.dict(
        "os.environ",
        {"GMS_ADB_PROXY_STATE_ROOT": str(tmp_path)},
    ), patch(
        "worker_agent.adb_proxy._read_hub_config",
        return_value={"backend": [{"name": "b", "addr": "a", "pair_code": "p"}]},
    ), patch(
        "worker_agent.adb_proxy._stop_managed", return_value=False
    ), patch(
        "worker_agent.adb_proxy._force_kill_adb_port"
    ), patch(
        "worker_agent.adb_proxy._binary", return_value="/bin/adb-hub"
    ), patch(
        # Popen dup's the write handle parent-side; production closes it in a
        # finally, so the fake log handle must be a closeable mock.
        "worker_agent.adb_proxy._process_log", return_value=MagicMock()
    ), patch(
        "worker_agent.adb_proxy.subprocess.Popen", return_value=crashed
    ), patch(
        "worker_agent.adb_proxy._tail_log",
        return_value=["adb-hub error: local adb server error: start-server failed"],
    ), pytest.raises(
        RuntimeError,
        match=r"adb-hub 启动失败.*local adb server error",
    ):
        _restart_hub(tmp_path / "hub.toml")


def _spawn_daemon_hub(
    tmp_path: Path,
    bind_error_attempts: set[int],
    listening_attempts: set[int] = frozenset(),
):
    """Popen side effect: per-attempt hub log lines, tracked process alive.

    Mimics adb-hub --daemon: the tracked process keeps running (poll stays
    None) even when the daemon child fails to bind :5037 (log-only bind
    error), and startup completes only when the "listening" marker is
    written for that attempt.
    """
    state = {"attempt": 0}

    def popen_effect(*args, **kwargs):
        state["attempt"] += 1
        process = MagicMock(pid=400 + state["attempt"])
        process.poll.return_value = None
        log = tmp_path / "logs" / "hub.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8") as stream:
            if state["attempt"] in bind_error_attempts:
                stream.write(
                    "adb-hub error: failed to bind 127.0.0.1:5037: "
                    "Address in use (os error 98)\n"
                )
            if state["attempt"] in listening_attempts:
                stream.write(
                    "2026-09-21T02:09:13.725249Z INFO adb-hub 0.4.5 "
                    'listening version="0.4.5" listen=127.0.0.1:5037\n'
                )
        return process

    return popen_effect


def test_restart_hub_retries_when_daemon_masks_bind_failure(tmp_path):
    """--daemon 下 bind 失败仅见于日志(进程不退出):也应触发端口清理重试。"""
    with patch.dict(
        "os.environ",
        {"GMS_ADB_PROXY_STATE_ROOT": str(tmp_path)},
    ), patch(
        "worker_agent.adb_proxy._read_hub_config",
        return_value={"backend": [{"name": "b", "addr": "a", "pair_code": "p"}]},
    ), patch(
        "worker_agent.adb_proxy._stop_managed", return_value=False
    ), patch(
        "worker_agent.adb_proxy._force_kill_adb_port"
    ), patch(
        "worker_agent.adb_proxy._binary", return_value="/bin/adb-hub"
    ), patch(
        # Popen dup's the write handle parent-side; production closes it in a
        # finally, so the fake log handle must be a closeable mock.
        "worker_agent.adb_proxy._process_log", return_value=MagicMock()
    ), patch(
        "worker_agent.adb_proxy.subprocess.Popen",
        side_effect=_spawn_daemon_hub(tmp_path, {1}, {2}),
    ) as popen, patch(
        "worker_agent.adb_proxy.time.sleep"
    ), patch(
        "worker_agent.adb_proxy._wait_tcp", return_value=True
    ), patch(
        "worker_agent.adb_proxy._wait_adb_server", return_value=(True, "")
    ):
        _restart_hub(tmp_path / "hub.toml")

    assert popen.call_count == 2


def test_restart_hub_retries_once_when_hub_stalls_before_listening(tmp_path):
    """hub 卡在 listening 之前（无 bind 错误）应清理端口重试一次。"""
    with patch.dict(
        "os.environ",
        {"GMS_ADB_PROXY_STATE_ROOT": str(tmp_path)},
    ), patch(
        "worker_agent.adb_proxy._read_hub_config",
        return_value={"backend": [{"name": "b", "addr": "a", "pair_code": "p"}]},
    ), patch(
        "worker_agent.adb_proxy._stop_managed", return_value=False
    ), patch(
        "worker_agent.adb_proxy._force_kill_adb_port"
    ), patch(
        "worker_agent.adb_proxy._binary", return_value="/bin/adb-hub"
    ), patch(
        # Popen dup's the write handle parent-side; production closes it in a
        # finally, so the fake log handle must be a closeable mock.
        "worker_agent.adb_proxy._process_log", return_value=MagicMock()
    ), patch(
        "worker_agent.adb_proxy.subprocess.Popen",
        side_effect=_spawn_daemon_hub(tmp_path, set(), {2}),
    ) as popen, patch(
        "worker_agent.adb_proxy._hub_startup_exited",
        return_value=(False, ""),
    ), patch(
        "worker_agent.adb_proxy.time.sleep"
    ), patch(
        "worker_agent.adb_proxy._wait_tcp", return_value=True
    ), patch(
        "worker_agent.adb_proxy._wait_adb_server", return_value=(True, "")
    ):
        _restart_hub(tmp_path / "hub.toml")

    assert popen.call_count == 2


def test_restart_hub_fails_fast_when_hub_never_reaches_listening(tmp_path):
    """两次启动都无就绪标记时应立即报“监听就绪超时”，不空转探测 RST。"""
    with patch.dict(
        "os.environ",
        {"GMS_ADB_PROXY_STATE_ROOT": str(tmp_path)},
    ), patch(
        "worker_agent.adb_proxy._read_hub_config",
        return_value={"backend": [{"name": "b", "addr": "a", "pair_code": "p"}]},
    ), patch(
        "worker_agent.adb_proxy._stop_managed", return_value=False
    ), patch(
        "worker_agent.adb_proxy._force_kill_adb_port"
    ), patch(
        "worker_agent.adb_proxy._binary", return_value="/bin/adb-hub"
    ), patch(
        # Popen dup's the write handle parent-side; production closes it in a
        # finally, so the fake log handle must be a closeable mock.
        "worker_agent.adb_proxy._process_log", return_value=MagicMock()
    ), patch(
        "worker_agent.adb_proxy.subprocess.Popen",
        side_effect=_spawn_daemon_hub(tmp_path, set()),
    ) as popen, patch(
        "worker_agent.adb_proxy._hub_startup_exited",
        return_value=(False, ""),
    ), patch(
        "worker_agent.adb_proxy._wait_hub_listening", return_value=False
    ), pytest.raises(
        RuntimeError,
        match=r"adb-hub 启动超时.*监听就绪",
    ):
        _restart_hub(tmp_path / "hub.toml")

    assert popen.call_count == 2


def test_wait_adb_server_fails_fast_on_fresh_bind_error(tmp_path):
    """hub 日志出现 bind 冲突时立即失败,不空转探测抢占 5037 的进程。"""
    log = tmp_path / "logs" / "hub.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(
        "adb-hub error: failed to bind 127.0.0.1:5037: "
        "Address in use (os error 98)\n",
        encoding="utf-8",
    )
    process = MagicMock()
    process.poll.return_value = None
    with patch.dict(
        "os.environ",
        {"GMS_ADB_PROXY_STATE_ROOT": str(tmp_path)},
    ), patch(
        "worker_agent.adb_proxy._adb_devices_safe"
    ) as probe, patch(
        "worker_agent.adb_proxy.time.sleep"
    ):
        ready, detail = _wait_adb_server(process, timeout=20, log_offset=0)

    assert ready is False
    assert "failed to bind" in detail
    probe.assert_not_called()


def test_force_kill_adb_port_escalates_to_kill_until_port_drains():
    """TERM 后端口仍被占用应升级 KILL,端口清空后确认无重新抢占再返回。"""
    listeners = [[411], [411], [], []]
    runs = []
    with patch(
        "worker_agent.adb_proxy._kill_adb_server"
    ) as graceful, patch(
        "worker_agent.adb_proxy._listeners_on_loopback_port",
        side_effect=lambda _port: listeners.pop(0),
    ), patch(
        "worker_agent.adb_proxy.subprocess.run",
        side_effect=lambda cmd, **kwargs: runs.append(cmd),
    ), patch(
        "worker_agent.adb_proxy.time.sleep"
    ):
        _force_kill_adb_port(5037)

    graceful.assert_called_once_with("127.0.0.1:5037")
    assert ["kill", "-TERM", "411"] in runs
    assert ["kill", "-KILL", "411"] in runs


def test_worker_source_command_derives_code_without_returning_it(tmp_path):
    agent = WorkerAgent(_config(tmp_path))
    agent.client = MagicMock()
    result = {"running": True, "devices": ["SERIAL"]}
    command = {
        "id": "adb-proxy-source",
        "command_type": "adb_proxy",
        "payload": {
            "action": "source_start",
            "devices": ["SERIAL"],
            "access_token": "signed-grant",
        },
    }
    with patch(
        "worker_agent.app.execute_adb_proxy_action", return_value=result
    ) as execute:
        agent.handle(command)

    assert execute.call_args.kwargs["pair_code"] == pair_code_from_grant(
        "worker-secret",
        "signed-grant",
    )
    agent.client.adb_proxy_pair_code.assert_not_called()
    assert agent.runtime.previous_command(command["id"])["result"] == result


def test_worker_target_command_fetches_code_with_short_lived_grant(tmp_path):
    agent = WorkerAgent(_config(tmp_path))
    agent.client = MagicMock()
    agent.heartbeat = MagicMock()
    agent.client.adb_proxy_pair_code.return_value = "ZXCV2345"
    payload = {
        "action": "target_connect",
        "source_worker_id": "worker-origin",
        "source_address": "10.10.10.206",
        "devices": ["SERIAL"],
        "access_token": "signed-grant",
    }
    with patch(
        "worker_agent.app.execute_adb_proxy_action",
        return_value={"connected": True},
    ) as execute:
        agent.handle({
            "id": "adb-proxy-target",
            "command_type": "adb_proxy",
            "payload": payload,
        })

    agent.client.adb_proxy_pair_code.assert_called_once_with(
        "worker-origin", "signed-grant"
    )
    agent.heartbeat.assert_called_once_with()
    assert execute.call_args.kwargs["pair_code"] == "ZXCV2345"


def test_target_disconnect_waits_for_removed_serials_before_return(tmp_path):
    """断开后必须轮询到被移除的 serial 真正离开 inventory 才返回。

    Worker 在 ACK 前用 heartbeat 推送 inventory；若不等 hub 重启收敛就
    返回，UI 的断开后自动刷新会拿到仍含旧设备的列表。
    """
    (tmp_path / "target.json").write_text(
        (
            '{"imports":['
            '{"source_worker_id":"worker-remote","source_address":"10.10.10.206",'
            '"devices":["ATS357629"],"generation":1},'
            '{"source_worker_id":"worker-other","source_address":"10.10.10.207",'
            '"devices":["KEEP"],"generation":1}]}'
        ),
        encoding="utf-8",
    )
    (tmp_path / "hub.toml").write_text(
        (
            '[[backend]]\nname = "gms-worker-remote"\n'
            'addr = "10.10.10.206:5038"\npair_code = "AAAA2345"\n'
            'enabled = true\n\n[[backend]]\nname = "gms-worker-other"\n'
            'addr = "10.10.10.207:5038"\npair_code = "BBBB2345"\n'
            'enabled = true\n'
        ),
        encoding="utf-8",
    )
    still_visible = [{"serial": "gms-worker-remote:ATS357629", "state": "device"}]
    gone = [{"serial": "KEEP", "state": "device"}]
    with patch.dict(
        "os.environ",
        {"GMS_ADB_PROXY_STATE_ROOT": str(tmp_path)},
    ), patch(
        "worker_agent.adb_proxy._restart_hub"
    ) as restart, patch(
        "worker_agent.adb_proxy._adb_devices_safe",
        side_effect=[still_visible, still_visible, gone],
    ) as probe, patch(
        "worker_agent.adb_proxy.time.sleep"
    ):
        result = execute_adb_proxy_action(
            "target_disconnect",
            {
                "source_worker_id": "worker-remote",
                "generation": 1,
            },
        )

    assert result["connected"] is True
    assert result["remaining_imports"][0]["source_worker_id"] == "worker-other"
    restart.assert_called_once_with(tmp_path / "hub.toml")
    assert probe.call_count == 3


def test_wait_for_devices_gone_never_fails_the_disconnect():
    """等待只是 best-effort：超时后静默返回，不让已成功的断开报错。"""
    visible = [{"serial": "gms-src:SERIAL", "state": "device"}]
    with patch(
        "worker_agent.adb_proxy._adb_devices_safe",
        return_value=visible,
    ) as probe, patch(
        "worker_agent.adb_proxy.time.sleep"
    ):
        _wait_for_devices_gone(["SERIAL"], "gms-src", timeout=0.5)

    assert probe.call_count >= 2
