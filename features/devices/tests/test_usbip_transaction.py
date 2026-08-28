"""start_usbip 事务回滚测试。

attach 在 Ubuntu 侧失败时，本次事务在 Windows 上新 bind 的设备必须被
unbind 回滚（P1-4 幽灵 bind），且不能把之前已 Shared 的设备一并回滚。
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from features.devices import usbip, usbip_flash, usbip_transaction


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


class AttachStabilizationTests(unittest.TestCase):
    def test_waits_until_usbip_port_appears(self):
        class FakeSshManager:
            def __init__(self):
                self.port_calls = 0
                self.adb_calls = 0

            def execute_command(self, ssh, cmd, timeout=None, get_pty=False):
                if cmd == "adb devices":
                    self.adb_calls += 1
                    if self.adb_calls == 1:
                        return ("List of devices attached\n", "", 0)
                    return ("List of devices attached\nUSBIP001\tdevice\n", "", 0)
                if cmd.startswith("sudo usbip attach"):
                    return ("attached", "", 0)
                if cmd == "sudo -n /usr/bin/usbip port":
                    self.port_calls += 1
                    if self.port_calls == 1:
                        return ("Imported USB devices\n", "", 0)
                    return (
                        "Port 00: <Port in Use>\n"
                        "  1-1 -> usbip://172.16.14.66:3240/1-1\n",
                        "",
                        0,
                    )
                return ("", "", 0)

        manager = usbip.USBIPManager()
        manager.ssh_manager = FakeSshManager()
        with patch("features.devices.usbip.time.sleep", return_value=None):
            attached, _devices = manager._attach_devices(
                object(), "172.16.14.66", ["1-1"]
            )
        self.assertEqual(attached, ["1-1"])
        self.assertGreaterEqual(manager.ssh_manager.port_calls, 3)

    def test_retries_when_successful_attach_drops_before_enumeration(self):
        class FakeSshManager:
            def __init__(self):
                self.attach_calls = 0
                self.port_calls = 0
                self.adb_calls = 0

            def execute_command(self, ssh, cmd, timeout=None, get_pty=False):
                if cmd == "adb devices":
                    self.adb_calls += 1
                    if self.adb_calls == 1:
                        return ("List of devices attached\n", "", 0)
                    return (
                        "List of devices attached\nUSBIP001\tdevice\n",
                        "",
                        0,
                    )
                if cmd == "fastboot devices":
                    return ("", "", 0)
                if cmd.startswith("sudo usbip attach"):
                    self.attach_calls += 1
                    return ("", "", 0)
                if cmd == "sudo -n /usr/bin/usbip port":
                    self.port_calls += 1
                    # 前两次 attach 都在枚举前掉线：每轮包含
                    # 6 次首快照轮询 + 1 次稳定性快照。第三次
                    # attach 后连续两次都能观测到精确 BUSID。
                    if self.port_calls <= 14:
                        return ("Imported USB devices\n", "", 0)
                    return (
                        "Port 00: <Port in Use>\n"
                        "  1-1 -> usbip://172.16.14.66:3240/1-1\n",
                        "",
                        0,
                    )
                return ("", "", 0)

        manager = usbip.USBIPManager()
        manager.ssh_manager = FakeSshManager()
        with patch("features.devices.usbip.time.sleep", return_value=None):
            attached, devices = manager._attach_devices(
                object(), "172.16.14.66", ["1-1"]
            )

        self.assertEqual(attached, ["1-1"])
        self.assertEqual(devices, ["USBIP001"])
        self.assertEqual(manager.ssh_manager.attach_calls, 3)


class AutoBindPolicyTests(unittest.TestCase):
    def test_firmware_policy_is_added_and_verified_for_assigned_busid(self):
        ssh_manager = MagicMock()
        manager = _manager(CONFIG, ssh_manager)
        win_ssh = MagicMock()
        policy_lists = iter([
            ("GUID EFFECT OPERATION BUSID\n", "", 0),
            ("abc Allow AutoBind 1-1\n", "", 0),
        ])

        def execute(_target, cmd, timeout=None, get_pty=False):
            if cmd == "usbipd policy list":
                return next(policy_lists)
            if cmd.startswith("usbipd policy add"):
                return "policy added", "", 0
            raise AssertionError(cmd)

        ssh_manager.execute_command.side_effect = execute
        with patch.object(
            usbip_flash, "usbip_manager", manager
        ), patch.object(
            manager, "_create_windows_ssh", return_value=win_ssh
        ), patch.object(
            manager, "_is_windows_host", return_value=True
        ), patch.object(
            manager, "check_usbipd_installed", return_value=(True, "5.2.0")
        ):
            result = usbip_flash.ensure_usbip_auto_bind_policies(
                "hcq@172.16.14.66", ["1-1"], "pw"
            )

        self.assertTrue(result["success"])
        self.assertEqual(result["added_busids"], ["1-1"])
        ssh_manager.execute_command.assert_any_call(
            win_ssh,
            "usbipd policy add --effect allow --operation AutoBind --busid 1-1",
            timeout=15,
        )
        win_ssh.close.assert_called_once()

    def test_firmware_policy_requires_usbipd_policy_support(self):
        ssh_manager = MagicMock()
        manager = _manager(CONFIG, ssh_manager)
        win_ssh = MagicMock()
        ssh_manager.execute_command.return_value = (
            "", "Unknown command 'policy'", 1
        )
        with patch.object(
            usbip_flash, "usbip_manager", manager
        ), patch.object(
            manager, "_create_windows_ssh", return_value=win_ssh
        ), patch.object(
            manager, "_is_windows_host", return_value=True
        ), patch.object(
            manager, "check_usbipd_installed", return_value=(True, "4.1.0")
        ):
            result = usbip_flash.ensure_usbip_auto_bind_policies(
                "hcq@172.16.14.66", ["1-1"], "pw"
            )

        self.assertFalse(result["success"])
        self.assertIn("4.2.0", result["error"])


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
            if cmd == "sudo -n /usr/bin/usbip port":
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


class UsbipPortCommandTests(unittest.TestCase):
    """usbip 客户端路径不能写死：install.sh sudoers 同时放行两个位置。"""

    def test_env_override_wins(self):
        with patch.dict(
            "os.environ", {"USBIP_BIN": "/opt/usbip"}, clear=False,
        ):
            self.assertEqual(
                usbip_transaction._resolve_usbip_command(), "/opt/usbip"
            )

    def test_falls_back_through_known_locations_to_path(self):
        with patch.dict("os.environ", {}, clear=True), patch.object(
            usbip_transaction.os.path, "exists", return_value=False,
        ):
            self.assertEqual(usbip_transaction._resolve_usbip_command(), "usbip")
        with patch.dict("os.environ", {}, clear=True), patch.object(
            usbip_transaction.os.path, "exists",
            side_effect=lambda p: p == "/usr/sbin/usbip",
        ):
            self.assertEqual(
                usbip_transaction._resolve_usbip_command(), "/usr/sbin/usbip"
            )
        self.assertTrue(
            usbip_transaction.USBIP_PORT_COMMAND.startswith("sudo -n ")
        )
        self.assertTrue(usbip_transaction.USBIP_PORT_COMMAND.endswith(" port"))


class StartUsbipDetachScopingTests(unittest.TestCase):
    def test_start_usbip_detaches_only_selected_busids(self):
        win_ssh = MagicMock()
        ssh_manager = MagicMock()
        ssh_manager.get_connection.return_value = MagicMock()
        manager = _manager(CONFIG, ssh_manager)
        ubuntu_port_responses = iter([
            ("Imported USB devices\n", "", 0),
            ("Imported USB devices\n", "", 0),
        ])

        def execute(target, cmd, timeout=None, get_pty=False):
            if target is win_ssh:
                responses = {
                    "powershell -Command \"$env:OS\"": ("Windows_NT", "", 0),
                    "usbipd --version": ("4.2.0", "", 0),
                    'tasklist /FI "IMAGENAME eq adb.exe" /NH': ("", "", 0),
                    "usbipd list | findstr 1-2": ("1-2 Not Shared", "", 0),
                    "usbipd bind --busid 1-2": ("", "", 0),
                    "usbipd detach --busid 1-2": ("", "", 0),
                    "usbipd unbind --busid 1-2": ("", "", 0),
                }
                return responses.get(cmd, ("", "", 0))
            if cmd == "sudo -n /usr/bin/usbip port":
                return next(ubuntu_port_responses)
            return ("", "", 0)

        ssh_manager.execute_command.side_effect = execute
        with patch.object(
            usbip.usbip_manager, "ssh_manager", ssh_manager
        ), patch.object(
            usbip.USBIPManager,
            "_create_windows_ssh",
            side_effect=lambda *args, **kwargs: win_ssh,
        ), patch.object(
            usbip, "probe_tcp_quality", return_value={"reachable": True}
        ), patch(
            "features.devices.usbip.time.sleep"
        ), patch.object(
            manager, "_is_windows_host", return_value=True
        ), patch.object(
            manager, "check_usbipd_installed", return_value=(True, "4.2.0")
        ), patch.object(
            manager, "_stop_windows_adb", return_value={"success": True}
        ), patch.object(
            manager, "_find_android_devices", return_value=["1-2"]
        ), patch.object(
            manager, "_ensure_vhci_driver"
        ), patch.object(
            manager, "_attach_devices", return_value=([], [])
        ), patch.object(
            usbip, "detach_ubuntu_usbip_ports"
        ) as detach:
            manager.start_usbip("hcq@172.16.14.66", device_password="pw")

        detach.assert_called_once()
        self.assertEqual(detach.call_args.kwargs.get("detach_all"), False)
        # detach 必须限定到本次 attach 的 BUSID，不能波及同主机其他设备。
        self.assertEqual(detach.call_args.kwargs.get("busids"), ["1-2"])
        self.assertEqual(detach.call_args.args[1], "172.16.14.66")


if __name__ == "__main__":
    unittest.main()
