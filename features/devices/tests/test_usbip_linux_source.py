"""Ubuntu/Linux USB/IP 来源主机支持测试。

覆盖：udev 设备清单解析、BUSID 预测、usbipd 服务端启动/复用/停止的
命令编排，以及 manager 的来源 OS 分支。
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from features.devices import usbip, usbip_flash, usbip_linux_source


UDEV_OUTPUT = """@@DEV /dev/bus/usb/001/017
BUSNUM=001
DEVNUM=017
DEVPATH=/devices/platform/fd800000.usb/usb1/1-2/1-2.13
ID_VENDOR_ID=2207
ID_MODEL_ID=0006
ID_SERIAL_SHORT=RKTEST123
ID_VENDOR_FROM_DATABASE=Fuzhou Rockchip Electronics Company
ID_MODEL_FROM_DATABASE=unknown product
@@END
@@DEV /dev/bus/usb/001/018
BUSNUM=001
DEVNUM=018
DEVPATH=/devices/platform/fd800000.usb/usb1/1-3
ID_VENDOR_ID=046d
ID_MODEL_ID=c077
ID_VENDOR_FROM_DATABASE=Logitech, Inc.
ID_MODEL_FROM_DATABASE=M105 Optical Mouse
@@END
@@DEV /dev/bus/usb/001/019
BUSNUM=001
DEVNUM=019
DEVPATH=/devices/platform/fd800000.usb/usb1/1-2/1-2.4
ID_VENDOR_ID=18d1
ID_MODEL_ID=4d00
ID_MODEL=USB download gadget
@@END
"""


def _fake_ssh_manager(responses: dict[str, tuple[str, str, int]]):
    ssh_manager = MagicMock()
    calls: list[str] = []

    def execute(target, cmd, timeout=None, get_pty=False):
        calls.append(cmd)
        for key, value in responses.items():
            if cmd.startswith(key) or cmd == key:
                return value
        return ("", "", 0)

    ssh_manager.execute_command.side_effect = execute
    ssh_manager.calls = calls
    return ssh_manager


class UdevInventoryTests(unittest.TestCase):
    def test_parse_property_blocks_and_predict_busid(self):
        blocks = usbip_linux_source.parse_udev_property_blocks(UDEV_OUTPUT)
        self.assertEqual(len(blocks), 3)
        self.assertEqual(blocks[0]["ID_SERIAL_SHORT"], "RKTEST123")
        # 1-2.13 → 父hub端口13；busnum=1、devnum=17。
        self.assertEqual(
            usbip_linux_source.predict_linux_usbip_busid("001", "017", blocks[0]["DEVPATH"]),
            "1-17-13",
        )
        # 顶层设备 1-3 → 端口3。
        self.assertEqual(
            usbip_linux_source.predict_linux_usbip_busid("001", "018", blocks[1]["DEVPATH"]),
            "1-18-3",
        )

    def test_list_filters_android_devices_by_vid_and_marker(self):
        ssh_manager = _fake_ssh_manager({
            "for d in /dev/bus/usb": (UDEV_OUTPUT, "", 0),
        })
        devices = usbip_linux_source.list_ubuntu_usb_devices(
            ssh_manager,
            MagicMock(),
            vid_pids=("2207:0006", "18d1:4d00"),
            markers=("rockusb", "usb download gadget"),
        )
        busids = [item["busid"] for item in devices]
        self.assertIn("1-17-13", busids)   # 2207:0006 精确匹配
        self.assertIn("1-19-4", busids)    # 18d1:4d00 VID 匹配
        self.assertNotIn("1-18-3", busids) # 鼠标被过滤
        by_busid = {item["busid"]: item for item in devices}
        self.assertEqual(by_busid["1-17-13"]["serial"], "RKTEST123")
        self.assertEqual(by_busid["1-19-4"]["serial"], "")
        self.assertIn("2207:0006", by_busid["1-17-13"]["label"])

    def test_list_without_filters_returns_all_devices(self):
        ssh_manager = _fake_ssh_manager({
            "for d in /dev/bus/usb": (UDEV_OUTPUT, "", 0),
        })
        devices = usbip_linux_source.list_ubuntu_usb_devices(
            ssh_manager, MagicMock(),
        )
        self.assertEqual(len(devices), 3)


class ServerLifecycleTests(unittest.TestCase):
    def test_missing_binary_auto_deploy_fails_with_install_guide(self):
        # 无可用 usbipd 且自动部署失败（sudo 与用户目录均不可写）时，
        # 错误必须带上自动部署失败原因和安装指引。
        ssh_manager = _fake_ssh_manager({
            "for b in": ("", "", 0),
            "sudo -n install": ("", "sudo: a password is required", 1),
            "mkdir -p": ("", "mkdir: cannot create directory", 1),
        })
        result = usbip_linux_source.ensure_ubuntu_usbip_server(
            ssh_manager, MagicMock(), serials=["S1"],
        )
        self.assertFalse(result["success"])
        self.assertIn("自动部署失败", result["error"])
        self.assertIn("install_guide", result)

    def test_missing_binary_auto_deploys_then_starts(self):
        # sudo 安装成功后 resolve 能找到新二进制并继续启动。
        responses = {
            "for b in": ("/usr/local/bin/usbipd\n", "", 0),
            "/usr/local/bin/usbipd --version": ("usbipd 0.9.2", "", 0),
            "pgrep -af": ("", "", 1),
        }
        ssh_manager = _fake_ssh_manager(responses)

        def execute(target, cmd, timeout=None, get_pty=False):
            ssh_manager.calls.append(cmd)
            # 部署前来源没有任何可用 usbipd。
            if cmd.startswith("for b in") and not any(
                item.startswith("sudo -n install") for item in ssh_manager.calls[:-1]
            ):
                return ("", "", 0)
            if cmd.startswith("pgrep"):
                if any(
                    cmd_item.startswith("/usr/local/bin/usbipd bind")
                    for cmd_item in ssh_manager.calls
                ):
                    return ("123 /usr/local/bin/usbipd bind --stop-adb --serial S1", "", 0)
                return ("", "", 1)
            for key, value in responses.items():
                if cmd.startswith(key) or cmd == key:
                    return value
            return ("", "", 0)

        ssh_manager.execute_command.side_effect = execute
        result = usbip_linux_source.ensure_ubuntu_usbip_server(
            ssh_manager, MagicMock(), serials=["S1"],
        )
        self.assertTrue(result["success"])
        self.assertTrue(result["started"])
        self.assertTrue(any(
            cmd.startswith("sudo -n install") for cmd in ssh_manager.calls
        ))

    def test_resolve_prefers_newest_candidate(self):
        # 用户目录部署的新版本必须压过 PATH 里的旧系统安装，
        # 否则无 sudo 主机上的自动升级永远不生效。
        ssh_manager = _fake_ssh_manager({
            "for b in": (
                "/usr/bin/usbipd\n/home/wlq/.local/bin/usbipd\n", "", 0,
            ),
            "/usr/bin/usbipd --version": ("usbipd 0.9.0", "", 0),
            "/home/wlq/.local/bin/usbipd --version": ("usbipd 0.9.2", "", 0),
        })
        binary, version = usbip_linux_source.resolve_linux_usbipd_bin(
            ssh_manager, MagicMock(),
        )
        self.assertEqual(binary, "/home/wlq/.local/bin/usbipd")
        self.assertEqual(version, "usbipd 0.9.2")

    def test_install_falls_back_to_user_local_without_sudo(self):
        # sudo 需要密码的主机上，安装必须回退到用户目录并验证可用。
        responses = {
            "sudo -n install": ("", "sudo: a password is required", 1),
            "mkdir -p": ("", "", 0),
            "/home/wlq/.local/bin/usbipd --version": ("usbipd 0.9.2", "", 0),
        }
        ssh_manager = _fake_ssh_manager(responses)

        def execute(target, cmd, timeout=None, get_pty=False):
            ssh_manager.calls.append(cmd)
            # 部署前来源没有可用二进制，用户目录安装后才出现。
            if cmd.startswith("for b in"):
                if any(
                    item.startswith("mkdir -p") for item in ssh_manager.calls[:-1]
                ):
                    return ("/home/wlq/.local/bin/usbipd\n", "", 0)
                return ("", "", 0)
            for key, value in responses.items():
                if cmd.startswith(key) or cmd == key:
                    return value
            return ("", "", 0)

        ssh_manager.execute_command.side_effect = execute
        result = usbip_linux_source.install_ubuntu_usbipd(
            ssh_manager, MagicMock(),
        )
        self.assertTrue(result["success"])
        self.assertTrue(result["user_local"])
        self.assertEqual(result["version"], "usbipd 0.9.2")
        self.assertTrue(any(
            cmd.startswith("sudo -n install") for cmd in ssh_manager.calls
        ))
        self.assertTrue(any(
            cmd.startswith("mkdir -p") for cmd in ssh_manager.calls
        ))

    def test_old_version_is_rejected(self):
        ssh_manager = _fake_ssh_manager({
            "for b in": ("/usr/local/bin/usbipd\n", "", 0),
            "/usr/local/bin/usbipd --version": ("usbipd 0.9.1", "", 0),
            "sudo -n install": ("", "sudo: a password is required", 1),
            "mkdir -p": ("", "mkdir: cannot create directory", 1),
        })
        result = usbip_linux_source.ensure_ubuntu_usbip_server(
            ssh_manager, MagicMock(), serials=["S1"],
        )
        self.assertFalse(result["success"])
        self.assertIn("版本过低", result["error"])
        self.assertIn("自动部署失败", result["error"])

    def test_start_new_server_with_serial_filters(self):
        responses = {
            "for b in": ("/usr/local/bin/usbipd\n", "", 0),
            "/usr/local/bin/usbipd --version": ("usbipd 0.9.2", "", 0),
            "pgrep -af": ("", "", 1),
        }
        ssh_manager = _fake_ssh_manager(responses)

        # 启动命令执行后进程出现。
        def execute(target, cmd, timeout=None, get_pty=False):
            ssh_manager.calls.append(cmd)
            if cmd.startswith("pgrep"):
                if any(
                    cmd_item.startswith("/usr/local/bin/usbipd bind")
                    for cmd_item in ssh_manager.calls
                ):
                    return ("123 /usr/local/bin/usbipd bind --stop-adb --serial S1", "", 0)
                return ("", "", 1)
            for key, value in responses.items():
                if cmd.startswith(key) or cmd == key:
                    return value
            return ("", "", 0)

        ssh_manager.execute_command.side_effect = execute
        result = usbip_linux_source.ensure_ubuntu_usbip_server(
            ssh_manager, MagicMock(), serials=["S1"],
        )
        self.assertTrue(result["success"])
        self.assertTrue(result["started"])
        start_cmd = next(
            cmd for cmd in ssh_manager.calls
            if cmd.startswith("/usr/local/bin/usbipd bind")
        )
        self.assertIn("--stop-adb", start_cmd)
        self.assertIn("--serial S1", start_cmd)

    def test_reuse_running_server_covering_requested_serials(self):
        ssh_manager = _fake_ssh_manager({
            "for b in": ("/usr/local/bin/usbipd\n", "", 0),
            "/usr/local/bin/usbipd --version": ("usbipd 0.9.2", "", 0),
            "pgrep -af": (
                "123 /usr/local/bin/usbipd bind --stop-adb --serial S1 --serial S2",
                "", 0,
            ),
        })
        result = usbip_linux_source.ensure_ubuntu_usbip_server(
            ssh_manager, MagicMock(), serials=["S2"],
        )
        self.assertTrue(result["success"])
        self.assertTrue(result["reused"])
        self.assertFalse(
            any(cmd.startswith("/usr/local/bin/usbipd bind") for cmd in ssh_manager.calls)
        )

    def test_restart_merges_serial_filters(self):
        responses = {
            "for b in": ("/usr/local/bin/usbipd\n", "", 0),
            "/usr/local/bin/usbipd --version": ("usbipd 0.9.2", "", 0),
            "pkill -f": ("", "", 0),
        }
        ssh_manager = _fake_ssh_manager(responses)
        # 依次：ensure 初始查询(运行中S1) → stop 内部查询(运行中) →
        # stop 验证(已消失) → 启动后轮询(运行中S1+S2)。
        pgrep_results = [
            ("99 /usr/local/bin/usbipd bind --stop-adb --serial S1", "", 0),
            ("99 /usr/local/bin/usbipd bind --stop-adb --serial S1", "", 0),
            ("", "", 1),
            ("100 /usr/local/bin/usbipd bind --stop-adb --serial S1 --serial S2", "", 0),
        ]
        pgrep_index = {"i": 0}

        def execute(target, cmd, timeout=None, get_pty=False):
            ssh_manager.calls.append(cmd)
            if cmd.startswith("pgrep"):
                result = pgrep_results[min(pgrep_index["i"], len(pgrep_results) - 1)]
                pgrep_index["i"] += 1
                return result
            for key, value in responses.items():
                if cmd.startswith(key) or cmd == key:
                    return value
            return ("", "", 0)

        ssh_manager.execute_command.side_effect = execute
        result = usbip_linux_source.ensure_ubuntu_usbip_server(
            ssh_manager, MagicMock(), serials=["S2"],
        )
        self.assertTrue(result["success"])
        self.assertTrue(result["started"])
        self.assertEqual(result["serials"], ["S1", "S2"])
        self.assertTrue(any("pkill" in cmd for cmd in ssh_manager.calls))

    def test_stop_server_kills_and_verifies(self):
        responses = {"pkill -f": ("", "", 0)}
        ssh_manager = _fake_ssh_manager(responses)
        pgrep_results = [("5 usbipd bind --vid 2207", "", 0), ("", "", 1)]
        pgrep_index = {"i": 0}

        def execute(target, cmd, timeout=None, get_pty=False):
            ssh_manager.calls.append(cmd)
            if cmd.startswith("pgrep"):
                result = pgrep_results[min(pgrep_index["i"], len(pgrep_results) - 1)]
                pgrep_index["i"] += 1
                return result
            for key, value in responses.items():
                if cmd.startswith(key) or cmd == key:
                    return value
            return ("", "", 0)

        ssh_manager.execute_command.side_effect = execute
        result = usbip_linux_source.stop_ubuntu_usbip_server(
            ssh_manager, MagicMock(),
        )
        self.assertTrue(result["success"])
        self.assertTrue(result["stopped"])


class ManagerOsBranchTests(unittest.TestCase):
    def _manager(self, ssh_manager) -> usbip.USBIPManager:
        manager = usbip.USBIPManager.__new__(usbip.USBIPManager)
        manager.ssh_manager = ssh_manager
        manager.config_manager = MagicMock()
        manager.config_manager.load_config.return_value = {}
        manager.device_sources = {}
        return manager

    def test_detect_source_os_windows_and_linux(self):
        ssh_manager = _fake_ssh_manager({
            "ver 2>&1": ("Microsoft Windows [Version 10.0]", "", 0),
        })
        self.assertEqual(
            self._manager(ssh_manager)._detect_source_os(MagicMock()), "windows",
        )
        ssh_manager = _fake_ssh_manager({
            "ver 2>&1": ("bash: ver: command not found", "", 127),
            "uname -s": ("Linux", "", 0),
        })
        self.assertEqual(
            self._manager(ssh_manager)._detect_source_os(MagicMock()), "linux",
        )

    def test_public_source_os_values(self):
        self.assertEqual(usbip.USBIPManager._source_os_public("windows"), "windows")
        self.assertEqual(usbip.USBIPManager._source_os_public("linux"), "ubuntu")

    def test_rollback_linux_only_stops_started_server(self):
        ssh_manager = _fake_ssh_manager({
            "pgrep -af": ("", "", 1),
        })
        manager = self._manager(ssh_manager)
        # started=True → 停止；reused（started=False）→ 不动。
        self.assertTrue(
            manager._rollback_source_side(MagicMock(), {"kind": "linux", "started": False})
        )
        self.assertFalse(
            any("pkill" in cmd for cmd in ssh_manager.calls)
        )

    def test_rollback_windows_dispatches_to_windows_binds(self):
        manager = self._manager(MagicMock())
        with patch.object(
            manager, "_rollback_windows_binds", return_value=True,
        ) as mock_rollback:
            self.assertTrue(
                manager._rollback_source_side(
                    MagicMock(), {"kind": "windows", "newly_bound": ["1-2"]},
                )
            )
        mock_rollback.assert_called_once()

    def test_ensure_source_export_ready_reports_version_failure(self):
        # 来源 usbipd 版本过低且自动部署失败（sudo 需要密码）时，失败原因
        # 和安装指引必须向上传递，供 connect 预检把真实错误返回给用户而
        # 不是误导性的路由建议。
        ssh_manager = _fake_ssh_manager({
            "for b in": ("/usr/bin/usbipd\n", "", 0),
            "/usr/bin/usbipd --version": ("usbipd 0.9.0", "", 0),
            "sudo -n install": ("", "sudo: a password is required", 1),
            "mkdir -p": ("", "mkdir: cannot create directory", 1),
        })
        manager = self._manager(ssh_manager)
        manager.config_manager.load_config.return_value = {"device_pswd": "pw"}
        with patch.object(
            manager, "_create_windows_ssh", return_value=MagicMock(),
        ), patch.object(
            manager, "_detect_source_os", return_value="linux",
        ), patch.object(
            manager,
            "_find_android_devices_linux",
            return_value=[{"busid": "1-2", "serial": "S1", "vid_pid": "2207:0006"}],
        ):
            result = manager.ensure_source_export_ready("wlq@10.0.0.5", ["1-2"])
        self.assertFalse(result["success"])
        self.assertFalse(result["started"])
        self.assertIn("版本过低", result["detail"])
        self.assertIn("0.9.0", result["detail"])
        self.assertTrue(result["install_guide"])


class AutoBindUbuntuTests(unittest.TestCase):
    def test_ensure_ubuntu_export_uses_assignment_serials(self):
        ssh_manager = _fake_ssh_manager({
            "for b in": ("/usr/local/bin/usbipd\n", "", 0),
            "/usr/local/bin/usbipd --version": ("usbipd 0.9.2", "", 0),
            "pgrep -af": (
                "7 /usr/local/bin/usbipd bind --stop-adb --serial RKTEST123",
                "", 0,
            ),
        })
        manager = usbip.USBIPManager.__new__(usbip.USBIPManager)
        manager.ssh_manager = ssh_manager
        manager.config_manager = MagicMock()
        manager.config_manager.load_config.return_value = {}
        manager.device_sources = {}
        runtime_config = {
            "usbip_cluster_assignments": {
                "hcq@10.0.0.5|1-17-13": {
                    "device_host": "hcq@10.0.0.5",
                    "busid": "1-17-13",
                    "device_serials": ["RKTEST123"],
                    "status": "attached",
                },
            },
        }
        manager.config_manager.get_runtime_config.return_value = runtime_config
        with patch.object(
            usbip_flash, "open_usbip_source_ssh",
            return_value=(MagicMock(), ""),
        ), patch.object(
            usbip_flash.usbip_manager, "_detect_source_os", return_value="linux",
        ), patch.object(
            usbip_flash.usbip_manager, "_find_android_devices_linux",
            return_value=[],
        ), patch.object(
            usbip_flash.usbip_manager, "ssh_manager", ssh_manager,
        ), patch.object(
            usbip_flash.usbip_manager, "config_manager", manager.config_manager,
        ), patch.object(
            usbip_flash.runtime, "config_manager", manager.config_manager,
        ):
            result = usbip_flash.ensure_usbip_auto_bind_policies(
                "hcq@10.0.0.5", ["1-17-13"],
            )
        self.assertTrue(result["success"])
        self.assertEqual(result["source_os"], "ubuntu")


class SourceOsCacheTests(unittest.TestCase):
    def test_record_and_lookup_source_os(self):
        from features.devices import usbip_persistence

        config_manager = MagicMock()
        state: dict = {}

        def get_runtime_config():
            return state.get("cfg") or {}

        def update(patch):
            state.setdefault("cfg", {}).update(patch)
            return True

        config_manager.get_runtime_config.side_effect = get_runtime_config
        config_manager.update_runtime_config.side_effect = update
        with patch.object(
            usbip_persistence.runtime, "config_manager", config_manager,
        ):
            # 对外值 "ubuntu" 归一化为内部 "linux"。
            usbip_persistence.record_usbip_source_os("hcq@10.0.0.5", "ubuntu")
            self.assertEqual(
                usbip_persistence.lookup_usbip_source_os("hcq@10.0.0.5"),
                "linux",
            )
            self.assertEqual(
                usbip_persistence.lookup_usbip_source_os("other@10.0.0.9"),
                "",
            )
            # 未知 OS 不写入。
            usbip_persistence.record_usbip_source_os("hcq@10.0.0.6", "macos")
            self.assertEqual(
                usbip_persistence.lookup_usbip_source_os("hcq@10.0.0.6"),
                "",
            )
            # 同值短窗口内不重复写盘。
            calls_before = config_manager.update_runtime_config.call_count
            usbip_persistence.record_usbip_source_os("hcq@10.0.0.5", "linux")
            self.assertEqual(
                config_manager.update_runtime_config.call_count, calls_before,
            )
            # OS 变化（如主机重装）会更新缓存。
            usbip_persistence.record_usbip_source_os("hcq@10.0.0.5", "windows")
            self.assertEqual(
                usbip_persistence.lookup_usbip_source_os("hcq@10.0.0.5"),
                "windows",
            )

    def test_probe_source_os_detects_linux(self):
        ssh_manager = _fake_ssh_manager({
            "ver 2>&1": ("bash: ver: command not found", "", 127),
            "uname -s": ("Linux", "", 0),
        })
        manager = usbip.USBIPManager.__new__(usbip.USBIPManager)
        manager.ssh_manager = ssh_manager
        manager.config_manager = MagicMock()
        manager.config_manager.load_config.return_value = {"device_pswd": "pw"}
        manager.config_manager.find_device_host_password.return_value = None
        manager.device_sources = {}
        with patch.object(
            manager, "_create_windows_ssh", return_value=MagicMock(),
        ):
            result = manager.probe_source_os("hcq@10.0.0.5")
        self.assertEqual(result["source_os"], "linux")

    def test_probe_source_os_without_credentials(self):
        manager = usbip.USBIPManager.__new__(usbip.USBIPManager)
        manager.ssh_manager = MagicMock()
        manager.config_manager = MagicMock()
        manager.config_manager.load_config.return_value = {}
        manager.config_manager.find_device_host_password.return_value = None
        manager.device_sources = {}
        result = manager.probe_source_os("hcq@10.0.0.5")
        self.assertEqual(result["source_os"], "")
        self.assertIn("SSH凭据", result["error"])


if __name__ == "__main__":
    unittest.main()
