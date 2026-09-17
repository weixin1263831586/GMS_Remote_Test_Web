from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from bootstrap.application import create_app
from features.auth import auth_service
from features.system.state import global_state
from features.system.terminal_service import (
    close_websocket_terminal,
    handle_terminal_connect,
    handle_terminal_input,
)
from foundation.device_claims import DeviceClaimRegistry


def _pin_adb_binary():
    """Pin a deterministic adb binary for resolver-backed call sites.

    测试机不一定安装 platform-tools；解析器现在会在设备轮询/终端探测/
    ADB 握手里被调用，统一钉到 /bin/true（存在且可执行）保证行为只取决于
    打桩的 subprocess.run，而不是 runner 的 PATH。
    """
    return patch.dict(os.environ, {"GMS_ADB_PATH": "/bin/true"})


class _FakeChannel:
    def __init__(self):
        self.closed = False
        self.sent = []
        # 模拟真实 shell：登录提示符就绪一次，之后每收到一条命令输出
        # 一个新提示符，供 handle_adb_shell_connect 的 drain 辅助立即命中。
        self._prompt_pending = True

    def resize_pty(self, **_kwargs):
        return None

    def recv_ready(self):
        return self._prompt_pending

    def recv(self, _size):
        self._prompt_pending = False
        return b"hcq@localhost:~$ "

    def send(self, value):
        self.sent.append(value)
        self._prompt_pending = True
        return len(value)

    def close(self):
        self.closed = True


class _FakeWebSocket:
    def __init__(self):
        self.state = SimpleNamespace()
        self.messages = []

    async def send_json(self, value):
        self.messages.append(value)


class TerminalSecurityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.original_db_path = auth_service.db_path
        self.original_initialized = auth_service._initialized
        auth_service.db_path = Path(self.tmp.name) / "auth.sqlite3"
        auth_service._initialized = False
        self.environment = patch.dict(
            "os.environ",
            {
                "GMS_AUTH_REQUIRED": "true",
                "GMS_SECURE_COOKIES": "false",
                "TRUSTED_HOSTS": "testserver",
                # /bin/true 存在且可执行于所有 runner：ADB 解析器
                # （foundation.adb_binary）拿到确定值，行为只取决于
                # 各测试对 subprocess.run 的打桩。
                "GMS_ADB_PATH": "/bin/true",
            },
        )
        self.environment.start()
        self.client = TestClient(create_app())

    def tearDown(self):
        self.client.close()
        self.environment.stop()
        auth_service.db_path = self.original_db_path
        auth_service._initialized = self.original_initialized
        with global_state.terminal_lock:
            global_state.terminal_ssh_sessions.clear()
        self.tmp.cleanup()

    def _setup_admin(self):
        return self.client.post(
            "/api/auth/setup",
            headers={"Origin": "http://testserver"},
            json={"username": "admin", "password": "strongpass1"},
        )

    def test_terminal_message_requires_elevation_even_on_non_terminal_socket_name(self):
        self.assertEqual(self._setup_admin().status_code, 200)
        with self.client.websocket_connect(
            "/api/system/websocket/workspace-bypass-attempt",
            headers={"Origin": "http://testserver"},
        ) as websocket:
            websocket.send_json(
                {
                    "type": "terminal_connect",
                    "mode": "ssh",
                    "host": "127.0.0.1",
                    "user": "root",
                }
            )
            response = websocket.receive_json()

        self.assertEqual(response["type"], "terminal_error")
        self.assertTrue(response["elevation_required"])

    def test_client_host_is_ignored_in_favor_of_worker_directory(self):
        self.assertEqual(self._setup_admin().status_code, 200)
        self.assertEqual(
            self.client.post(
                "/api/auth/elevate",
                headers={"Origin": "http://testserver"},
                json={"username": "admin", "password": "strongpass1"},
            ).status_code,
            200,
        )
        with self.client.websocket_connect(
            "/api/system/websocket/terminal-authorized",
            headers={"Origin": "http://testserver"},
        ) as websocket:
            websocket.send_json(
                {
                    "type": "terminal_connect",
                    "mode": "ssh",
                    "worker_id": "missing-worker",
                    "host": "127.0.0.1",
                    "user": "root",
                    "password": "attacker-value",
                }
            )
            response = websocket.receive_json()

        self.assertEqual(response["type"], "terminal_error")
        self.assertIn("Worker", response["error"])

    def test_each_connect_gets_a_server_generated_connection_id(self):
        websocket = _FakeWebSocket()
        channels = [_FakeChannel(), _FakeChannel()]
        with patch(
            "features.system.terminal_service.config_manager.load_config",
            return_value={},
        ), patch(
            "features.system.terminal_service.resolve_authorized_terminal_target",
            return_value=("ats-worker-controller", "localhost", "admin", "", ""),
        ), patch(
            "features.system.terminal_service.is_local_host",
            return_value=True,
        ), patch(
            "features.system.terminal_service.create_local_terminal_channel",
            side_effect=channels,
        ), patch("features.system.terminal_output.threading.Thread.start"):
            asyncio.run(handle_terminal_connect("same-client", websocket, {"mode": "ssh"}))
            first = websocket.messages[-1]["connection_id"]
            asyncio.run(handle_terminal_connect("same-client", websocket, {"mode": "ssh"}))
            second = websocket.messages[-1]["connection_id"]

        self.assertNotEqual(first, second)
        self.assertTrue(channels[0].closed)
        self.assertNotIn("same-client", global_state.terminal_ssh_sessions)
        self.assertIn(second, global_state.terminal_ssh_sessions)
        close_websocket_terminal(websocket)

    def test_remote_terminal_failure_requests_host_scoped_credential(self):
        websocket = _FakeWebSocket()
        with patch(
            "features.system.terminal_service.config_manager.load_config",
            return_value={"use_key_auth": True},
        ), patch(
            "features.system.terminal_service.resolve_authorized_terminal_target",
            return_value=("worker-118", "172.16.14.118", "hcq", "", ""),
        ), patch(
            "features.system.terminal_service.is_local_host",
            return_value=False,
        ), patch(
            "features.system.terminal_service.ssh_manager.create_connection",
            return_value=None,
        ):
            asyncio.run(handle_terminal_connect(
                "admin-user-id", websocket,
                {"mode": "ssh", "worker_id": "worker-118"},
            ))

        message = websocket.messages[-1]
        self.assertEqual(message["type"], "terminal_error")
        self.assertTrue(message["credential_required"])
        self.assertEqual(message["device_host"], "hcq@172.16.14.118")

    def test_adb_terminal_holds_generation_fenced_claim_until_close(self):
        websocket = _FakeWebSocket()
        channel = _FakeChannel()
        registry = DeviceClaimRegistry(Path(self.tmp.name) / "claims.sqlite3")
        cluster = SimpleNamespace(repository=SimpleNamespace(claims=registry))
        with patch(
            "features.system.terminal_service.config_manager.load_config",
            return_value={},
        ), patch(
            "features.system.terminal_service.resolve_authorized_terminal_target",
            return_value=("ats-worker-controller", "localhost", "admin", "", "SERIAL-1"),
        ), patch(
            "features.cluster.get_cluster_service", return_value=cluster,
        ), patch(
            "features.system.terminal_service.config_manager.is_config_host_local",
            return_value=True,
        ), patch(
            "features.system.terminal_service.create_local_terminal_channel",
            return_value=channel,
        ), patch("features.system.terminal_output.threading.Thread.start"):
            asyncio.run(handle_terminal_connect(
                "admin-user-id", websocket, {"mode": "adb", "serial_no": "SERIAL-1"}
            ))

        connected = websocket.messages[-1]
        claim = registry.active_claim("ats-worker-controller:SERIAL-1")
        self.assertEqual(connected["type"], "terminal_connected")
        self.assertEqual(connected["lease_id"], claim["id"])
        self.assertEqual(connected["generation"], claim["generation"])
        self.assertEqual(claim["owner_id"], "admin-user-id")

        close_websocket_terminal(websocket)
        self.assertIsNone(registry.active_claim("ats-worker-controller:SERIAL-1"))
        self.assertTrue(channel.closed)

    def test_adb_terminal_leaves_final_command_echo_for_browser_readiness(self):
        """The output pump must receive the final adb command and device prompt.

        The browser recognizes a ready ADB pane from the command echo followed
        by an Android prompt. Waiting/draining after the final send consumed
        that evidence and made every successful shell look like a timeout.
        """
        websocket = _FakeWebSocket()
        channel = _FakeChannel()
        registry = DeviceClaimRegistry(Path(self.tmp.name) / "echo-claims.sqlite3")
        cluster = SimpleNamespace(repository=SimpleNamespace(claims=registry))
        with patch(
            "features.system.terminal_service.config_manager.load_config",
            return_value={},
        ), patch(
            "features.system.terminal_service.resolve_authorized_terminal_target",
            return_value=("ats-worker-controller", "localhost", "admin", "", "SERIAL-1"),
        ), patch(
            "features.cluster.get_cluster_service", return_value=cluster,
        ), patch(
            "features.system.terminal_service.config_manager.is_config_host_local",
            return_value=True,
        ), patch(
            "features.system.terminal_service.create_local_terminal_channel",
            return_value=channel,
        ), patch(
            "features.system.terminal_service._wait_for_shell_prompt",
            new_callable=AsyncMock,
        ) as wait_for_prompt, patch(
            "features.system.terminal_output.threading.Thread.start"
        ):
            asyncio.run(handle_terminal_connect(
                "admin-user-id", websocket, {"mode": "adb", "serial_no": "SERIAL-1"}
            ))

        self.assertEqual(wait_for_prompt.await_count, 3)
        self.assertEqual(channel.sent[-1], "/bin/true -s SERIAL-1 shell\n")
        close_websocket_terminal(websocket)

    def test_adb_terminal_input_closes_after_claim_revocation(self):
        websocket = _FakeWebSocket()
        channel = _FakeChannel()
        registry = DeviceClaimRegistry(Path(self.tmp.name) / "revoked-claims.sqlite3")
        cluster = SimpleNamespace(repository=SimpleNamespace(claims=registry))
        with patch(
            "features.system.terminal_service.config_manager.load_config", return_value={}
        ), patch(
            "features.system.terminal_service.resolve_authorized_terminal_target",
            return_value=("ats-worker-controller", "localhost", "admin", "", "SERIAL-1"),
        ), patch(
            "features.cluster.get_cluster_service", return_value=cluster
        ), patch(
            "features.system.terminal_service.config_manager.is_config_host_local",
            return_value=True,
        ), patch(
            "features.system.terminal_service.create_local_terminal_channel",
            return_value=channel,
        ), patch("features.system.terminal_output.threading.Thread.start"):
            asyncio.run(handle_terminal_connect(
                "admin-user-id", websocket, {"mode": "adb", "serial_no": "SERIAL-1"}
            ))

        registry.force_release("ats-worker-controller:SERIAL-1")
        asyncio.run(handle_terminal_input(
            "admin-user-id", websocket, {"input": "id\n"}
        ))

        self.assertTrue(channel.closed)
        self.assertEqual(websocket.messages[-1]["type"], "terminal_error")
        self.assertIn("租约", websocket.messages[-1]["error"])

    def test_adb_terminal_accepts_live_device_missing_from_stale_inventory(self):
        """库存滞后时实时探测命中 → 放行并回填库存（ADB Shell 启动失败修复）。"""
        websocket = _FakeWebSocket()
        channel = _FakeChannel()
        registry = DeviceClaimRegistry(Path(self.tmp.name) / "stale-claims.sqlite3")
        cluster = SimpleNamespace(
            config=SimpleNamespace(local_worker_id="ats-worker-controller"),
            repository=SimpleNamespace(
                claims=registry,
                list_devices=lambda worker_id: [],
                upsert_seen_device=lambda worker_id, serial: None,
            ),
        )
        with patch(
            "features.system.terminal_service.config_manager.load_config",
            return_value={},
        ), patch(
            "features.system.terminal_service.config_manager.is_config_host_local",
            return_value=True,
        ), patch(
            "features.cluster.get_cluster_service", return_value=cluster,
        ), patch(
            "features.system.terminal_service.create_local_terminal_channel",
            return_value=channel,
        ), patch(
            "features.system.terminal_service.subprocess.run",
            return_value=SimpleNamespace(
                stdout="List of devices attached\nNEW-SERIAL\tdevice\n", returncode=0
            ),
        ):
            asyncio.run(handle_terminal_connect(
                "admin-user-id", websocket, {"mode": "adb", "serial_no": "NEW-SERIAL"}
            ))

        self.assertEqual(websocket.messages[-1]["type"], "terminal_connected")

        close_websocket_terminal(websocket)

    def test_adb_terminal_rejects_serial_absent_from_inventory_and_live_probe(self):
        """库存与实时探测都找不到的序列号维持拒绝，不放宽归属边界。"""
        websocket = _FakeWebSocket()
        cluster = SimpleNamespace(
            config=SimpleNamespace(local_worker_id="ats-worker-controller"),
            repository=SimpleNamespace(
                list_devices=lambda worker_id: [],
                upsert_seen_device=lambda worker_id, serial: None,
            ),
        )
        with patch(
            "features.system.terminal_service.config_manager.load_config",
            return_value={},
        ), patch(
            "features.cluster.get_cluster_service", return_value=cluster,
        ), patch(
            "features.system.terminal_service.subprocess.run",
            return_value=SimpleNamespace(
                stdout="List of devices attached\n", returncode=0
            ),
        ):
            asyncio.run(handle_terminal_connect(
                "admin-user-id", websocket, {"mode": "adb", "serial_no": "GHOST-1"}
            ))

        message = websocket.messages[-1]
        self.assertEqual(message["type"], "terminal_error")
        self.assertIn("设备不属于所选 Worker", message["error"])

    def test_adb_terminal_rejects_offline_inventory_row_when_live_probe_misses(self):
        """库存 offline 的僵尸行必须实时探测确认，探测未命中 → 明确报离线。

        生产事故：设备拔走后库存快照残留 offline 行，终端校验只看
        composite_id 是否在库存里就放行，adb shell 报 device not found
        退回宿主提示符，前端只能显示"ADB Shell 启动失败"。
        """
        websocket = _FakeWebSocket()
        cluster = SimpleNamespace(
            config=SimpleNamespace(local_worker_id="ats-worker-controller"),
            repository=SimpleNamespace(
                list_devices=lambda worker_id: [
                    {"id": "ats-worker-controller:STALE-SERIAL", "state": "offline"}
                ],
                upsert_seen_device=lambda worker_id, serial: None,
            ),
        )
        with patch(
            "features.system.terminal_service.config_manager.load_config",
            return_value={},
        ), patch(
            "features.system.terminal_service.config_manager.is_config_host_local",
            return_value=True,
        ), patch(
            "features.cluster.get_cluster_service", return_value=cluster,
        ), patch(
            "features.system.terminal_service.subprocess.run",
            return_value=SimpleNamespace(
                stdout="List of devices attached\n", returncode=0
            ),
        ):
            asyncio.run(handle_terminal_connect(
                "admin-user-id", websocket, {"mode": "adb", "serial_no": "STALE-SERIAL"}
            ))

        message = websocket.messages[-1]
        self.assertEqual(message["type"], "terminal_error")
        self.assertIn("不在线", message["error"])

    def test_adb_terminal_accepts_offline_inventory_row_when_probe_confirms_device(self):
        """库存 offline 但实时探测在线（设备刚插回、快照未刷新）→ 放行。"""
        websocket = _FakeWebSocket()
        channel = _FakeChannel()
        registry = DeviceClaimRegistry(Path(self.tmp.name) / "stale-offline-claims.sqlite3")
        cluster = SimpleNamespace(
            config=SimpleNamespace(local_worker_id="ats-worker-controller"),
            repository=SimpleNamespace(
                claims=registry,
                list_devices=lambda worker_id: [
                    {"id": "ats-worker-controller:BACK-SERIAL", "state": "offline"}
                ],
                upsert_seen_device=lambda worker_id, serial: None,
            ),
        )
        with patch(
            "features.system.terminal_service.config_manager.load_config",
            return_value={},
        ), patch(
            "features.system.terminal_service.config_manager.is_config_host_local",
            return_value=True,
        ), patch(
            "features.cluster.get_cluster_service", return_value=cluster,
        ), patch(
            "features.system.terminal_service.create_local_terminal_channel",
            return_value=channel,
        ), patch(
            "features.system.terminal_service.subprocess.run",
            return_value=SimpleNamespace(
                stdout="List of devices attached\nBACK-SERIAL\tdevice\n", returncode=0
            ),
        ):
            asyncio.run(handle_terminal_connect(
                "admin-user-id", websocket, {"mode": "adb", "serial_no": "BACK-SERIAL"}
            ))

        self.assertEqual(websocket.messages[-1]["type"], "terminal_connected")

        close_websocket_terminal(websocket)


if __name__ == "__main__":
    unittest.main()
