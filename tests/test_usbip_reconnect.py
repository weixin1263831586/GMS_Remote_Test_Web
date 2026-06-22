import asyncio
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from tempfile import TemporaryDirectory
from unittest.mock import patch

from core.config import ConfigManager
from core.schemas import USBIPStartRequest
from core.state import global_state
from core.usbip import USBIPManager, parse_usbipd_android_busids


class UsbipCredentialTests(unittest.TestCase):
    def test_usbipd_persisted_guid_is_not_treated_as_busid(self):
        output = """
Connected:
BUSID  VID:PID    DEVICE                                                        STATE
1-13   0403:6001  USB Serial Converter                                          Not shared
Persisted:
GUID                                  DEVICE
85aba5e0-8dbc-4d80-9d24-23778558f81e  Android ADB Interface
"""

        self.assertEqual(parse_usbipd_android_busids(output), [])

    def test_attach_devices_reports_only_successful_attach_commands(self):
        class FakeSshManager:
            def execute_command(self, ssh, cmd, timeout=None, get_pty=False):
                if cmd == "adb devices":
                    return ("List of devices attached\n", "", 0)
                if cmd.startswith("sudo usbip attach"):
                    return ("", "failed to attach", 1)
                return ("", "", 0)

        manager = USBIPManager()
        manager.ssh_manager = FakeSshManager()

        attached, devices = manager._attach_devices(object(), "172.16.14.66", ["85aba5e0-8dbc"])

        self.assertEqual(attached, [])
        self.assertEqual(devices, [])

    def test_attach_devices_waits_until_adb_serial_appears(self):
        class FakeSshManager:
            def __init__(self):
                self.adb_calls = 0

            def execute_command(self, ssh, cmd, timeout=None, get_pty=False):
                if cmd == "adb devices":
                    self.adb_calls += 1
                    if self.adb_calls < 4:
                        return ("List of devices attached\nLOCAL001\tdevice\n", "", 0)
                    return ("List of devices attached\nLOCAL001\tdevice\nUSBIP001\tdevice\n", "", 0)
                if cmd.startswith("sudo usbip attach"):
                    return ("attached", "", 0)
                return ("", "", 0)

        manager = USBIPManager()
        manager.ssh_manager = FakeSshManager()

        with patch("core.usbip.time.sleep", return_value=None):
            attached, devices = manager._attach_devices(object(), "172.16.14.66", ["1-1"])

        self.assertEqual(attached, ["1-1"])
        self.assertEqual(devices, ["USBIP001"])
        self.assertGreaterEqual(manager.ssh_manager.adb_calls, 4)

    def test_device_host_password_matches_full_host_before_username_fallback(self):
        manager = ConfigManager()
        config = {
            "client_ssh_credentials": [
                {"device_host": "hcq@172.16.14.66", "username": "hcq", "host": "172.16.14.66", "password": "pw66"},
                {"device_host": "hcq@172.16.14.67", "username": "hcq", "host": "172.16.14.67", "password": "pw67"},
                {"username": "legacy", "password": "legacy-pw"},
            ]
        }

        self.assertEqual(manager.find_device_host_password("hcq@172.16.14.66", config), "pw66")
        self.assertEqual(manager.find_device_host_password("hcq@172.16.14.67", config), "pw67")
        self.assertEqual(manager.find_device_host_password("legacy@10.0.0.8", config), "legacy-pw")

    def test_upsert_device_host_password_preserves_runtime_and_updates_host(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "core").mkdir()
            configs = root / "configs"
            configs.mkdir()
            (configs / "config.json").write_text("{}", encoding="utf-8")
            (configs / "config_runtime.json").write_text(
                json.dumps({
                    "sidebar_order": ["test"],
                    "client_ssh_credentials": [
                        {"device_host": "hcq@172.16.14.66", "username": "hcq", "host": "172.16.14.66", "password": "old"}
                    ],
                }),
                encoding="utf-8",
            )

            manager = ConfigManager(base_dir=str(root / "core"))

            self.assertTrue(manager.upsert_device_host_password("hcq@172.16.14.66", "new"))
            self.assertTrue(manager.upsert_device_host_password("user@172.16.14.67:2222", "pw67"))

            runtime = json.loads((configs / "config_runtime.json").read_text(encoding="utf-8"))
            self.assertEqual(runtime["sidebar_order"], ["test"])
            self.assertEqual(manager.find_device_host_password("hcq@172.16.14.66"), "new")
            self.assertEqual(manager.find_device_host_password("user@172.16.14.67:2222"), "pw67")
            self.assertEqual(len(runtime["client_ssh_credentials"]), 2)

    def test_usbip_connect_persists_submitted_password_after_success(self):
        import routers.integrations as integrations

        saved = {}

        class FakeConfigManager:
            def load_config(self):
                return {"device_pswd": "", "client_ssh_credentials": []}

            def upsert_device_host_password(self, device_host, password):
                saved["device_host"] = device_host
                saved["password"] = password
                return True

            def get_runtime_config(self):
                return {}

            def save_runtime_config(self, data):
                saved["runtime"] = data
                return True

        class FakeUsbipManager:
            def start_usbip(self, device_host, device_password, usbip_attach_host=None):
                saved["start_args"] = (device_host, device_password, usbip_attach_host)
                return {"success": True, "message": "ok", "device_list": ["USBIP001"]}

        request = SimpleNamespace(headers={}, client=SimpleNamespace(host="127.0.0.1"))
        req = USBIPStartRequest(device_host="hcq@172.16.14.66", device_password="secret")

        with patch.object(integrations, "config_manager", FakeConfigManager()), \
                patch.object(integrations, "usbip_manager", FakeUsbipManager()), \
                patch.object(integrations, "get_client_id_from_request", return_value="hcq@172.16.14.66"):
            response = asyncio.run(integrations.start_usbip(req=req, request=request, help=False))

        self.assertEqual(response.status_code, 200)
        body = json.loads(response.body.decode("utf-8"))
        self.assertTrue(body["success"])
        self.assertEqual(saved["start_args"], ("hcq@172.16.14.66", "secret", None))
        self.assertEqual(saved["device_host"], "hcq@172.16.14.66")
        self.assertEqual(saved["password"], "secret")

    def test_usbip_connect_waits_for_adb_device_before_reporting_success(self):
        import routers.integrations as integrations

        class FakeConfigManager:
            def load_config(self):
                return {"device_pswd": "secret", "client_ssh_credentials": []}

        class FakeUsbipManager:
            def start_usbip(self, device_host, device_password, usbip_attach_host=None):
                return {"success": True, "message": "attached", "devices": ["1-1"], "device_list": []}

        request = SimpleNamespace(headers={}, client=SimpleNamespace(host="127.0.0.1"))
        req = USBIPStartRequest(device_host="hcq@172.16.14.66")

        with patch.object(integrations, "config_manager", FakeConfigManager()), \
                patch.object(integrations, "usbip_manager", FakeUsbipManager()), \
                patch.object(integrations, "get_client_id_from_request", return_value="hcq@172.16.14.66"):
            response = asyncio.run(integrations.start_usbip(req=req, request=request, help=False))

        self.assertEqual(response.status_code, 200)
        body = json.loads(response.body.decode("utf-8"))
        self.assertFalse(body["success"])
        self.assertIn("ADB", body["error"])

    def test_suppressed_usbip_auto_connect_is_blocked_until_manual_connect(self):
        import core.usbip_reconnect as reconnect
        import routers.integrations as integrations

        calls = []

        class FakeConfigManager:
            def load_config(self):
                return {"device_pswd": "secret", "client_ssh_credentials": []}

            def get_runtime_config(self):
                return {}

            def save_runtime_config(self, data):
                return True

        class FakeUsbipManager:
            def start_usbip(self, device_host, device_password, usbip_attach_host=None):
                calls.append((device_host, device_password, usbip_attach_host))
                return {"success": True, "message": "ok", "device_list": ["USBIP001"]}

        request = SimpleNamespace(headers={}, client=SimpleNamespace(host="127.0.0.1"))
        reconnect.suppress_usbip_reconnect("hcq@172.16.14.66", ["USBIP001"])
        try:
            with patch.object(integrations, "config_manager", FakeConfigManager()), \
                    patch.object(integrations, "usbip_manager", FakeUsbipManager()), \
                    patch.object(integrations, "get_client_id_from_request", return_value="hcq@172.16.14.66"):
                auto_response = asyncio.run(integrations.start_usbip(
                    req=USBIPStartRequest(device_host="hcq@172.16.14.66"),
                    request=request,
                    help=False,
                ))
                manual_response = asyncio.run(integrations.start_usbip(
                    req=USBIPStartRequest(device_host="hcq@172.16.14.66", manual_connect=True),
                    request=request,
                    help=False,
                ))
        finally:
            reconnect.clear_usbip_reconnect_suppression("hcq@172.16.14.66", ["USBIP001"])

        auto_body = json.loads(auto_response.body.decode("utf-8"))
        manual_body = json.loads(manual_response.body.decode("utf-8"))
        self.assertFalse(auto_body["success"])
        self.assertTrue(auto_body["manual_disconnect_suppressed"])
        self.assertTrue(manual_body["success"])
        self.assertEqual(calls, [("hcq@172.16.14.66", "secret", None)])
        self.assertFalse(reconnect.is_usbip_reconnect_suppressed("hcq@172.16.14.66", "USBIP001"))

    def test_failed_manual_usbip_connect_keeps_auto_reconnect_suppressed(self):
        import core.usbip_reconnect as reconnect
        import routers.integrations as integrations

        class FakeConfigManager:
            def load_config(self):
                return {"device_pswd": "secret", "client_ssh_credentials": []}

        class FakeUsbipManager:
            def start_usbip(self, device_host, device_password, usbip_attach_host=None):
                return {"success": False, "error": "未找到Android设备"}

        request = SimpleNamespace(headers={}, client=SimpleNamespace(host="127.0.0.1"))
        reconnect.suppress_usbip_reconnect("hcq@172.16.14.66", ["USBIP001"])
        try:
            with patch.object(integrations, "config_manager", FakeConfigManager()), \
                    patch.object(integrations, "usbip_manager", FakeUsbipManager()), \
                    patch.object(integrations, "get_client_id_from_request", return_value="hcq@172.16.14.66"):
                response = asyncio.run(integrations.start_usbip(
                    req=USBIPStartRequest(device_host="hcq@172.16.14.66", manual_connect=True),
                    request=request,
                    help=False,
                ))
            body = json.loads(response.body.decode("utf-8"))
            self.assertFalse(body["success"])
            self.assertTrue(reconnect.is_usbip_reconnect_suppressed("hcq@172.16.14.66", "USBIP001"))
        finally:
            reconnect.clear_usbip_reconnect_suppression("hcq@172.16.14.66", ["USBIP001"])

    def test_removed_usbip_device_schedules_server_side_reconnect(self):
        import core.usbip_reconnect as reconnect

        with global_state.usbip_devices_source_lock:
            old_sources = dict(global_state.usbip_devices_source)
            global_state.usbip_devices_source.clear()
            global_state.usbip_devices_source["USBIP001"] = {
                "source": "hcq@172.16.14.66",
                "timestamp": 1,
            }

        scheduled = []
        try:
            with patch.object(reconnect, "schedule_usbip_reconnect", side_effect=lambda host, reason="": scheduled.append((host, reason)) or True):
                hosts = reconnect.schedule_usbip_reconnect_for_removed_devices(["USBIP001"], reason="test")
        finally:
            with global_state.usbip_devices_source_lock:
                global_state.usbip_devices_source.clear()
                global_state.usbip_devices_source.update(old_sources)

        self.assertEqual(hosts, ["hcq@172.16.14.66"])
        self.assertEqual(scheduled[0][0], "hcq@172.16.14.66")

    def test_manual_usbip_disconnect_suppresses_server_side_reconnect(self):
        import core.usbip_reconnect as reconnect

        with global_state.usbip_devices_source_lock:
            old_sources = dict(global_state.usbip_devices_source)
            global_state.usbip_devices_source.clear()
            global_state.usbip_devices_source["USBIP001"] = {
                "source": "hcq@172.16.14.66",
                "timestamp": 1,
            }

        scheduled = []
        try:
            reconnect.suppress_usbip_reconnect("hcq@172.16.14.66", ["USBIP001"])
            with patch.object(reconnect, "schedule_usbip_reconnect", side_effect=lambda host, reason="": scheduled.append((host, reason)) or True):
                hosts = reconnect.schedule_usbip_reconnect_for_removed_devices(["USBIP001"], reason="manual disconnect")
        finally:
            reconnect.clear_usbip_reconnect_suppression("hcq@172.16.14.66", ["USBIP001"])
            with global_state.usbip_devices_source_lock:
                global_state.usbip_devices_source.clear()
                global_state.usbip_devices_source.update(old_sources)

        self.assertEqual(hosts, [])
        self.assertEqual(scheduled, [])

    def test_usbip_disconnect_finds_devices_from_runtime_sources(self):
        import routers.integrations as integrations

        old_sources = dict(global_state.usbip_devices_source)
        old_manager_sources = dict(integrations.usbip_manager.device_sources)
        try:
            with global_state.usbip_devices_source_lock:
                global_state.usbip_devices_source.clear()
            integrations.usbip_manager.device_sources.clear()

            with patch.object(integrations.config_manager, "get_runtime_config", return_value={
                "usbip_devices_source": {
                    "USBIP001": {"source": "hcq@172.16.14.66", "timestamp": 1},
                    "OTHER001": {"source": "hcq@172.16.14.67", "timestamp": 1},
                }
            }):
                self.assertEqual(
                    integrations._usbip_devices_for_host("hcq@172.16.14.66"),
                    ["USBIP001"],
                )
        finally:
            with global_state.usbip_devices_source_lock:
                global_state.usbip_devices_source.clear()
                global_state.usbip_devices_source.update(old_sources)
            integrations.usbip_manager.device_sources.clear()
            integrations.usbip_manager.device_sources.update(old_manager_sources)

    def test_device_list_refresh_keeps_usbip_source_for_reconnect(self):
        import routers.devices as devices_router

        old_sources = dict(global_state.usbip_devices_source)
        old_cache = dict(global_state.device_cache)
        try:
            with global_state.usbip_devices_source_lock:
                global_state.usbip_devices_source.clear()
                global_state.usbip_devices_source["USBIP001"] = {
                    "source": "hcq@172.16.14.66",
                    "timestamp": 1,
                }
            with global_state.device_cache_lock:
                global_state.device_cache = {"devices": [], "timestamp": 0}

            request = SimpleNamespace(headers={}, client=SimpleNamespace(host="127.0.0.1"))
            with patch.object(devices_router.device_manager, "get_connected_devices", return_value=["LOCAL001"]), \
                    patch.object(devices_router, "get_client_id_from_request", return_value="hcq@127.0.0.1"), \
                    patch.object(devices_router.client_manager, "get_client_id", return_value="hcq@127.0.0.1"):
                asyncio.run(devices_router.get_connected_devices(request=request, help=False, force_refresh=True))

            self.assertIn("USBIP001", global_state.usbip_devices_source)
        finally:
            with global_state.usbip_devices_source_lock:
                global_state.usbip_devices_source.clear()
                global_state.usbip_devices_source.update(old_sources)
            with global_state.device_cache_lock:
                global_state.device_cache = old_cache

    def test_usbip_reboot_returns_without_waiting_for_adb_online(self):
        import routers.devices as devices_router
        from core.schemas import DeviceActionRequest

        old_sources = dict(global_state.usbip_devices_source)
        calls = []
        try:
            with global_state.usbip_devices_source_lock:
                global_state.usbip_devices_source.clear()

            def fake_reboot_device(device_id, ssh=None, wait_for_online=True):
                calls.append((device_id, wait_for_online))
                return {"success": True, "back_online": False, "wait_time": 0.0}

            with patch.object(devices_router.config_manager, "get_runtime_config", return_value={
                "usbip_devices_source": {
                    "USBIP001": {"source": "hcq@172.16.14.66", "timestamp": 1}
                }
            }), patch.object(devices_router.device_manager, "reboot_device", side_effect=fake_reboot_device):
                response = asyncio.run(devices_router.reboot_devices(DeviceActionRequest(devices=["USBIP001"])))

            body = json.loads(response.body.decode("utf-8"))
            self.assertTrue(body["success"])
            self.assertEqual(calls, [("USBIP001", False)])
            self.assertTrue(body["data"]["results"][0]["usbip_reconnect_expected"])
        finally:
            with global_state.usbip_devices_source_lock:
                global_state.usbip_devices_source.clear()
                global_state.usbip_devices_source.update(old_sources)

    def test_frontend_submits_device_host_and_autoreconnects_usbip_disconnects(self):
        text = Path("static/js/app.js").read_text(encoding="utf-8", errors="ignore")

        self.assertIn("device_host: deviceHost", text)
        self.assertIn("scheduleUsbipReconnect", text)
        self.assertIn("USBIP_RECONNECT_MAX_ATTEMPTS", text)
        self.assertIn("USBIP_RECONNECT_INITIAL_DELAY_MS", text)
        self.assertIn("USB/IP 设备正在重启", text)
        self.assertIn("usbipManualDisconnectUntil", text)
        self.assertIn("data.source !== 'usbip_disconnect'", text)
        self.assertIn("manual_connect: true", text)
        self.assertIn("isUsbipAdbReady", text)
        self.assertNotIn("result.success || result.devices", text)
        self.assertNotIn("Button reset due to device disconnect", text)
