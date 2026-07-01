import asyncio
import json
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from features.devices.models import DeviceActionRequest, USBIPStartRequest
from features.devices.runtime import configure_runtime
from features.devices.usbip import (
    USBIPManager,
    find_device_host_password,
    parse_adb_device_states,
    parse_fastboot_devices,
    parse_usbipd_android_busids,
)


global_state = SimpleNamespace(
    usbip_devices_source={},
    usbip_devices_source_lock=threading.RLock(),
    usbip_states={},
    usbip_states_lock=threading.RLock(),
    device_cache={"devices": [], "timestamp": 0},
    device_cache_lock=threading.RLock(),
    user_states={},
    user_states_lock=threading.RLock(),
)

class EmptyConfigManager:
    def get_runtime_config(self):
        return {}


configure_runtime(
    selected_config_manager=EmptyConfigManager(),
    selected_global_state=global_state,
    selected_ssh_manager=None,
    selected_store_notification=None,
    selected_generate_help_or_continue=None,
    selected_get_client_id_from_request=None,
    selected_probe_windows_usbipd=None,
    selected_resolve_tailscale_device_host=None,
)


class UsbipCredentialTests(unittest.TestCase):
    def setUp(self):
        import features.devices.reconnect as reconnect

        reconnect.stop_usbip_reconnect_tasks(timeout=1)
        configure_runtime(
            selected_config_manager=EmptyConfigManager(),
            selected_global_state=global_state,
            selected_ssh_manager=None,
            selected_store_notification=None,
            selected_generate_help_or_continue=None,
            selected_get_client_id_from_request=None,
            selected_probe_windows_usbipd=None,
            selected_resolve_tailscale_device_host=None,
        )

    def tearDown(self):
        import features.devices.reconnect as reconnect

        reconnect.stop_usbip_reconnect_tasks(timeout=1)

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

    def test_usbipd_connected_android_adb_shared_busid_is_detected(self):
        output = """
Connected:
BUSID  VID:PID    DEVICE                                                        STATE
1-1    2207:0006  Android ADB Interface                                         Shared
1-13   0403:6001  USB Serial Converter                                          Not shared

Persisted:
GUID                                  DEVICE
466f3f47-c2c9-4ea1-bb28-333847ee3c00  Android ADB Interface
"""

        self.assertEqual(parse_usbipd_android_busids(output), ["1-1"])

    def test_usbipd_legacy_table_without_connected_header_is_detected(self):
        output = """
BUSID  VID:PID    DEVICE                                                        STATE
1-1    2207:0006  Android ADB Interface                                         Shared
1-13   0403:6001  USB Serial Converter                                          Not shared
"""

        self.assertEqual(parse_usbipd_android_busids(output), ["1-1"])

    def test_protocol_state_parsers_include_recovery_and_fastboot(self):
        adb_output = """
List of devices attached
ADB001	device
REC001	recovery
OFF001	offline
UNAUTH001	unauthorized
"""
        fastboot_output = "FB001\tfastboot\n"

        self.assertEqual(parse_adb_device_states(adb_output), {
            "ADB001": "device",
            "REC001": "recovery",
            "OFF001": "offline",
            "UNAUTH001": "unauthorized",
        })
        self.assertEqual(parse_fastboot_devices(fastboot_output), ["FB001"])

    def test_find_android_devices_parses_stderr_output(self):
        class FakeSshManager:
            def execute_command(self, ssh, cmd, timeout=None, get_pty=False):
                self.cmd = cmd
                self.get_pty = get_pty
                return (
                    "",
                    "Connected:\n"
                    "BUSID  VID:PID    DEVICE                                                        STATE\n"
                    "1-1    2207:0006  Android ADB Interface                                         Shared\n",
                    0,
                )

        manager = USBIPManager()
        manager.ssh_manager = FakeSshManager()

        self.assertEqual(manager._find_android_devices(object(), {}), ["1-1"])

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

    def test_attach_devices_accepts_existing_adb_visibility_after_attach_failure(self):
        class FakeSshManager:
            def execute_command(self, ssh, cmd, timeout=None, get_pty=False):
                if cmd == "adb devices":
                    return ("List of devices attached\nUSBIP001\tdevice\n", "", 0)
                if cmd.startswith("sudo usbip attach"):
                    return ("", "device already attached", 1)
                return ("", "", 0)

        manager = USBIPManager()
        manager.ssh_manager = FakeSshManager()

        attached, devices = manager._attach_devices(object(), "172.16.14.64", ["1-1"])

        self.assertEqual(attached, ["1-1"])
        self.assertEqual(devices, ["USBIP001"])

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

        with patch("features.devices.usbip.time.sleep", return_value=None):
            attached, devices = manager._attach_devices(object(), "172.16.14.66", ["1-1"])

        self.assertEqual(attached, ["1-1"])
        self.assertEqual(devices, ["USBIP001"])
        self.assertGreaterEqual(manager.ssh_manager.adb_calls, 4)

    def test_attach_devices_returns_when_fastboot_protocol_appears(self):
        class FakeSshManager:
            def __init__(self):
                self.fastboot_calls = 0

            def execute_command(self, ssh, cmd, timeout=None, get_pty=False):
                if cmd == "adb devices":
                    return ("List of devices attached\n", "", 0)
                if cmd == "fastboot devices":
                    self.fastboot_calls += 1
                    if self.fastboot_calls < 2:
                        return ("", "", 0)
                    return ("FB001\tfastboot\n", "", 0)
                if cmd.startswith("sudo usbip attach"):
                    return ("attached", "", 0)
                return ("", "", 0)

        manager = USBIPManager()
        manager.ssh_manager = FakeSshManager()

        with patch("features.devices.usbip.time.sleep", return_value=None):
            attached, devices = manager._attach_devices(object(), "172.16.14.66", ["1-1"])

        self.assertEqual(attached, ["1-1"])
        self.assertEqual(devices, [])
        self.assertEqual(manager.ssh_manager.fastboot_calls, 2)

    def test_attach_devices_returns_when_recovery_protocol_appears(self):
        class FakeSshManager:
            def __init__(self):
                self.adb_calls = 0

            def execute_command(self, ssh, cmd, timeout=None, get_pty=False):
                if cmd == "adb devices":
                    self.adb_calls += 1
                    if self.adb_calls < 2:
                        return ("List of devices attached\n", "", 0)
                    return ("List of devices attached\nREC001\trecovery\n", "", 0)
                if cmd.startswith("sudo usbip attach"):
                    return ("attached", "", 0)
                return ("", "", 0)

        manager = USBIPManager()
        manager.ssh_manager = FakeSshManager()

        with patch("features.devices.usbip.time.sleep", return_value=None):
            attached, devices = manager._attach_devices(object(), "172.16.14.66", ["1-1"])

        self.assertEqual(attached, ["1-1"])
        self.assertEqual(devices, [])
        self.assertEqual(manager.ssh_manager.adb_calls, 2)

    def test_protocol_status_is_scoped_to_attached_usbip_devices(self):
        manager = USBIPManager()
        scoped = manager._scope_protocol_status(
            {
                "adb": {"LOCAL001": "device", "USBIP001": "device"},
                "adb_ready": ["LOCAL001", "USBIP001"],
                "recovery": [],
                "sideload": [],
                "unauthorized": [],
                "offline": [],
                "fastboot": [],
                "mode": "adb",
            },
            ["USBIP001"],
        )

        self.assertEqual(scoped["adb"], {"USBIP001": "device"})
        self.assertEqual(scoped["adb_ready"], ["USBIP001"])

    def test_device_host_password_matches_full_host_before_username_fallback(self):
        config = {
            "client_ssh_credentials": [
                {"device_host": "hcq@172.16.14.66", "username": "hcq", "host": "172.16.14.66", "password": "pw66"},
                {"device_host": "hcq@172.16.14.67", "username": "hcq", "host": "172.16.14.67", "password": "pw67"},
                {"username": "legacy", "password": "legacy-pw"},
            ]
        }

        self.assertEqual(find_device_host_password("hcq@172.16.14.66", config), "pw66")
        self.assertEqual(find_device_host_password("hcq@172.16.14.67", config), "pw67")
        self.assertEqual(find_device_host_password("legacy@10.0.0.8", config), "legacy-pw")

    def test_usbip_connect_persists_submitted_password_after_success(self):
        import features.devices.integrations_api as integrations

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

        with patch.object(integrations.runtime, "config_manager", FakeConfigManager()), \
                patch.object(integrations, "usbip_manager", FakeUsbipManager()), \
                patch.object(integrations.runtime, "get_client_id_from_request", return_value="hcq@172.16.14.66"):
            response = asyncio.run(integrations.start_usbip(req=req, request=request, help=False))

        self.assertEqual(response.status_code, 200)
        body = json.loads(response.body.decode("utf-8"))
        self.assertTrue(body["success"])
        self.assertEqual(saved["start_args"], ("hcq@172.16.14.66", "secret", None))
        self.assertEqual(saved["device_host"], "hcq@172.16.14.66")
        self.assertEqual(saved["password"], "secret")

    def test_usbip_connect_accepts_transport_before_adb_device_is_ready(self):
        import features.devices.integrations_api as integrations

        class FakeConfigManager:
            def load_config(self):
                return {"device_pswd": "secret", "client_ssh_credentials": []}

        class FakeUsbipManager:
            def start_usbip(self, device_host, device_password, usbip_attach_host=None):
                return {
                    "success": True,
                    "message": "attached",
                    "devices": ["1-1"],
                    "device_list": [],
                    "transport_connected": True,
                    "protocol_status": {"mode": "fastboot", "fastboot": ["FB001"]},
                }

        request = SimpleNamespace(headers={}, client=SimpleNamespace(host="127.0.0.1"))
        req = USBIPStartRequest(device_host="hcq@172.16.14.66")

        with patch.object(integrations.runtime, "config_manager", FakeConfigManager()), \
                patch.object(integrations, "usbip_manager", FakeUsbipManager()), \
                patch.object(integrations.runtime, "get_client_id_from_request", return_value="hcq@172.16.14.66"):
            response = asyncio.run(integrations.start_usbip(req=req, request=request, help=False))

        self.assertEqual(response.status_code, 200)
        body = json.loads(response.body.decode("utf-8"))
        self.assertTrue(body["success"])
        self.assertTrue(body["transport_connected"])
        self.assertFalse(body["adb_ready"])
        self.assertEqual(body["protocol_status"]["mode"], "fastboot")

    def test_usbip_status_defaults_to_configured_device_host(self):
        import features.devices.integrations_api as integrations

        class FakeConfigManager:
            def load_config(self):
                return {"device_host": "hcq@172.16.14.66"}

        request = SimpleNamespace(headers={}, client=SimpleNamespace(host="127.0.0.1"))
        with global_state.usbip_states_lock:
            old_states = dict(global_state.usbip_states)
            global_state.usbip_states.clear()
            global_state.usbip_states["hcq@172.16.14.66"] = {"connected": True, "timestamp": 1}
        try:
            with patch.object(integrations.runtime, "config_manager", FakeConfigManager()), \
                    patch.object(integrations.runtime, "get_client_id_from_request", return_value="alice-user-id"), \
                    patch.object(integrations.runtime, "resolve_tailscale_device_host", None):
                response = asyncio.run(integrations.get_usbip_status(request=request))
        finally:
            with global_state.usbip_states_lock:
                global_state.usbip_states.clear()
                global_state.usbip_states.update(old_states)

        body = json.loads(response.body.decode("utf-8"))
        self.assertTrue(body["connected"])
        self.assertEqual(body["device_host"], "hcq@172.16.14.66")
        self.assertFalse(body["transport_connected"])

    def test_usbip_status_source_record_does_not_imply_transport_restored(self):
        import features.devices.integrations_api as integrations

        class FakeConfigManager:
            def load_config(self):
                return {"device_host": "hcq@172.16.14.66"}

        request = SimpleNamespace(headers={}, client=SimpleNamespace(host="127.0.0.1"))
        with global_state.usbip_states_lock:
            old_states = dict(global_state.usbip_states)
            global_state.usbip_states.clear()
        with global_state.usbip_devices_source_lock:
            old_sources = dict(global_state.usbip_devices_source)
            global_state.usbip_devices_source.clear()
            global_state.usbip_devices_source["USBIP001"] = {
                "source": "hcq@172.16.14.66",
                "timestamp": 1,
            }
        try:
            with patch.object(integrations.runtime, "config_manager", FakeConfigManager()), \
                    patch.object(integrations.runtime, "get_client_id_from_request", return_value="alice-user-id"), \
                    patch.object(integrations.runtime, "resolve_tailscale_device_host", None):
                response = asyncio.run(integrations.get_usbip_status(request=request))
        finally:
            with global_state.usbip_states_lock:
                global_state.usbip_states.clear()
                global_state.usbip_states.update(old_states)
            with global_state.usbip_devices_source_lock:
                global_state.usbip_devices_source.clear()
                global_state.usbip_devices_source.update(old_sources)

        body = json.loads(response.body.decode("utf-8"))
        self.assertTrue(body["connected"])
        self.assertFalse(body["transport_connected"])
        self.assertFalse(body["adb_ready"])

    def test_suppressed_usbip_auto_connect_is_blocked_until_manual_connect(self):
        import features.devices.integrations_api as integrations
        import features.devices.reconnect as reconnect

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
            with patch.object(integrations.runtime, "config_manager", FakeConfigManager()), \
                    patch.object(integrations, "usbip_manager", FakeUsbipManager()), \
                    patch.object(integrations.runtime, "get_client_id_from_request", return_value="hcq@172.16.14.66"):
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
        import features.devices.integrations_api as integrations
        import features.devices.reconnect as reconnect

        class FakeConfigManager:
            def load_config(self):
                return {"device_pswd": "secret", "client_ssh_credentials": []}

        class FakeUsbipManager:
            def start_usbip(self, device_host, device_password, usbip_attach_host=None):
                return {"success": False, "error": "未找到Android设备"}

        request = SimpleNamespace(headers={}, client=SimpleNamespace(host="127.0.0.1"))
        reconnect.suppress_usbip_reconnect("hcq@172.16.14.66", ["USBIP001"])
        try:
            with patch.object(integrations.runtime, "config_manager", FakeConfigManager()), \
                    patch.object(integrations, "usbip_manager", FakeUsbipManager()), \
                    patch.object(integrations.runtime, "get_client_id_from_request", return_value="hcq@172.16.14.66"):
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
        import features.devices.reconnect as reconnect

        with global_state.usbip_devices_source_lock:
            old_sources = dict(global_state.usbip_devices_source)
            global_state.usbip_devices_source.clear()
            global_state.usbip_devices_source["USBIP001"] = {
                "source": "hcq@172.16.14.66",
                "timestamp": 1,
            }

        scheduled = []
        try:
            with patch.object(
                reconnect,
                "schedule_usbip_reconnect",
                side_effect=lambda host, reason="", expected_devices=(): scheduled.append(
                    (host, reason, tuple(expected_devices))
                ) or True,
            ):
                hosts = reconnect.schedule_usbip_reconnect_for_removed_devices(["USBIP001"], reason="test")
        finally:
            with global_state.usbip_devices_source_lock:
                global_state.usbip_devices_source.clear()
                global_state.usbip_devices_source.update(old_sources)

        self.assertEqual(hosts, ["hcq@172.16.14.66"])
        self.assertEqual(scheduled, [("hcq@172.16.14.66", "test: USBIP001", ("USBIP001",))])

    def test_manual_usbip_disconnect_suppresses_server_side_reconnect(self):
        import features.devices.reconnect as reconnect

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
            with patch.object(
                reconnect,
                "schedule_usbip_reconnect",
                side_effect=lambda host, reason="", expected_devices=(): scheduled.append(
                    (host, reason, tuple(expected_devices))
                ) or True,
            ):
                hosts = reconnect.schedule_usbip_reconnect_for_removed_devices(["USBIP001"], reason="manual disconnect")
        finally:
            reconnect.clear_usbip_reconnect_suppression("hcq@172.16.14.66", ["USBIP001"])
            with global_state.usbip_devices_source_lock:
                global_state.usbip_devices_source.clear()
                global_state.usbip_devices_source.update(old_sources)

        self.assertEqual(hosts, [])
        self.assertEqual(scheduled, [])

    def test_usbip_disconnect_finds_devices_from_runtime_sources(self):
        import features.devices.integrations_api as integrations

        old_sources = dict(global_state.usbip_devices_source)
        old_manager_sources = dict(integrations.usbip_manager.device_sources)
        try:
            with global_state.usbip_devices_source_lock:
                global_state.usbip_devices_source.clear()
            integrations.usbip_manager.device_sources.clear()

            with patch.object(integrations.runtime.config_manager, "get_runtime_config", return_value={
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

    def test_usbip_disconnect_detaches_ubuntu_ports_in_normal_mode(self):
        import features.devices.integrations_api as integrations

        calls = []

        class FakeConfigManager:
            def load_config(self):
                return {
                    "device_host": "hcq@172.16.14.66",
                    "device_pswd": "secret",
                    "client_ssh_credentials": [],
                }

            def get_runtime_config(self):
                return {
                    "usbip_devices_source": {
                        "USBIP001": {"source": "hcq@172.16.14.66", "timestamp": 1},
                    }
                }

            def save_runtime_config(self, data):
                return True

        class FakeSshManager:
            def get_connection(self, config):
                return "ubuntu-ssh"

            def return_connection(self, ssh):
                calls.append(("return", ssh))

            def execute_command(self, ssh, cmd, timeout=None, get_pty=False):
                calls.append(("exec", ssh, cmd))
                return ("", "", 0)

        class FakeDeviceSSHConnection:
            def __init__(self, config):
                self.config = config

            def __enter__(self):
                return "windows-ssh"

            def __exit__(self, exc_type, exc, tb):
                return False

        request = SimpleNamespace(headers={}, client=SimpleNamespace(host="127.0.0.1"))

        with patch.object(integrations.runtime, "config_manager", FakeConfigManager()), \
                patch.object(integrations.runtime, "ssh_manager", FakeSshManager()), \
                patch.object(integrations.runtime, "get_client_id_from_request", return_value="hcq@172.16.14.66"), \
                patch.object(integrations.runtime, "resolve_tailscale_device_host", return_value=(None, None)), \
                patch.object(integrations, "DeviceSSHConnection", FakeDeviceSSHConnection), \
                patch.object(integrations, "notify_device_change", AsyncMock()), \
                patch("features.devices.reconnect.stop_usbip_reconnect_for_host") as stop_reconnect, \
                patch.object(integrations, "detach_ubuntu_usbip_ports", return_value=["00"] ) as detach:
            response = asyncio.run(integrations.stop_usbip(request=request, req=None))

        body = json.loads(response.body.decode("utf-8"))
        self.assertTrue(body["success"])
        detach.assert_called_once_with("ubuntu-ssh", "172.16.14.66", detach_all=False)
        stop_reconnect.assert_called_once_with("hcq@172.16.14.66", timeout=2)
        self.assertIn(("exec", "windows-ssh", "usbipd unbind --all"), calls)

    def test_usbip_disconnect_defaults_to_resolved_device_host_not_platform_user_id(self):
        import features.devices.integrations_api as integrations

        class FakeConfigManager:
            def load_config(self):
                return {
                    "client_hosts": {"172.16.14.66": "hcq"},
                    "client_ssh_credentials": [
                        {
                            "device_host": "hcq@172.16.14.66",
                            "username": "hcq",
                            "host": "172.16.14.66",
                            "password": "secret",
                        }
                    ],
                }

            def get_runtime_config(self):
                return {
                    "usbip_devices_source": {
                        "USBIP001": {"source": "hcq@172.16.14.66", "timestamp": 1},
                    }
                }

            def save_runtime_config(self, data):
                return True

        class FakeSshManager:
            def get_connection(self, config):
                return "ubuntu-ssh"

            def return_connection(self, ssh):
                pass

            def execute_command(self, ssh, cmd, timeout=None, get_pty=False):
                return ("", "", 0)

        class FakeDeviceSSHConnection:
            def __init__(self, config):
                self.config = config

            def __enter__(self):
                return "windows-ssh"

            def __exit__(self, exc_type, exc, tb):
                return False

        request = SimpleNamespace(headers={}, client=SimpleNamespace(host="172.16.14.66"))

        with patch.object(integrations.runtime, "config_manager", FakeConfigManager()), \
                patch.object(integrations.runtime, "ssh_manager", FakeSshManager()), \
                patch.object(integrations.runtime, "get_client_id_from_request", return_value="NqWo58sh1jr5c6ZiyxxPtQ"), \
                patch.object(integrations.runtime, "resolve_tailscale_device_host", return_value=(None, None)), \
                patch.object(integrations, "get_client_display_id_from_request", return_value="hcq@172.16.14.66"), \
                patch.object(integrations, "DeviceSSHConnection", FakeDeviceSSHConnection), \
                patch.object(integrations, "notify_device_change", AsyncMock()), \
                patch("features.devices.reconnect.stop_usbip_reconnect_for_host"), \
                patch.object(integrations, "detach_ubuntu_usbip_ports", return_value=["00"]) as detach:
            response = asyncio.run(integrations.stop_usbip(request=request, req=None))

        body = json.loads(response.body.decode("utf-8"))
        self.assertTrue(body["success"])
        detach.assert_called_once_with("ubuntu-ssh", "172.16.14.66", detach_all=False)

    def test_verified_detach_falls_back_when_target_device_remains(self):
        import features.devices.integrations_api as integrations

        adb_calls = 0

        class FakeSshManager:
            def execute_command(self, ssh, cmd, timeout=None, get_pty=False):
                nonlocal adb_calls
                if cmd == "adb devices":
                    adb_calls += 1
                    if adb_calls == 1:
                        return ("List of devices attached\nUSBIP001\tdevice\n", "", 0)
                    return ("List of devices attached\n", "", 0)
                return ("", "", 0)

        detach_calls = []

        def fake_detach(ssh, remote_host=None, detach_all=False):
            detach_calls.append((remote_host, detach_all))
            return ["00"] if not detach_all else ["01"]

        with patch.object(integrations.runtime, "ssh_manager", FakeSshManager()), \
                patch.object(integrations, "detach_ubuntu_usbip_ports", side_effect=fake_detach):
            result = integrations._detach_ubuntu_usbip_for_devices(
                "ubuntu-ssh",
                device_host="hcq@172.16.14.66",
                usbip_attach_host=None,
                devices_to_remove=["USBIP001"],
                detach_all=False,
            )

        self.assertEqual(detach_calls, [("172.16.14.66", False), (None, True)])
        self.assertEqual(result["detached_ports"], ["00", "01"])
        self.assertEqual(result["remaining_devices"], [])

    def test_device_list_refresh_keeps_usbip_source_for_reconnect(self):
        import features.devices.api as devices_router

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
                    patch.object(devices_router.runtime, "get_client_id_from_request", return_value="hcq@127.0.0.1"), \
                    patch.object(devices_router.runtime, "get_client_ip", return_value="127.0.0.1"), \
                    patch.object(devices_router.runtime, "client_manager", SimpleNamespace(get_client_id=lambda _ip: "hcq@127.0.0.1")):
                asyncio.run(devices_router.get_connected_devices(request=request, help=False, force_refresh=True))

            self.assertIn("USBIP001", global_state.usbip_devices_source)
        finally:
            with global_state.usbip_devices_source_lock:
                global_state.usbip_devices_source.clear()
                global_state.usbip_devices_source.update(old_sources)
            with global_state.device_cache_lock:
                global_state.device_cache = old_cache

    def test_device_list_hides_manually_disconnected_usbip_device(self):
        import features.devices.api as devices_router
        import features.devices.reconnect as reconnect

        old_cache = dict(global_state.device_cache)
        try:
            with global_state.device_cache_lock:
                global_state.device_cache = {"devices": [], "timestamp": 0}
            reconnect.suppress_usbip_reconnect("hcq@172.16.14.66", ["USBIP001"])
            request = SimpleNamespace(headers={}, client=SimpleNamespace(host="127.0.0.1"))
            with patch.object(devices_router.device_manager, "get_connected_devices", return_value=["LOCAL001", "USBIP001"]), \
                    patch.object(devices_router.runtime, "get_client_id_from_request", return_value="hcq@127.0.0.1"), \
                    patch.object(devices_router.runtime, "get_client_ip", return_value="127.0.0.1"), \
                    patch.object(devices_router.runtime, "client_manager", SimpleNamespace(get_client_id=lambda _ip: "hcq@127.0.0.1")):
                response = asyncio.run(devices_router.get_connected_devices(request=request, help=False, force_refresh=True))

            body = json.loads(response.body.decode("utf-8"))
            self.assertEqual([item["device_id"] for item in body], ["LOCAL001"])
        finally:
            reconnect.clear_usbip_reconnect_suppression("hcq@172.16.14.66", ["USBIP001"])
            with global_state.device_cache_lock:
                global_state.device_cache = old_cache

    def test_device_list_marks_persisted_usbip_source_when_memory_empty(self):
        import features.devices.api as devices_router

        old_sources = dict(global_state.usbip_devices_source)
        old_cache = dict(global_state.device_cache)
        try:
            with global_state.usbip_devices_source_lock:
                global_state.usbip_devices_source.clear()
            with global_state.device_cache_lock:
                global_state.device_cache = {"devices": [], "timestamp": 0}

            request = SimpleNamespace(headers={}, client=SimpleNamespace(host="127.0.0.1"))
            with patch.object(devices_router.device_manager, "get_connected_devices", return_value=["LOCAL001", "USBIP001"]), \
                    patch.object(devices_router.runtime.config_manager, "get_runtime_config", return_value={
                        "usbip_devices_source": {
                            "USBIP001": {"source": "hcq@172.16.14.66", "timestamp": 1}
                        }
                    }), \
                    patch.object(devices_router, "_prune_inactive_usbip_sources", side_effect=lambda _devices, sources, _config: sources), \
                    patch.object(devices_router.runtime, "get_client_id_from_request", return_value="hcq@127.0.0.1"), \
                    patch.object(devices_router.runtime, "get_client_ip", return_value="127.0.0.1"), \
                    patch.object(devices_router.runtime, "client_manager", SimpleNamespace(get_client_id=lambda _ip: "hcq@127.0.0.1")):
                response = asyncio.run(devices_router.get_connected_devices(request=request, help=False, force_refresh=True))

            body = json.loads(response.body.decode("utf-8"))
            devices = {item["device_id"]: item for item in body}
            self.assertNotIn("is_usbip", devices["LOCAL001"])
            self.assertTrue(devices["USBIP001"]["is_usbip"])
            self.assertEqual(devices["USBIP001"]["source"], "hcq@172.16.14.66")
        finally:
            with global_state.usbip_devices_source_lock:
                global_state.usbip_devices_source.clear()
                global_state.usbip_devices_source.update(old_sources)
            with global_state.device_cache_lock:
                global_state.device_cache = old_cache

    def test_usbip_reboot_returns_without_waiting_for_adb_online(self):
        import features.devices.operations_api as operations

        old_sources = dict(global_state.usbip_devices_source)
        calls = []
        try:
            with global_state.usbip_devices_source_lock:
                global_state.usbip_devices_source.clear()

            def fake_reboot_device(device_id, ssh=None, wait_for_online=True):
                calls.append((device_id, wait_for_online))
                return {"success": True, "back_online": False, "wait_time": 0.0}

            with patch.object(operations.runtime, "config_manager", SimpleNamespace(get_runtime_config=lambda: {
                "usbip_devices_source": {
                    "USBIP001": {"source": "hcq@172.16.14.66", "timestamp": 1}
                }
            })), patch.object(operations.device_manager, "reboot_device", side_effect=fake_reboot_device), \
                    patch.object(operations.reconnect, "schedule_usbip_reconnect", return_value=True):
                response = asyncio.run(operations.reboot_devices(DeviceActionRequest(devices=["USBIP001"])))

            body = json.loads(response.body.decode("utf-8"))
            self.assertTrue(body["success"])
            self.assertEqual(calls, [("USBIP001", False)])
            self.assertTrue(body["data"]["results"][0]["usbip_reconnect_expected"])
        finally:
            with global_state.usbip_devices_source_lock:
                global_state.usbip_devices_source.clear()
                global_state.usbip_devices_source.update(old_sources)

    def test_usbip_reboot_schedules_backend_reconnect_for_device_host(self):
        import features.devices.operations_api as operations

        old_sources = dict(global_state.usbip_devices_source)
        scheduled = []
        calls = []
        try:
            with global_state.usbip_devices_source_lock:
                global_state.usbip_devices_source.clear()
                global_state.usbip_devices_source["USBIP001"] = {
                    "source": "hcq@172.16.14.66",
                    "timestamp": 1,
                }

            def fake_reboot_device(device_id, ssh=None, wait_for_online=True):
                calls.append((device_id, wait_for_online))
                return {"success": True}

            with patch.object(operations.device_manager, "reboot_device", side_effect=fake_reboot_device), \
                    patch.object(operations.reconnect, "schedule_usbip_reconnect", side_effect=lambda host, reason="", expected_devices=(): scheduled.append((host, reason, tuple(expected_devices))) or True):
                response = asyncio.run(operations.reboot_devices(DeviceActionRequest(devices=["USBIP001"])))

            body = json.loads(response.body.decode("utf-8"))
            self.assertTrue(body["success"])
            self.assertEqual(calls, [("USBIP001", False)])
            self.assertEqual(scheduled, [("hcq@172.16.14.66", "USB/IP device reboot requested", ("USBIP001",))])
        finally:
            with global_state.usbip_devices_source_lock:
                global_state.usbip_devices_source.clear()
                global_state.usbip_devices_source.update(old_sources)

    def test_reconnect_waits_for_stable_expected_device(self):
        import features.devices.reconnect as reconnect

        class FakeConfigManager:
            def load_config(self, force_reload=False):
                return {"device_pswd": "secret", "client_ssh_credentials": []}

            def get_runtime_config(self):
                return {}

            def save_runtime_config(self, data):
                return True

        class FakeUsbipManager:
            def __init__(self):
                self.calls = 0

            def start_usbip(self, device_host, device_password):
                self.calls += 1
                return {"success": True, "device_list": ["USBIP001"]}

        fake_usbip = FakeUsbipManager()
        device_sequences = iter([
            [],
            ["USBIP001"],
            ["USBIP001"],
            ["USBIP001"],
        ])

        with patch.object(reconnect.runtime, "config_manager", FakeConfigManager()), \
                patch.object(reconnect, "usbip_manager", fake_usbip), \
                patch.object(reconnect.device_manager, "get_connected_devices", side_effect=lambda force_refresh=True: next(device_sequences)), \
                patch.object(reconnect, "USBIP_RECONNECT_INTERVAL_SECONDS", 0), \
                patch.object(reconnect, "USBIP_RECONNECT_STABLE_INTERVAL_SECONDS", 0):
            reconnect._reconnect_worker(
                "hcq@172.16.14.66",
                "test",
                threading.Event(),
                ("USBIP001",),
            )

        self.assertEqual(fake_usbip.calls, 2)
        self.assertIn("USBIP001", global_state.usbip_devices_source)

    def test_reconnect_result_is_ignored_after_manual_disconnect_suppression(self):
        import features.devices.reconnect as reconnect

        class FakeConfigManager:
            def load_config(self, force_reload=False):
                return {"device_pswd": "secret", "client_ssh_credentials": []}

            def get_runtime_config(self):
                return {}

        class FakeUsbipManager:
            def start_usbip(self, device_host, device_password):
                reconnect.suppress_usbip_reconnect(device_host, ["USBIP001"])
                return {
                    "success": True,
                    "transport_connected": True,
                    "device_list": ["USBIP001"],
                }

        old_sources = dict(global_state.usbip_devices_source)
        try:
            with global_state.usbip_devices_source_lock:
                global_state.usbip_devices_source.clear()
            with patch.object(reconnect.runtime, "config_manager", FakeConfigManager()), \
                    patch.object(reconnect, "usbip_manager", FakeUsbipManager()), \
                    patch.object(reconnect.device_manager, "get_connected_devices", return_value=["USBIP001"]):
                reconnect._reconnect_worker(
                    "hcq@172.16.14.66",
                    "test",
                    threading.Event(),
                    ("USBIP001",),
                )
            self.assertNotIn("USBIP001", global_state.usbip_devices_source)
        finally:
            reconnect.clear_usbip_reconnect_suppression("hcq@172.16.14.66", ["USBIP001"])
            with global_state.usbip_devices_source_lock:
                global_state.usbip_devices_source.clear()
                global_state.usbip_devices_source.update(old_sources)

    def test_reconcile_observed_usbip_device_restores_source_mapping(self):
        import features.devices.reconnect as reconnect

        old_sources = dict(global_state.usbip_devices_source)
        old_states = dict(global_state.usbip_states)
        class FakeConfigManager:
            def get_runtime_config(self):
                return {}

            def save_runtime_config(self, data):
                return True

        try:
            with global_state.usbip_devices_source_lock:
                global_state.usbip_devices_source.clear()
            with global_state.usbip_states_lock:
                global_state.usbip_states.clear()
                global_state.usbip_states["hcq@172.16.14.66"] = {
                    "connected": True,
                    "transport_connected": False,
                    "adb_ready": False,
                    "reconnecting": True,
                    "expected_devices": ["USBIP001"],
                    "protocol_status": {"mode": "reconnecting"},
                }
            with patch.object(reconnect.runtime, "config_manager", FakeConfigManager()):
                restored = reconnect.reconcile_observed_usbip_devices(["LOCAL001", "USBIP001"])

            self.assertEqual(restored, {"hcq@172.16.14.66": ["USBIP001"]})
            self.assertEqual(
                global_state.usbip_devices_source["USBIP001"]["source"],
                "hcq@172.16.14.66",
            )
            self.assertFalse(global_state.usbip_states["hcq@172.16.14.66"]["reconnecting"])
            self.assertTrue(global_state.usbip_states["hcq@172.16.14.66"]["adb_ready"])
        finally:
            with global_state.usbip_devices_source_lock:
                global_state.usbip_devices_source.clear()
                global_state.usbip_devices_source.update(old_sources)
            with global_state.usbip_states_lock:
                global_state.usbip_states.clear()
                global_state.usbip_states.update(old_states)

    def test_frontend_waits_for_backend_autoreconnects_usbip_disconnects(self):
        text = Path("web/static/js/navigation.js").read_text(encoding="utf-8", errors="ignore")

        self.assertIn("device_host: deviceHost", text)
        self.assertIn("scheduleUsbipReconnect", text)
        self.assertIn("USBIP_RECONNECT_MAX_ATTEMPTS", text)
        self.assertIn("USBIP_RECONNECT_INITIAL_DELAY_MS", text)
        self.assertIn("USB/IP 设备正在重启", text)
        self.assertIn("usbipManualDisconnectUntil", text)
        self.assertIn("data.source !== 'usbip_disconnect'", text)
        self.assertIn("manual_connect: true", text)
        self.assertIn("isUsbipAdbReady", text)
        self.assertIn("isUsbipProtocolVisible", text)
        self.assertIn("usbipReconnectWaiting || usbipReconnectTimer", text)
        self.assertNotIn("status.transport_connected || usbipDevices.length > 0", text)
        self.assertIn("等待后端自动重连", text)
        self.assertIn("/api/usbip/status?device_host=", text)
        self.assertNotIn("const result = await apiCall('/api/usbip/connect', 'POST', payload);", text)
        self.assertNotIn("result.success || result.devices", text)
        self.assertNotIn("Button reset due to device disconnect", text)
