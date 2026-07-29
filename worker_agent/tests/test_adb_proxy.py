from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from worker_agent.adb_proxy import (
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
