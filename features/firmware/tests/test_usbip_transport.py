import asyncio
import unittest
from unittest.mock import patch

from features.firmware import runtime, usbip_transport


class FakeSshManager:
    def __init__(self, responses=()):
        self.responses = iter(responses)
        self.commands = []

    def execute_command(self, _ssh, cmd, timeout=None):
        self.commands.append((cmd, timeout))
        return next(self.responses)


class UsbipFirmwareTransportTests(unittest.TestCase):
    def test_mode_switch_schedules_source_reconnect(self):
        with patch.object(
            usbip_transport.usbip_reconnect,
            "usbip_source_host_for_device",
            return_value="hcq@172.16.14.66",
        ), patch.object(
            usbip_transport.usbip_reconnect,
            "schedule_usbip_reconnect",
            return_value=True,
        ) as schedule:
            scheduled = usbip_transport.schedule_usbip_mode_reconnect(
                "rk3572test", "fastboot"
            )
        self.assertTrue(scheduled)
        schedule.assert_called_once_with(
            "hcq@172.16.14.66",
            reason="USB/IP rk3572test switching to fastboot",
            expected_devices=["rk3572test"],
            accept_transport_only=True,
        )

    def test_waits_for_loader_and_adb_reenumeration(self):
        manager = FakeSshManager([
            ("List of rockusb connected(0)\n", "", 0),
            ("List of rockusb connected(1)\n", "", 0),
        ])
        runtime.configure_runtime(ssh_manager=manager)
        ready, output = asyncio.run(usbip_transport.wait_for_rockusb_loaders(
            object(), "upgrade_tool ld", 1, timeout=1, interval=0.01
        ))
        self.assertTrue(ready)
        self.assertIn("connected(1)", output)

        manager = FakeSshManager([
            ("List of devices attached\n", "", 0),
            ("List of devices attached\nrk3572test\tdevice\n", "", 0),
        ])
        runtime.configure_runtime(ssh_manager=manager)
        ready, observed = asyncio.run(usbip_transport.wait_for_adb_devices(
            object(), ["rk3572test"], timeout=1, interval=0.01
        ))
        self.assertTrue(ready)
        self.assertEqual(observed, ["rk3572test"])

    def test_preflight_installs_autobind_for_assigned_port(self):
        routes = [{
            "device_host": "hcq@172.16.14.66",
            "source_host": "172.16.14.66",
            "busids": ["1-1"],
            "device_ids": ["rk3572test"],
        }]
        with patch.object(
            usbip_transport.usbip_reconnect,
            "usbip_source_host_for_device",
            return_value="hcq@172.16.14.66",
        ), patch.object(
            usbip_transport, "resolve_usbip_flash_routes", return_value=routes
        ), patch.object(
            usbip_transport,
            "ensure_usbip_auto_bind_policies",
            return_value={"success": True},
        ) as ensure:
            resolved, error = asyncio.run(
                usbip_transport.prepare_usbip_firmware_routes(["rk3572test"])
            )
        self.assertEqual((resolved, error), (routes, ""))
        ensure.assert_called_once_with("hcq@172.16.14.66", ["1-1"])

    def test_maskrom_watch_retries_until_new_instance_is_ready(self):
        manager = FakeSshManager([
            ("", "device not found", 1),
            ("", "", 0),
        ])
        runtime.configure_runtime(ssh_manager=manager)
        result = asyncio.run(usbip_transport.reattach_usbip_after_rockusb_reset(
            object(), [{"source_host": "172.16.14.66", "busids": ["1-1"]}],
            timeout=1, interval=0.01,
        ))
        self.assertTrue(result["success"])
        # 单次 attach 超时必须覆盖 Windows 端重新枚举后的慢速挂载，
        # 不能用 1s 截断即将成功的请求。
        self.assertEqual(manager.commands[0], (
            "sudo usbip attach -r 172.16.14.66 -b 1-1", 4
        ))
        self.assertGreaterEqual(result["attempts"], 2)

    def test_maskrom_watch_reports_pending_and_last_error(self):
        manager = FakeSshManager([
            ("", "usbip: error: Exported Device not found", 1),
        ] * 10)
        runtime.configure_runtime(ssh_manager=manager)
        result = asyncio.run(usbip_transport.reattach_usbip_after_rockusb_reset(
            object(), [{"source_host": "172.16.14.66", "busids": ["1-1"]}],
            timeout=0.5, interval=0.01,
        ))
        self.assertFalse(result["success"])
        self.assertEqual(
            result["pending"],
            [{"source_host": "172.16.14.66", "busid": "1-1"}],
        )
        self.assertEqual(
            result["errors"]["172.16.14.66/1-1"],
            "usbip: error: Exported Device not found",
        )
        self.assertGreaterEqual(result["attempts"], 1)

    def test_protocols_accept_adb_fastbootd_and_vendor_fastboot_label(self):
        manager = FakeSshManager([
            ("List of devices attached\nADB001\tdevice\n", "", 0),
            ("FB001\tfastbootd\nrk3572test\t Android Fastboot\n", "", 0),
        ])
        runtime.configure_runtime(ssh_manager=manager)
        protocols = usbip_transport.device_flash_protocols(
            object(), ["ADB001", "FB001", "rk3572test", "MISSING"]
        )
        self.assertEqual(protocols, {
            "ADB001": "adb", "FB001": "fastboot",
            "rk3572test": "fastboot", "MISSING": "",
        })

    def test_only_routed_loader_is_accepted_for_direct_retry(self):
        route = [{"device_ids": ["rk3572test"], "busids": ["1-1"]}]
        self.assertEqual(
            usbip_transport.accept_direct_rockusb_loaders(
                {"rk3572test": ""}, route,
                "List of rockusb connected(1)\n",
            ),
            {"rk3572test": "rockusb-loader"},
        )
        self.assertEqual(
            usbip_transport.accept_direct_rockusb_loaders(
                {"MISSING": ""}, route, "List of rockusb connected(1)\n"
            ),
            {"MISSING": ""},
        )


if __name__ == "__main__":
    unittest.main()
