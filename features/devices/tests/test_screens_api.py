import json
import unittest
from contextlib import asynccontextmanager, contextmanager
from types import SimpleNamespace
from unittest.mock import patch

from features.devices import screens_api
from features.devices import support as device_support
from features.devices.models import DeviceActionRequest


class FakeConfigManager:
    def load_config(self):
        return {"ubuntu_host": "192.168.0.2", "ubuntu_user": "test user"}

    def get_ubuntu_user(self, _config):
        return "test user"

    def get_ubuntu_host(self, _config):
        return "192.168.0.2"


class FakeSshManager:
    def __init__(self):
        self.commands = []

    @contextmanager
    def optional_connection(self, _config):
        yield object()

    @asynccontextmanager
    async def async_optional_connection(self, config):
        with self.optional_connection(config) as ssh:
            yield ssh

    def execute_command(self, _ssh, command, timeout=None):
        self.commands.append(command)
        if command.startswith("curl "):
            return "200", "", 0
        if command == "which scrcpy":
            return "/usr/bin/scrcpy\n", "", 0
        if "pgrep -f" in command:
            return "RUNNING\n", "", 0
        return "", "", 0


class DeviceScreensApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_scrcpy_requires_explicit_devices(self):
        with (
            patch.object(
                screens_api.runtime,
                "config_manager",
                FakeConfigManager(),
            ),
            patch.object(
                screens_api.runtime,
                "ssh_manager",
                FakeSshManager(),
            ),
        ):
            response = await screens_api.show_device_screens(
                DeviceActionRequest(devices=[]),
                SimpleNamespace(state=SimpleNamespace()),
            )

        body = json.loads(response.body)
        self.assertEqual(response.status_code, 400)
        self.assertIn("explicit device selection", body["error"])

    async def test_scrcpy_response_uses_shared_novnc_url_helper(self):
        ssh_manager = FakeSshManager()

        with (
            patch.object(screens_api.runtime, "config_manager", FakeConfigManager()),
            patch.object(screens_api.runtime, "ssh_manager", ssh_manager),
            patch.object(
                screens_api.runtime,
                "get_client_id_from_request",
                return_value="user-id",
            ),
            patch.object(
                device_support,
                "acquire_device_operation_claim",
                return_value=(
                    "operation:scrcpy:test",
                    [{
                        "id": "claim-1",
                        "device_key": "ats-worker-controller:ABC-123",
                        "generation": 1,
                        "owner_id": "user-id",
                    }],
                    None,
                ),
            ),
            patch.object(device_support, "release_device_operation_claim"),
            patch.object(device_support, "audit_device_operation"),
            patch.object(screens_api.asyncio, "sleep", return_value=None),
        ):
            response = await screens_api.show_device_screens(
                DeviceActionRequest(devices=["ABC-123"]),
                SimpleNamespace(state=SimpleNamespace()),
            )

        body = json.loads(response.body)
        self.assertTrue(body["success"])
        self.assertEqual(
            body["vnc_sessions"][0]["url"],
            "http://192.168.0.2:6080/vnc.html?autoconnect=true&resize=scale",
        )
        curl_check = "http://192.168.0.2:6080/vnc.html?resize=scale --connect-timeout 3"
        self.assertTrue(any(curl_check in cmd for cmd in ssh_manager.commands))
        self.assertTrue(any("--no-control" in cmd for cmd in ssh_manager.commands))


if __name__ == "__main__":
    unittest.main()
