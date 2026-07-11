import asyncio
import threading
import unittest
from contextlib import asynccontextmanager, contextmanager
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

    def get_runtime_config(self):
        return {
            "usbip_devices_source": {
                "USBIP001": {"source": "hcq@172.16.14.66", "timestamp": 1},
            }
        }


class _SshManager:
    def __init__(self):
        self.commands = []

    @contextmanager
    def optional_connection(self, config):
        yield object()

    @asynccontextmanager
    async def async_optional_connection(self, config):
        with self.optional_connection(config) as ssh:
            yield ssh

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
                SimpleNamespace(
                    usbip_devices_source={},
                    usbip_devices_source_lock=threading.RLock(),
                ),
            ),
        ):
            payload = operations_api._build_devices_management_payload(
                ["ABC-123"],
                {"ABC-123": {"serial_no": "ABC-123"}},
                {},
                client_id="alice@10.0.0.8",
            )

        self.assertTrue(payload["devices"][0]["locked_by_self"])

    def test_management_payload_uses_persisted_usbip_source(self):
        with (
            patch.object(operations_api.device_lock_manager, "get_all_locks", return_value={}),
            patch.object(operations_api.runtime, "config_manager", _ConfigManager()),
            patch.object(operations_api, "_active_usbip_serials", return_value={"USBIP001"}),
            patch.object(
                operations_api.runtime,
                "global_state",
                SimpleNamespace(
                    usbip_devices_source={},
                    usbip_devices_source_lock=threading.RLock(),
                ),
            ),
        ):
            payload = operations_api._build_devices_management_payload(
                ["USBIP001", "LOCAL001"],
                {
                    "USBIP001": {"serial_no": "USBIP001"},
                    "LOCAL001": {"serial_no": "LOCAL001"},
                },
                {},
                client_id="alice@10.0.0.8",
            )

        devices = {device["device_id"]: device for device in payload["devices"]}
        self.assertEqual(devices["USBIP001"]["source_type"], "usbip")
        self.assertEqual(devices["USBIP001"]["source_host"], "hcq@172.16.14.66")
        self.assertEqual(devices["LOCAL001"]["source_type"], "local")

    def test_management_payload_clears_stale_usbip_source_without_active_port(self):
        runtime_config = {
            "usbip_devices_source": {
                "USBIP001": {"source": "hcq@172.16.14.66", "timestamp": 1},
            }
        }

        class ConfigManager(_ConfigManager):
            def get_runtime_config(self):
                return runtime_config

            def save_runtime_config(self, config):
                saved_config = dict(config)
                runtime_config.clear()
                runtime_config.update(saved_config)
                return True

        global_state = SimpleNamespace(
            usbip_devices_source={
                "USBIP001": {"source": "hcq@172.16.14.66", "timestamp": 1},
            },
            usbip_devices_source_lock=threading.RLock(),
        )

        with (
            patch.object(operations_api.device_lock_manager, "get_all_locks", return_value={}),
            patch.object(operations_api.runtime, "config_manager", ConfigManager()),
            patch.object(operations_api.runtime, "global_state", global_state),
            patch.object(operations_api, "_active_usbip_serials", return_value=set()),
        ):
            payload = operations_api._build_devices_management_payload(
                ["USBIP001"],
                {"USBIP001": {"serial_no": "USBIP001"}},
                {},
                client_id="alice@10.0.0.8",
            )

        self.assertEqual(payload["devices"][0]["source_type"], "local")
        self.assertNotIn("USBIP001", global_state.usbip_devices_source)
        self.assertNotIn("USBIP001", runtime_config.get("usbip_devices_source", {}))

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
