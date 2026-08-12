from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

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


class _FakeChannel:
    def __init__(self):
        self.closed = False

    def resize_pty(self, **_kwargs):
        return None

    def send(self, value):
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


if __name__ == "__main__":
    unittest.main()
