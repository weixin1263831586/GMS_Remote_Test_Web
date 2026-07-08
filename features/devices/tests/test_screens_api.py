import json
import unittest
from contextlib import contextmanager
from unittest.mock import patch

from features.devices import screens_api
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
    async def test_scrcpy_response_uses_shared_novnc_url_helper(self):
        ssh_manager = FakeSshManager()

        with (
            patch.object(screens_api.runtime, "config_manager", FakeConfigManager()),
            patch.object(screens_api.runtime, "ssh_manager", ssh_manager),
            patch.object(screens_api.asyncio, "sleep", return_value=None),
        ):
            response = await screens_api.show_device_screens(
                DeviceActionRequest(devices=["ABC-123"])
            )

        body = json.loads(response.body)
        self.assertTrue(body["success"])
        self.assertEqual(
            body["vnc_sessions"][0]["url"],
            "http://192.168.0.2:6080/vnc.html?autoconnect=true&resize=scale",
        )
        curl_check = "http://192.168.0.2:6080/vnc.html?resize=scale --connect-timeout 3"
        self.assertTrue(any(curl_check in cmd for cmd in ssh_manager.commands))


if __name__ == "__main__":
    unittest.main()
