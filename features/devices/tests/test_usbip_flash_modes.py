import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from features.devices import reconnect, usbip_flash, usbip_transport_probe
from features.devices.usb import configured_usbip_vid_pids
from features.devices.usbip import (
    USBIPManager,
    parse_adb_device_states,
    parse_fastboot_devices,
    parse_usbipd_android_busids,
)


class UsbipFlashModeTests(unittest.TestCase):
    def test_fastboot_download_gadget_is_detected_after_reenumeration(self):
        output = """
Connected:
BUSID  VID:PID    DEVICE                 STATE
1-1    18d1:4d00  USB download gadget    Not shared
1-13   0403:6001  USB Serial Converter   Not shared
Persisted:
GUID                                  DEVICE
abc                                   Android ADB Interface
"""
        self.assertEqual(parse_usbipd_android_busids(output), ["1-1"])

    def test_plural_and_legacy_vid_pid_configuration(self):
        self.assertEqual(
            configured_usbip_vid_pids({
                "usbip_vid_pids": ["2207:0006", "18D1:4D00", "invalid"],
                "usbip_vid_pid": "05ac:12a8",
            }),
            ("2207:0006", "18d1:4d00"),
        )
        self.assertEqual(
            configured_usbip_vid_pids({"usbip_vid_pid": "05ac:12a8"}),
            ("2207:0006", "18d1:4d00", "2207:351a", "05ac:12a8"),
        )

    def test_protocol_parsers_include_recovery_and_vendor_fastboot_label(self):
        adb_output = (
            "List of devices attached\nADB001\tdevice\nREC001\trecovery\n"
            "OFF001\toffline\nUNAUTH001\tunauthorized\n"
        )
        self.assertEqual(parse_adb_device_states(adb_output), {
            "ADB001": "device",
            "REC001": "recovery",
            "OFF001": "offline",
            "UNAUTH001": "unauthorized",
        })
        self.assertEqual(
            parse_fastboot_devices(
                "FB001\tfastboot\nFB002\tfastbootd\n"
                "rk3572test\t Android Fastboot\n"
            ),
            ["FB001", "FB002", "rk3572test"],
        )

    def test_autobind_policy_match_requires_exact_busid_token(self):
        from features.devices.usbip_flash import usbipd_policy_line_covers_busid

        policy_output = "p1  Allow AutoBind 1-11\np2  Allow AutoBind 1-1.2\n"
        # "1-1" 不能误命中 "1-11" / "1-1.2" 的既有规则。
        self.assertFalse(usbipd_policy_line_covers_busid(policy_output, "1-1"))
        self.assertTrue(usbipd_policy_line_covers_busid(policy_output, "1-11"))
        self.assertTrue(usbipd_policy_line_covers_busid(policy_output, "1-1.2"))
        self.assertTrue(usbipd_policy_line_covers_busid(
            "Allow AutoBind 1-1", "1-1",
        ))
        self.assertFalse(usbipd_policy_line_covers_busid(
            "Deny AutoBind 1-1", "1-1",
        ))

    def test_firmware_route_uses_only_local_worker_assignment(self):
        runtime_config = {
            "usbip_cluster_assignments": {
                "local|1-1": {
                    "device_host": "hcq@172.16.14.66",
                    "source_host": "172.16.14.66",
                    "worker_id": "ats-worker-controller",
                    "busid": "1-1",
                    "device_serials": ["rk3572test"],
                    "status": "attached",
                },
                "remote|2-1": {
                    "device_host": "hjf@172.16.14.188",
                    "source_host": "172.16.14.188",
                    "worker_id": "ats-worker-remote",
                    "busid": "2-1",
                    "device_serials": ["REMOTE001"],
                    "status": "attached",
                },
            }
        }
        config = SimpleNamespace(get_runtime_config=lambda: runtime_config)
        with patch.object(
            usbip_flash, "_local_worker_id", return_value="ats-worker-controller"
        ), patch.object(
            usbip_flash.runtime, "config_manager", config,
        ), patch.object(
            usbip_flash, "_known_sources", return_value={
                "rk3572test": {"source": "hcq@172.16.14.66"},
                "REMOTE001": {"source": "hjf@172.16.14.188"},
            },
        ):
            routes = usbip_flash.resolve_usbip_flash_routes(
                ["rk3572test", "REMOTE001"]
            )
        self.assertEqual(routes, [{
            "device_host": "hcq@172.16.14.66",
            "source_host": "172.16.14.66",
            "busids": ["1-1"],
            "device_ids": ["rk3572test"],
        }])

    def test_loader_transport_is_accepted_without_second_detach(self):
        class Config:
            def load_config(self, force_reload=False):
                return {"device_pswd": "secret", "client_ssh_credentials": []}

            def get_runtime_config(self):
                return {}

            def save_runtime_config(self, data):
                return True

        class Usbip:
            calls = 0

            def start_usbip(self, device_host, device_password, **kwargs):
                self.calls += 1
                return {
                    "success": True,
                    "transport_connected": True,
                    "device_list": [],
                    "protocol_status": {"mode": "unknown"},
                }

        state = SimpleNamespace(
            usbip_states={},
            usbip_states_lock=threading.RLock(),
            usbip_devices_source={},
            usbip_devices_source_lock=threading.RLock(),
            device_cache={"devices": [], "timestamp": 0},
            device_cache_lock=threading.RLock(),
        )
        manager = Usbip()
        reconnect._transport_only_hosts.add("hcq@172.16.14.66")
        try:
            with patch.object(reconnect.runtime, "config_manager", Config()), \
                    patch.object(reconnect.runtime, "global_state", state), \
                    patch.object(reconnect, "usbip_manager", manager), \
                    patch.object(
                        reconnect, "_resolved_busids_for_devices",
                        return_value=["1-1"],
                    ), \
                    patch.object(reconnect, "has_blocked_adb_process", return_value=False):
                reconnect._reconnect_worker(
                    "hcq@172.16.14.66", "loader", threading.Event(),
                    ("USBIP001",),
                )
        finally:
            reconnect._transport_only_hosts.discard("hcq@172.16.14.66")
        self.assertEqual(manager.calls, 1)
        self.assertFalse(state.usbip_states["hcq@172.16.14.66"]["reconnecting"])

    def test_reconnect_retries_after_transient_blocked_adb(self):
        class Config:
            def load_config(self, force_reload=False):
                return {"device_pswd": "secret", "client_ssh_credentials": []}

            def get_runtime_config(self):
                return {}

            def save_runtime_config(self, data):
                return True

        class Usbip:
            calls = 0

            def start_usbip(self, device_host, device_password, **kwargs):
                self.calls += 1
                return {
                    "success": True,
                    "transport_connected": True,
                    "device_list": [],
                    "protocol_status": {"mode": "fastboot"},
                }

        manager = Usbip()
        state = SimpleNamespace(
            usbip_states={},
            usbip_states_lock=threading.RLock(),
            usbip_devices_source={},
            usbip_devices_source_lock=threading.RLock(),
            device_cache={"devices": [], "timestamp": 0},
            device_cache_lock=threading.RLock(),
        )
        with patch.object(reconnect.runtime, "config_manager", Config()), \
                patch.object(reconnect.runtime, "global_state", state), \
                patch.object(reconnect, "usbip_manager", manager), \
                patch.object(
                    reconnect, "_resolved_busids_for_devices",
                    return_value=["1-1"],
                ), \
                patch.object(reconnect, "has_blocked_adb_process", side_effect=[True, False]), \
                patch.object(reconnect, "USBIP_RECONNECT_INTERVAL_SECONDS", 0):
            reconnect._reconnect_worker(
                "hcq@172.16.14.66", "fastboot", threading.Event(),
                ("USBIP001",),
            )
        self.assertEqual(manager.calls, 1)

    def test_reconnect_refuses_unscoped_whole_host_fallback(self):
        class Config:
            def load_config(self, force_reload=False):
                return {"device_pswd": "secret", "client_ssh_credentials": []}

            def get_runtime_config(self):
                return {}

        class Usbip:
            calls = 0

            def start_usbip(self, device_host, device_password, **kwargs):
                self.calls += 1
                raise AssertionError("whole-host reconnect must be refused")

        state = SimpleNamespace(
            usbip_states={},
            usbip_states_lock=threading.RLock(),
            usbip_devices_source={},
            usbip_devices_source_lock=threading.RLock(),
            device_cache={"devices": [], "timestamp": 0},
            device_cache_lock=threading.RLock(),
        )
        manager = Usbip()
        with patch.object(reconnect.runtime, "config_manager", Config()), \
                patch.object(reconnect.runtime, "global_state", state), \
                patch.object(reconnect, "usbip_manager", manager), \
                patch.object(reconnect, "has_blocked_adb_process", return_value=False), \
                patch.object(
                    reconnect, "probe_existing_local_usbip_transport",
                    return_value=None,
                ), patch.object(
                    reconnect, "_resolved_busids_for_devices", return_value=[],
                ):
            reconnect._reconnect_worker(
                "hcq@172.16.14.66", "loader", threading.Event(),
                ("USBIP001",),
            )

        self.assertEqual(manager.calls, 0)
        self.assertIn(
            "missing persistent BUSID assignment",
            state.usbip_states["hcq@172.16.14.66"]["reason"],
        )

    def test_reconnect_preserves_existing_transport_when_adb_is_offline(self):
        class Config:
            def load_config(self, force_reload=False):
                return {"device_pswd": "secret"}

            def get_runtime_config(self):
                return {}

            def save_runtime_config(self, data):
                return True

        class Usbip:
            calls = 0

            def start_usbip(self, device_host, device_password, **kwargs):
                self.calls += 1
                raise AssertionError("healthy USB/IP transport must not be detached")

        state = SimpleNamespace(
            usbip_states={},
            usbip_states_lock=threading.RLock(),
            usbip_devices_source={},
            usbip_devices_source_lock=threading.RLock(),
            device_cache={"devices": [], "timestamp": 0},
            device_cache_lock=threading.RLock(),
        )
        manager = Usbip()
        existing = {
            "success": True,
            "transport_connected": True,
            "device_list": [],
            "protocol_status": {
                "mode": "offline",
                "adb": {"rk3572test": "offline"},
                "offline": ["rk3572test"],
            },
        }
        with patch.object(reconnect.runtime, "config_manager", Config()), \
                patch.object(reconnect.runtime, "global_state", state), \
                patch.object(reconnect, "usbip_manager", manager), \
                patch.object(reconnect, "has_blocked_adb_process", return_value=False), \
                patch.object(
                    reconnect,
                    "probe_existing_local_usbip_transport",
                    return_value=existing,
                ):
            reconnect._reconnect_worker(
                "hcq@172.16.14.66", "ADB offline", threading.Event(),
                ("rk3572test",),
            )

        self.assertEqual(manager.calls, 0)
        self.assertTrue(
            state.usbip_states["hcq@172.16.14.66"]["transport_connected"]
        )
        self.assertEqual(
            state.usbip_states["hcq@172.16.14.66"]["protocol_status"]["mode"],
            "offline",
        )

    def test_existing_transport_probe_matches_exact_route_and_scopes_protocol(self):
        runtime_config = {
            "usbip_cluster_assignments": {
                "hcq@172.16.14.66|1-1": {
                    "device_host": "hcq@172.16.14.66",
                    "source_host": "172.16.14.66",
                    "worker_id": "ats-worker-controller",
                    "busid": "1-1",
                    "device_serials": ["rk3572test"],
                    "status": "unknown",
                },
            },
        }

        class Config:
            def get_runtime_config(self):
                return runtime_config

        class Ssh:
            returned = False

            def get_connection(self, config):
                return "ubuntu-ssh"

            def execute_command(self, ssh, command, timeout=None):
                self.assert_command = command
                return (
                    "Port 00: <Port in Use>\n"
                    "  1-1 -> usbip://172.16.14.66:3240/1-1\n",
                    "",
                    0,
                )

            def return_connection(self, ssh):
                self.returned = True

        ssh_manager = Ssh()
        manager = SimpleNamespace(
            probe_protocol_status=lambda ssh: {
                "adb": {"RK3562GMS7": "device", "rk3572test": "offline"},
                "adb_ready": ["RK3562GMS7"],
                "recovery": [],
                "sideload": [],
                "unauthorized": [],
                "offline": ["rk3572test"],
                "fastboot": [],
                "mode": "adb",
            },
        )
        with patch.object(reconnect.runtime, "config_manager", Config()), \
                patch.object(reconnect.runtime, "ssh_manager", ssh_manager), \
                patch(
                    "features.devices.usbip_transport_probe.usbip_manager",
                    manager,
                ), \
                patch.object(reconnect, "_local_worker_id", return_value="ats-worker-controller"):
            result = usbip_transport_probe.probe_existing_local_usbip_transport(
                "hcq@172.16.14.66",
                {"rk3572test"},
                {},
                local_worker_id="ats-worker-controller",
            )

        self.assertIsNotNone(result)
        self.assertTrue(result["transport_connected"])
        self.assertEqual(result["protocol_status"]["mode"], "offline")
        self.assertEqual(result["protocol_status"]["adb_ready"], [])
        self.assertTrue(ssh_manager.returned)


class UsbipAttachVerificationTests(unittest.TestCase):
    def test_attach_devices_rejects_transport_that_drops_after_attach(self):
        class FakeSshManager:
            def __init__(self):
                self.port_calls = 0
                self.attach_calls = 0

            def execute_command(self, ssh, cmd, timeout=None, get_pty=False):
                if cmd == "adb devices":
                    return ("List of devices attached\n", "", 0)
                if cmd == "fastboot devices":
                    return ("", "", 0)
                if cmd.startswith("sudo usbip attach"):
                    self.attach_calls += 1
                    return ("attached", "", 0)
                if cmd == "sudo -n /usr/bin/usbip port":
                    self.port_calls += 1
                    if self.port_calls == 1:
                        return (
                            "Port 00: <Port in Use>\n"
                            "  1-1 -> usbip://172.16.14.66:3240/1-1\n",
                            "",
                            0,
                        )
                    return ("Imported USB devices\n", "", 0)
                return ("", "", 0)

        manager = USBIPManager()
        manager.ssh_manager = FakeSshManager()
        with patch("features.devices.usbip.time.sleep", return_value=None):
            attached, devices = manager._attach_devices(
                object(), "172.16.14.66", ["1-1"]
            )

        self.assertEqual(attached, [])
        self.assertEqual(devices, [])
        self.assertEqual(manager.ssh_manager.attach_calls, 3)
        self.assertEqual(manager.ssh_manager.port_calls, 16)

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

    def test_protocol_status_without_attribution_is_unknown(self):
        # transport-only/Loader/枚举失败时 device_list 为空：其他本地设备
        # （如直连的 RK3562GMS7）的 ADB 在线状态不能算作 USB/IP 设备状态，
        # 否则重连 worker 会把 "adb" 误判为传输已恢复。
        manager = USBIPManager()
        scoped = manager._scope_protocol_status(
            {
                "adb": {"RK3562GMS7": "device"},
                "adb_ready": ["RK3562GMS7"],
                "recovery": [],
                "sideload": [],
                "unauthorized": [],
                "offline": [],
                "fastboot": [],
                "mode": "adb",
            },
            [],
        )
        self.assertEqual(scoped["mode"], "unknown")
        self.assertEqual(scoped["adb_ready"], [])
        self.assertEqual(scoped["adb"], {})
        # 原始全局探测保留在 unscoped，供诊断展示。
        self.assertEqual(scoped["unscoped"]["adb_ready"], ["RK3562GMS7"])


class UsbipAssignmentPruningTests(unittest.TestCase):
    def test_stale_unknown_busid_is_pruned_after_windows_reenumeration(self):
        import features.devices.integrations_api as integrations

        runtime_config = {
            "usbip_cluster_assignments": {
                "hcq@172.16.14.66|1-20": {
                    "device_host": "hcq@172.16.14.66",
                    "worker_id": "ats-worker-controller",
                    "busid": "1-20",
                    "status": "unknown",
                },
                "hcq@172.16.14.66|1-9": {
                    "device_host": "hcq@172.16.14.66",
                    "worker_id": "ats-worker-controller",
                    "busid": "1-9",
                    "status": "attached",
                },
            },
        }

        class FakeConfigManager:
            def get_runtime_config(self):
                return dict(runtime_config)

            def update_runtime_config(self, updates):
                runtime_config.update(updates)
                return True

        with patch.object(
            integrations.runtime, "config_manager", FakeConfigManager()
        ):
            removed = integrations._prune_stale_unknown_usbip_assignments(
                "hcq@172.16.14.66",
                {"1-1", "1-9"},
            )

        self.assertEqual(removed, ["hcq@172.16.14.66|1-20"])
        self.assertEqual(
            set(runtime_config["usbip_cluster_assignments"]),
            {"hcq@172.16.14.66|1-9"},
        )

        with patch.object(
            integrations.runtime, "config_manager", FakeConfigManager()
        ):
            integrations._prune_stale_unknown_usbip_assignments(
                "hcq@172.16.14.66",
                set(),
            )
        self.assertEqual(
            set(runtime_config["usbip_cluster_assignments"]),
            {"hcq@172.16.14.66|1-9"},
        )


if __name__ == "__main__":
    unittest.main()
