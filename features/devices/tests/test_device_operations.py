import asyncio
import json
import threading
import unittest
from contextlib import asynccontextmanager, contextmanager
from types import SimpleNamespace
from unittest.mock import patch

from starlette.requests import Request

from features.auth import CurrentUser
from features.devices import device_lock_manager, management_api, operations_api
from features.devices.models import DeviceActionRequest, WifiConnectRequest
from features.devices.utils import DeviceUtils


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
    @staticmethod
    def request_for(username="alice"):
        request = Request({
            "type": "http",
            "method": "POST",
            "path": "/",
            "headers": [],
            "client": ("127.0.0.1", 1234),
        })
        request.state.current_user = CurrentUser(
            id=f"id-{username}", username=username, role="user"
        )
        return request

    def test_management_payload_uses_request_client_id_for_self_lock(self):
        with (
            patch.object(
                management_api.device_lock_manager,
                "get_all_locks",
                return_value={
                    "ABC-123": {
                        "client_id": "alice@10.0.0.8",
                        "username": "alice",
                    }
                },
            ),
            patch.object(
                management_api.runtime,
                "config_manager",
                _ConfigManager(),
            ),
            patch.object(
                management_api.runtime,
                "global_state",
                SimpleNamespace(
                    usbip_devices_source={},
                    usbip_devices_source_lock=threading.RLock(),
                ),
            ),
        ):
            payload = management_api._build_devices_management_payload(
                ["ABC-123"],
                {"ABC-123": {"serial_no": "ABC-123"}},
                {},
                client_id="alice@10.0.0.8",
            )

        self.assertTrue(payload["devices"][0]["locked_by_self"])

    def test_fastboot_parser_and_protocol_merge_deduplicate_serials(self):
        self.assertEqual(
            DeviceUtils.parse_fastboot_devices(
                "FB001\tfastboot\nFB002 fastboot\nFB003 fastbootd\n"
                "malformed extra-value\n"
            ),
            ["FB001", "FB002", "FB003"],
        )
        devices, protocols = management_api._merge_device_protocols(
            ["ADB001", "SWITCHING"],
            ["SWITCHING", "FB001"],
        )
        self.assertEqual(devices, ["ADB001", "SWITCHING", "FB001"])
        self.assertEqual(protocols["ADB001"], "adb")
        self.assertEqual(protocols["SWITCHING"], "fastboot")
        self.assertEqual(protocols["FB001"], "fastboot")

    def test_management_endpoint_merges_adb_and_fastboot_inventory(self):
        def run_local(command, _timeout):
            if command == "adb devices":
                return "List of devices attached\nADB001\tdevice\n", "", 0
            self.assertIn("adb -s ADB001 shell", command)
            return (
                "===DEVICE:ADB001===\nADB001\nModel\n14\n90\nSoC\n",
                "",
                0,
            )

        with (
            patch.object(management_api.runtime, "config_manager", _ConfigManager()),
            patch.object(
                management_api.runtime,
                "global_state",
                SimpleNamespace(
                    device_cache={"devices": [], "timestamp": 0},
                    device_cache_lock=threading.RLock(),
                    usbip_devices_source={},
                    usbip_devices_source_lock=threading.RLock(),
                ),
            ),
            patch.object(
                management_api.runtime,
                "get_client_id_from_request",
                return_value="alice",
            ),
            patch.object(
                management_api.runtime,
                "run_local_shell_command",
                side_effect=run_local,
            ),
            patch.object(management_api, "is_local_host", return_value=True),
            patch.object(management_api, "has_blocked_adb_process", return_value=False),
            patch.object(
                management_api.device_manager,
                "get_fastboot_devices",
                return_value=["FB001"],
            ),
            patch.object(management_api.device_lock_manager, "get_all_locks", return_value={}),
            patch.object(management_api, "_known_usbip_sources", return_value={}),
            patch("features.users.auto_assign_new_devices", return_value=[]),
            patch("features.users.build_device_group_map", return_value={}),
        ):
            response = asyncio.run(
                management_api.devices_management(self.request_for())
            )

        devices = {
            item["device_id"]: item
            for item in json.loads(response.body)["devices"]
        }
        self.assertEqual(devices["ADB001"]["protocol"], "adb")
        self.assertEqual(devices["ADB001"]["status"], "online")
        self.assertEqual(devices["FB001"]["protocol"], "fastboot")
        self.assertEqual(devices["FB001"]["status"], "fastboot")
        self.assertEqual(devices["FB001"]["model"], "")

    def test_management_payload_hides_another_users_identity_and_lease(self):
        payload = {
            "devices": [{
                "device_id": "ABC-123",
                "locked_by": "bob",
                "locked_username": "bob",
                "locked_client_id": "id-bob",
                "locked_by_self": False,
                "lease_id": "lease-secret",
                "lease_generation": 7,
            }]
        }

        sanitized = management_api._sanitize_management_payload(
            payload,
            principal=CurrentUser(id="id-alice", username="alice", role="user"),
        )

        device = sanitized["devices"][0]
        self.assertEqual(device["locked_by"], "occupied")
        self.assertEqual(device["locked_username"], "")
        self.assertEqual(device["locked_client_id"], "")
        self.assertEqual(device["lease_id"], "")
        self.assertEqual(device["lease_generation"], 0)

    def test_management_payload_uses_persisted_usbip_source(self):
        with (
            patch.object(management_api.device_lock_manager, "get_all_locks", return_value={}),
            patch.object(management_api.runtime, "config_manager", _ConfigManager()),
            patch.object(management_api, "_active_usbip_serials", return_value={"USBIP001"}),
            patch.object(
                management_api.runtime,
                "global_state",
                SimpleNamespace(
                    usbip_devices_source={},
                    usbip_devices_source_lock=threading.RLock(),
                ),
            ),
        ):
            payload = management_api._build_devices_management_payload(
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

    def test_management_payload_labels_local_adb_proxy_import(self):
        with (
            patch.object(
                management_api.device_lock_manager,
                "get_all_locks",
                return_value={},
            ),
            patch.object(management_api.runtime, "config_manager", _ConfigManager()),
            patch.object(management_api, "_known_usbip_sources", return_value={}),
            patch.object(
                management_api,
                "_local_adb_proxy_sources",
                return_value={
                    "RK3576GMS6": {
                        "source_worker_id": "ats-worker-118",
                        "source_serial": "RK3576GMS6",
                        "target_worker_id": "worker-local",
                    },
                },
            ),
        ):
            payload = management_api._build_devices_management_payload(
                ["RK3576GMS6"],
                {"RK3576GMS6": {"serial_no": "RK3576GMS6"}},
                {},
                client_id="alice@10.0.0.8",
            )

        device = payload["devices"][0]
        self.assertEqual(device["source_type"], "adb_proxy")
        self.assertEqual(device["transport"], "adb_proxy")
        self.assertEqual(
            device["source_host"],
            "ats-worker-118 → worker-local",
        )
        self.assertEqual(
            device["adb_proxy_source_worker_id"],
            "ats-worker-118",
        )

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
            patch.object(management_api.device_lock_manager, "get_all_locks", return_value={}),
            patch.object(management_api.runtime, "config_manager", ConfigManager()),
            patch.object(management_api.runtime, "global_state", global_state),
            patch.object(management_api, "_active_usbip_serials", return_value=set()),
        ):
            payload = management_api._build_devices_management_payload(
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
                operations_api.connect_wifi(
                    WifiConnectRequest(devices=["ABC-123"]),
                    self.request_for(),
                )
            )
        finally:
            operations_api.runtime.config_manager = original_config_manager
            operations_api.runtime.ssh_manager = original_ssh_manager

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(ssh_manager.commands), 1)
        self.assertIn("'Lab Wifi'", ssh_manager.commands[0])
        self.assertIn("'lab password'", ssh_manager.commands[0])

    def test_local_mutation_holds_fenced_claim_until_side_effect_finishes(self):
        serial = "MUTATION-CLAIM-DEVICE"
        device_lock_manager.force_unlock_device(serial)
        observed = {}

        def reboot(device_id, _ssh=None, _wait_for_online=True):
            claim = device_lock_manager.get_lock_status(device_id)
            observed.update(claim or {})
            return {"success": True}

        with (
            patch.object(
                operations_api.device_manager,
                "reboot_device",
                side_effect=reboot,
            ),
            patch.object(operations_api, "_known_usbip_device_ids", return_value=set()),
            patch.object(operations_api.runtime, "config_manager", _ConfigManager()),
            patch.object(
                operations_api.runtime,
                "global_state",
                SimpleNamespace(
                    usbip_devices_source={},
                    usbip_devices_source_lock=threading.RLock(),
                ),
            ),
        ):
            response = asyncio.run(
                operations_api.reboot_devices(
                    DeviceActionRequest(devices=[serial]),
                    self.request_for("alice"),
                )
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(observed["client_id"], "id-alice")
        self.assertTrue(observed["lease_id"].startswith("claim-"))
        self.assertGreaterEqual(observed["generation"], 1)
        self.assertIsNone(device_lock_manager.get_lock_status(serial))


if __name__ == "__main__":
    unittest.main()
