"""start_usbip 事务回滚测试。

attach 在 Ubuntu 侧失败时，本次事务在 Windows 上新 bind 的设备必须被
unbind 回滚（P1-4 幽灵 bind），且不能把之前已 Shared 的设备一并回滚。
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from features.devices import usbip


def _manager(config: dict, ssh_manager) -> usbip.USBIPManager:
    manager = usbip.USBIPManager.__new__(usbip.USBIPManager)
    config_manager = MagicMock()
    config_manager.load_config.return_value = config
    config_manager.find_device_host_password.return_value = None
    manager.config_manager = config_manager
    manager.ssh_manager = ssh_manager
    manager.device_sources = {}
    return manager


CONFIG = {
    "device_host": "hcq@172.16.14.66",
    "device_pswd": "",
    "ubuntu_host": "127.0.0.1",
    "usbip_vid_pid": "05ac:12a8",
}


class BindStateParsingTests(unittest.TestCase):
    """usbipd STATE 列必须按词判定：'Not Shared' 不得误判为已 Shared。"""

    def test_not_shared_device_is_bound_and_tracked(self):
        ssh_manager = MagicMock()
        manager = usbip.USBIPManager.__new__(usbip.USBIPManager)
        manager.ssh_manager = ssh_manager
        ssh_manager.execute_command.side_effect = (
            lambda target, cmd, timeout=None, get_pty=False: (
                ("1-2 Not Shared", "", 0)
                if cmd == "usbipd list | findstr 1-2"
                else ("", "", 0)
            )
        )
        track: list[str] = []
        bound = manager._bind_devices(MagicMock(), ["1-2"], track_newly_bound=track)
        self.assertEqual(bound, ["1-2"])
        self.assertEqual(track, ["1-2"])

    def test_already_shared_device_is_not_tracked_for_rollback(self):
        ssh_manager = MagicMock()
        manager = usbip.USBIPManager.__new__(usbip.USBIPManager)
        manager.ssh_manager = ssh_manager
        ssh_manager.execute_command.side_effect = (
            lambda target, cmd, timeout=None, get_pty=False: (
                ("1-2 Shared", "", 0)
                if cmd == "usbipd list | findstr 1-2"
                else ("", "", 0)
            )
        )
        track: list[str] = []
        bound = manager._bind_devices(MagicMock(), ["1-2"], track_newly_bound=track)
        self.assertEqual(bound, ["1-2"])
        self.assertEqual(track, [])


class StartUsbipRollbackTests(unittest.TestCase):
    def setUp(self):
        self.ssh_manager = MagicMock()

    def test_attach_failure_rolls_back_newly_bound_windows_binds(self):
        manager = _manager(CONFIG, self.ssh_manager)

        win_ssh = MagicMock()
        exec_calls: list[str] = []
        ubuntu_port_responses = iter([
            ("List of attached gadgets\n", "", 0),
            (
                "Port 00: <Port in Use>\n"
                "    1-2 | 05ac:12a8 | Device | "
                "Remote USB/IP host 172.16.14.66\n",
                "",
                0,
            ),
            ("List of attached gadgets\n", "", 0),
            ("List of attached gadgets\n", "", 0),
        ])

        def execute(target, cmd, timeout=None, get_pty=False):
            exec_calls.append(cmd)
            if target is win_ssh:
                responses = {
                    "powershell -Command \"$env:OS\"": ("Windows_NT", "", 0),
                    "usbipd --version": ("4.0.0", "", 0),
                    'tasklist /FI "IMAGENAME eq adb.exe" /NH': ("", "", 0),
                    "usbipd list | findstr 1-2": ("1-2 Not Shared", "", 0),
                    "usbipd bind --busid 1-2": ("", "", 0),
                    # Rollback commands on a fresh Windows connection:
                    "usbipd detach --busid 1-2": ("", "", 0),
                    "usbipd unbind --busid 1-2": ("", "", 0),
                }
                return responses.get(cmd, ("", "", 0))
            # Ubuntu side (including vhci probe and usbip port listing).
            if cmd == "usbip port":
                return next(ubuntu_port_responses)
            if cmd.startswith("sudo usbip attach"):
                return ("", "attach failed", 1)
            return ("", "", 0)

        ubuntu_ssh = MagicMock()
        self.ssh_manager.execute_command.side_effect = execute
        self.ssh_manager.get_connection.return_value = ubuntu_ssh
        # detach_ubuntu_usbip_ports 走模块级 usbip_manager.ssh_manager，
        # 与测试实例共享同一 stub 才能覆盖模块级单例的调用。
        with patch.object(
            usbip.usbip_manager, "ssh_manager", self.ssh_manager
        ), patch.object(
            usbip.USBIPManager,
            "_create_windows_ssh",
            side_effect=lambda *args, **kwargs: win_ssh,
        ) as create_windows_ssh, patch.object(
            usbip, "probe_tcp_quality", return_value={"reachable": True}
        ), patch(
            "features.devices.usbip.time.sleep"
        ), patch.object(
            manager, "_is_windows_host", return_value=True
        ), patch.object(
            manager, "check_usbipd_installed", return_value=(True, "4.0.0")
        ), patch.object(
            manager, "_stop_windows_adb", return_value={"success": True}
        ), patch.object(
            manager, "_find_android_devices", return_value=["1-2"]
        ), patch.object(
            manager, "_ensure_vhci_driver"
        ), patch.object(
            manager, "_attach_devices", return_value=([], [])
        ):
            result = manager.start_usbip("hcq@172.16.14.66", device_password="pw")

        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "USBIP_ATTACH_FAILED")
        self.assertTrue(result["rollback_complete"])
        # Windows 侧新 bind 的 1-2 被回滚（detach 后 unbind）。
        self.assertIn("usbipd unbind --busid 1-2", exec_calls)
        self.assertIn("sudo usbip detach -p 00", exec_calls)
        # 回滚复用 prepare/bind 阶段的连接，失败路径不得再次建立 SSH。
        self.assertEqual(create_windows_ssh.call_count, 1)
        win_ssh.close.assert_called_once()

    def test_preexisting_shared_devices_are_not_rolled_back(self):
        manager = _manager(CONFIG, self.ssh_manager)
        win_ssh = MagicMock()
        exec_calls: list[str] = []

        def execute(target, cmd, timeout=None, get_pty=False):
            exec_calls.append(cmd)
            if target is win_ssh:
                responses = {
                    "powershell -Command \"$env:OS\"": ("Windows_NT", "", 0),
                    "usbipd --version": ("4.0.0", "", 0),
                    'tasklist /FI "IMAGENAME eq adb.exe" /NH': ("", "", 0),
                    # Device already Shared before this transaction.
                    "usbipd list | findstr 1-2": ("1-2 Shared", "", 0),
                }
                return responses.get(cmd, ("", "", 0))
            return ("", "", 0)

        self.ssh_manager.execute_command.side_effect = execute
        self.ssh_manager.get_connection.return_value = None  # Ubuntu unreachable

        with patch.object(
            usbip.USBIPManager,
            "_create_windows_ssh",
            side_effect=lambda *args, **kwargs: win_ssh,
        ), patch.object(usbip, "probe_tcp_quality", return_value={"reachable": True}), patch(
            "features.devices.usbip.time.sleep"
        ), patch.object(
            manager, "_is_windows_host", return_value=True
        ), patch.object(
            manager, "check_usbipd_installed", return_value=(True, "4.0.0")
        ), patch.object(
            manager, "_stop_windows_adb", return_value={"success": True}
        ), patch.object(
            manager, "_find_android_devices", return_value=["1-2"]
        ):
            result = manager.start_usbip("hcq@172.16.14.66", device_password="pw")

        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "USBIP_ATTACH_FAILED")
        # 1-2 是事务前已 Shared 的设备：bind 阶段跳过，回滚阶段不得动它。
        self.assertNotIn("usbipd bind --busid 1-2", exec_calls)
        self.assertNotIn("usbipd unbind --busid 1-2", exec_calls)
        # 没有新 bind 需要回滚时 rollback_complete 为 True。
        self.assertTrue(result["rollback_complete"])

    def test_ubuntu_unreachable_reports_rollback_state(self):
        manager = _manager(CONFIG, self.ssh_manager)
        win_ssh = MagicMock()

        def execute(target, cmd, timeout=None, get_pty=False):
            if target is win_ssh:
                responses = {
                    "powershell -Command \"$env:OS\"": ("Windows_NT", "", 0),
                    "usbipd --version": ("4.0.0", "", 0),
                    'tasklist /FI "IMAGENAME eq adb.exe" /NH': ("", "", 0),
                    "usbipd list | findstr 1-2": ("1-2 Not Shared", "", 0),
                    "usbipd bind --busid 1-2": ("", "", 0),
                    "usbipd detach --busid 1-2": ("", "", 0),
                    "usbipd unbind --busid 1-2": ("", "", 0),
                }
                return responses.get(cmd, ("", "", 0))
            return ("", "", 0)

        self.ssh_manager.execute_command.side_effect = execute
        self.ssh_manager.get_connection.return_value = None

        with patch.object(
            usbip.USBIPManager,
            "_create_windows_ssh",
            side_effect=lambda *args, **kwargs: win_ssh,
        ), patch.object(usbip, "probe_tcp_quality", return_value={"reachable": True}), patch(
            "features.devices.usbip.time.sleep"
        ), patch.object(
            manager, "_is_windows_host", return_value=True
        ), patch.object(
            manager, "check_usbipd_installed", return_value=(True, "4.0.0")
        ), patch.object(
            manager, "_stop_windows_adb", return_value={"success": True}
        ), patch.object(
            manager, "_find_android_devices", return_value=["1-2"]
        ):
            result = manager.start_usbip("hcq@172.16.14.66", device_password="pw")

        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "USBIP_ATTACH_FAILED")
        self.assertTrue(result["rollback_complete"])


if __name__ == "__main__":
    unittest.main()
