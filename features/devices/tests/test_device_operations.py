import asyncio
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

from features.devices import operations_api
from features.devices.models import WifiConnectRequest


class _ConfigManager:
    def load_config(self):
        return {"wifi": {"ssid": "Lab Wifi", "password": "lab password"}}

    def get_ubuntu_host(self, _config):
        return "192.168.0.2"

    def get_ubuntu_user(self, _config):
        return "test user"

    def get_wifi_defaults(self, config=None):
        wifi = (config or self.load_config()).get("wifi") or {}
        return {"ssid": wifi["ssid"], "password": wifi["password"]}


class _SshManager:
    def __init__(self):
        self.commands = []

    @contextmanager
    def optional_connection(self, config):
        yield object()

    def execute_command(self, ssh, command):
        self.commands.append(command)
        return "", "", 0


class DeviceOperationsTests(unittest.TestCase):
    def test_management_payload_uses_request_client_id_for_self_lock(self):
        with (
            patch.object(
                operations_api.device_lock_manager,
                "get_all_locks",
                return_value={
                    "ABC-123": {
                        "client_id": "alice@10.0.0.8",
                        "username": "alice",
                    }
                },
            ),
            patch.object(
                operations_api.runtime,
                "config_manager",
                _ConfigManager(),
            ),
            patch.object(
                operations_api.runtime,
                "global_state",
                SimpleNamespace(usbip_devices_source={}),
            ),
        ):
            payload = operations_api._build_devices_management_payload(
                ["ABC-123"],
                {"ABC-123": {"serial_no": "ABC-123"}},
                {},
                client_id="alice@10.0.0.8",
            )

        self.assertTrue(payload["devices"][0]["locked_by_self"])

    def test_connect_wifi_uses_configured_defaults_when_request_omits_credentials(self):
        ssh_manager = _SshManager()
        original_config_manager = operations_api.runtime.config_manager
        original_ssh_manager = operations_api.runtime.ssh_manager
        operations_api.runtime.config_manager = _ConfigManager()
        operations_api.runtime.ssh_manager = ssh_manager
        try:
            response = asyncio.run(
                operations_api.connect_wifi(WifiConnectRequest(devices=["ABC-123"]))
            )
        finally:
            operations_api.runtime.config_manager = original_config_manager
            operations_api.runtime.ssh_manager = original_ssh_manager

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(ssh_manager.commands), 1)
        self.assertIn("'Lab Wifi'", ssh_manager.commands[0])
        self.assertIn("'lab password'", ssh_manager.commands[0])


if __name__ == "__main__":
    unittest.main()
