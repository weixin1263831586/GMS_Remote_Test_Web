"""Ubuntu/Linux USB/IP 来源主机支持测试。

覆盖：udev 设备清单解析、BUSID 预测、usbipd 服务端启动/复用/停止的
命令编排，以及 manager 的来源 OS 分支。
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from foundation.command_result import CommandResult
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


def _cr(value) -> CommandResult:
    """历史 tuple ``(stdout, stderr, code)`` → :class:`CommandResult`。"""
    if isinstance(value, CommandResult):
        return _cr(value)
    stdout, stderr, code = value
    return CommandResult(stdout=stdout, stderr=stderr, code=code)


def _fake_ssh_manager(responses: dict[str, tuple[str, str, int]]):
    ssh_manager = MagicMock()
    calls: list[str] = []

    def execute(target, cmd, timeout=None, get_pty=False):
        calls.append(cmd)
        for key, value in responses.items():
            if cmd.startswith(key) or cmd == key:
                return _cr(value)
        return CommandResult(stdout="", stderr="", code=0)

    ssh_manager.execute_command.side_effect = execute
    ssh_manager.calls = calls
    return ssh_manager


def _pgrep_protocol_state(candidates: list[tuple[str, str]]):
    """Shared state for the new pgrep -f + /proc/<pid>/cmdline protocol.

    ``candidates`` 是当前 (pid, cmdline) 列表；pgrep -f 只返回 PID，
    /proc/<pid>/cmdline 查询返回对应 argv。测试通过修改 ``candidates``[:]`
    模拟进程出现/消失。
    """
    return {"candidates": list(candidates)}


def _execute_with_pgrep_protocol(state, ssh_manager, responses):
    """Build an execute() implementing pgrep -f + /proc ownership checks."""

    def execute(target, cmd, timeout=None, get_pty=False):
        ssh_manager.calls.append(cmd)
        if cmd.startswith("pgrep"):
            pids = "\n".join(pid for pid, _cmdline in state["candidates"])
            return _cr((pids + "\n" if pids else "", "", 0 if pids else 1))
        if "/proc/" in cmd and "/cmdline" in cmd:
            pid = cmd.split("/proc/")[1].split("/")[0]
            for cpid, cmdline in state["candidates"]:
                if cpid == pid:
                    return _cr((cmdline, "", 0))
            return CommandResult(stdout="", stderr="", code=1)
        if cmd.startswith("cat ") and "gms-usbipd.pid" in cmd:
            candidates = state["candidates"]
            if candidates:
                return _cr((candidates[0][0] + "\n", "", 0))
            return CommandResult(stdout="", stderr="", code=1)
        for key, value in responses.items():
            if cmd.startswith(key) or cmd == key:
                return _cr(value)
        return CommandResult(stdout="", stderr="", code=0)

    return execute


# Worker 出口 IP 解析所需的通用响应：来源 SSH 会话可见地址 10.0.0.5，
# Worker 访问该地址的出口 IP 为 172.16.10.20。
WORKER_EGRESS_SETUP = {
    "echo $SSH_CONNECTION": ("10.0.0.9 54321 10.0.0.5 22\n", "", 0),
    "ip route get": ("10.0.0.5 dev eth0 src 172.16.10.20\n", "", 0),
}


def _worker_ssh_factory():
    """Fake per-worker SSH factory recording hosts and returning mocks."""
    opened: dict[str, object] = {}

    def factory(host: str):
        conn = MagicMock()
        opened[host] = conn
        return conn

    return factory, opened


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
            "/usr/local/bin/usbipd --version": ("usbipd 0.9.5", "", 0),
            **WORKER_EGRESS_SETUP,
        }
        ssh_manager = _fake_ssh_manager(responses)
        worker_factory, _opened = _worker_ssh_factory()
        # 部署前没有任何 usbipd 进程；启动命令执行后进程出现。
        state = _pgrep_protocol_state([])

        def execute(target, cmd, timeout=None, get_pty=False):
            ssh_manager.calls.append(cmd)
            # 部署前来源没有任何可用 usbipd。
            if cmd.startswith("for b in") and not any(
                item.startswith("sudo -n install") for item in ssh_manager.calls[:-1]
            ):
                return _cr(("", "", 0))
            if any(
                cmd_item.startswith("/usr/local/bin/usbipd bind")
                for cmd_item in ssh_manager.calls
            ) and not state["candidates"]:
                state["candidates"] = [
                    ("123", "usbipd bind --stop-adb --serial S1"),
                ]
            return _execute_with_pgrep_protocol(
                state, ssh_manager, responses
            )(target, cmd, timeout, get_pty)

        ssh_manager.execute_command.side_effect = execute
        result = usbip_linux_source.ensure_ubuntu_usbip_server(
            ssh_manager, MagicMock(), serials=["S1"],
            allow_worker_hosts=["wlq@172.16.10.20"],
            worker_ssh_factory=worker_factory,
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
            "/home/wlq/.local/bin/usbipd --version": ("usbipd 0.9.5", "", 0),
        })
        binary, version = usbip_linux_source.resolve_linux_usbipd_bin(
            ssh_manager, MagicMock(),
        )
        self.assertEqual(binary, "/home/wlq/.local/bin/usbipd")
        self.assertEqual(version, "usbipd 0.9.5")

    def test_install_falls_back_to_user_local_without_sudo(self):
        # sudo 需要密码的主机上，安装必须回退到用户目录并验证可用。
        responses = {
            "sudo -n install": ("", "sudo: a password is required", 1),
            "mkdir -p": ("", "", 0),
            "/home/wlq/.local/bin/usbipd --version": ("usbipd 0.9.5", "", 0),
        }
        ssh_manager = _fake_ssh_manager(responses)

        def execute(target, cmd, timeout=None, get_pty=False):
            ssh_manager.calls.append(cmd)
            # 部署前来源没有可用二进制，用户目录安装后才出现。
            if cmd.startswith("for b in"):
                if any(
                    item.startswith("mkdir -p") for item in ssh_manager.calls[:-1]
                ):
                    return _cr(("/home/wlq/.local/bin/usbipd\n", "", 0))
                return _cr(("", "", 0))
            for key, value in responses.items():
                if cmd.startswith(key) or cmd == key:
                    return _cr(value)
            return _cr(("", "", 0))

        ssh_manager.execute_command.side_effect = execute
        result = usbip_linux_source.install_ubuntu_usbipd(
            ssh_manager, MagicMock(),
        )
        self.assertTrue(result["success"])
        self.assertTrue(result["user_local"])
        self.assertEqual(result["version"], "usbipd 0.9.5")
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
            "/usr/local/bin/usbipd --version": ("usbipd 0.9.5", "", 0),
            **WORKER_EGRESS_SETUP,
        }
        ssh_manager = _fake_ssh_manager(responses)
        worker_factory, _opened = _worker_ssh_factory()
        # 启动命令执行后进程出现。
        state = _pgrep_protocol_state([])

        def execute(target, cmd, timeout=None, get_pty=False):
            if any(
                cmd_item.startswith("/usr/local/bin/usbipd bind")
                for cmd_item in ssh_manager.calls
            ) and not state["candidates"]:
                state["candidates"] = [
                    ("123", "usbipd bind --stop-adb --serial S1"),
                ]
            return _execute_with_pgrep_protocol(
                state, ssh_manager, responses
            )(target, cmd, timeout, get_pty)

        ssh_manager.execute_command.side_effect = execute
        result = usbip_linux_source.ensure_ubuntu_usbip_server(
            ssh_manager, MagicMock(), serials=["S1"],
            allow_worker_hosts=["wlq@172.16.10.20"],
            worker_ssh_factory=worker_factory,
        )
        self.assertTrue(result["success"])
        self.assertTrue(result["started"])
        start_cmd = next(
            cmd for cmd in ssh_manager.calls
            if cmd.startswith("/usr/local/bin/usbipd bind")
        )
        self.assertIn("--stop-adb", start_cmd)
        self.assertIn("--serial S1", start_cmd)
        self.assertIn("--allow-client 172.16.10.20", start_cmd)

    def test_reuse_running_server_covering_requested_serials(self):
        responses = {
            "for b in": ("/usr/local/bin/usbipd\n", "", 0),
            "/usr/local/bin/usbipd --version": ("usbipd 0.9.5", "", 0),
        }
        ssh_manager = _fake_ssh_manager(responses)
        state = _pgrep_protocol_state([
            ("123", "usbipd bind --stop-adb --serial S1 --serial S2"),
        ])
        ssh_manager.execute_command.side_effect = _execute_with_pgrep_protocol(
            state, ssh_manager, responses,
        )
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
            "/usr/local/bin/usbipd --version": ("usbipd 0.9.5", "", 0),
            "kill": ("", "", 0),
            **WORKER_EGRESS_SETUP,
        }
        ssh_manager = _fake_ssh_manager(responses)
        worker_factory, _opened = _worker_ssh_factory()
        # 流程：ensure 初始查询(运行中S1) → 覆盖不足合并 → kill PID 停止 →
        # 停止后查询(已消失) → 启动 → 启动后轮询(运行中S1+S2)。
        state = _pgrep_protocol_state([
            ("99", "usbipd bind --stop-adb --serial S1"),
        ])

        def execute(target, cmd, timeout=None, get_pty=False):
            outcome = _execute_with_pgrep_protocol(
                state, ssh_manager, responses
            )(target, cmd, timeout, get_pty)
            # kill 之后进程消失；启动命令执行后新进程出现。
            if cmd.startswith("kill "):
                state["candidates"] = []
            elif any(
                cmd_item.startswith("/usr/local/bin/usbipd bind")
                for cmd_item in ssh_manager.calls
            ):
                state["candidates"] = [
                    ("100", "usbipd bind --stop-adb --serial S1 --serial S2"),
                ]
            return outcome

        ssh_manager.execute_command.side_effect = execute
        result = usbip_linux_source.ensure_ubuntu_usbip_server(
            ssh_manager, MagicMock(), serials=["S2"],
            allow_worker_hosts=["wlq@172.16.10.20"],
            worker_ssh_factory=worker_factory,
        )
        self.assertTrue(result["success"])
        self.assertTrue(result["started"])
        self.assertEqual(result["serials"], ["S1", "S2"])
        # 停止必须是精确 kill PID；不允许 pkill -f。
        self.assertTrue(any(
            cmd.startswith("kill ") for cmd in ssh_manager.calls
        ))
        self.assertFalse(any("pkill" in cmd for cmd in ssh_manager.calls))

    def test_stop_server_kills_and_verifies(self):
        # 停止动作按 PID ownership：pgrep 只枚举候选 PID，每个候选必须
        # 通过 /proc/<pid>/cmdline 校验后才 kill；不再执行 pkill -f。
        responses = {"kill": ("", "", 0)}
        ssh_manager = _fake_ssh_manager(responses)
        # 依次：初始 pgrep(候选 5) → /proc/5 校验(usbipd bind) →
        # 停止后 pgrep(无)。
        pgrep_results = [("5", "", 0), ("", "", 1)]
        pgrep_index = {"i": 0}

        def execute(target, cmd, timeout=None, get_pty=False):
            ssh_manager.calls.append(cmd)
            if cmd.startswith("pgrep"):
                result = pgrep_results[min(pgrep_index["i"], len(pgrep_results) - 1)]
                pgrep_index["i"] += 1
                return _cr(result)
            if "/proc/5/cmdline" in cmd:
                return _cr(("usbipd bind --vid 2207\0", "", 0))
            for key, value in responses.items():
                if cmd.startswith(key) or cmd == key:
                    return _cr(value)
            return _cr(("", "", 0))

        ssh_manager.execute_command.side_effect = execute
        result = usbip_linux_source.stop_ubuntu_usbip_server(
            ssh_manager, MagicMock(),
        )
        self.assertTrue(result["success"])
        self.assertTrue(result["stopped"])
        # 停止命令必须是精确 kill PID，不允许出现 pkill -f。
        self.assertTrue(any(
            cmd.startswith("kill ") and " 5 " in f" {cmd} "
            for cmd in ssh_manager.calls
        ))
        self.assertFalse(any("pkill" in cmd for cmd in ssh_manager.calls))

    def test_running_cmdline_requires_usbipd_binary(self):
        # pgrep 输出中恰好包含 "usbipd bind" 字样的无关进程（如编辑器
        # 打开的日志）不能被误判为运行中的 usbipd。
        info = usbip_linux_source.parse_usbip_running_cmdline(
            "999 vim /tmp/usbipd bind notes",
        )
        self.assertFalse(info["running"])
        info = usbip_linux_source.parse_usbip_running_cmdline(
            "123 /usr/local/bin/usbipd bind --stop-adb --serial S1",
        )
        self.assertTrue(info["running"])
        self.assertEqual(info["pid"], "123")
        self.assertEqual(info["serials"], ["S1"])

    def test_running_cmdline_parses_quoted_serial_as_single_argv(self):
        # shlex.quote 包裹的 serial（含空格/元字符）必须解析为单个 serial。
        info = usbip_linux_source.parse_usbip_running_cmdline(
            "123 /usr/local/bin/usbipd bind --serial 'RK1; rm -rf /' --vid 2207",
        )
        self.assertTrue(info["running"])
        self.assertEqual(info["serials"], ["RK1; rm -rf /"])
        self.assertEqual(info["vids"], ["2207"])
        # --serial=value 形式同样支持。
        info = usbip_linux_source.parse_usbip_running_cmdline(
            "124 usbipd bind --serial=ABC_1 --vid=18D1",
        )
        self.assertEqual(info["serials"], ["ABC_1"])
        self.assertEqual(info["vids"], ["18d1"])

    def test_start_writes_pid_file_and_quotes_serial(self):
        # USB serial 是外部输入：包含 shell 元字符时必须被 shlex.quote，
        # 且启动命令要写 PID 文件供后续按 PID 停止。
        responses = {
            "for b in": ("/usr/local/bin/usbipd\n", "", 0),
            "/usr/local/bin/usbipd --version": ("usbipd 0.9.5", "", 0),
            **WORKER_EGRESS_SETUP,
        }
        ssh_manager = _fake_ssh_manager(responses)
        worker_factory, _opened = _worker_ssh_factory()
        state = _pgrep_protocol_state([])

        def execute(target, cmd, timeout=None, get_pty=False):
            if any(
                cmd_item.startswith("/usr/local/bin/usbipd bind")
                for cmd_item in ssh_manager.calls
            ) and not state["candidates"]:
                state["candidates"] = [
                    ("123", "usbipd bind --stop-adb --serial 'RK1; rm -rf /'"),
                ]
            return _execute_with_pgrep_protocol(
                state, ssh_manager, responses
            )(target, cmd, timeout, get_pty)

        ssh_manager.execute_command.side_effect = execute
        result = usbip_linux_source.ensure_ubuntu_usbip_server(
            ssh_manager, MagicMock(), serials=['RK1; rm -rf /'],
            allow_worker_hosts=["wlq@172.16.10.20"],
            worker_ssh_factory=worker_factory,
        )
        self.assertTrue(result["success"])
        start_cmd = next(
            cmd for cmd in ssh_manager.calls
            if cmd.startswith("/usr/local/bin/usbipd bind")
        )
        self.assertIn("--serial 'RK1; rm -rf /'", start_cmd)
        self.assertIn("gms-usbipd.pid", start_cmd)
        self.assertIn("echo $!", start_cmd)

    def test_vid_only_request_not_reused_from_serial_only_instance(self):
        # P1-4 回归：旧实例带 --serial S1，请求追加 --vid 18d1 时，
        # serial-only 实例只导出串号命中的设备，不能声称已覆盖 VID。
        responses = {
            "for b in": ("/usr/local/bin/usbipd\n", "", 0),
            "/usr/local/bin/usbipd --version": ("usbipd 0.9.5", "", 0),
            "kill": ("", "", 0),
            **WORKER_EGRESS_SETUP,
        }
        ssh_manager = _fake_ssh_manager(responses)
        worker_factory, _opened = _worker_ssh_factory()
        # 流程：初始查询(serial-only S1) → 覆盖不足 kill → 重新查询(无) →
        # 启动 → 启动后轮询(S1 + vid 18d1)。
        state = _pgrep_protocol_state([
            ("9", "usbipd bind --stop-adb --serial S1"),
        ])

        def execute(target, cmd, timeout=None, get_pty=False):
            outcome = _execute_with_pgrep_protocol(
                state, ssh_manager, responses
            )(target, cmd, timeout, get_pty)
            if cmd.startswith("kill "):
                state["candidates"] = []
            elif any(
                cmd_item.startswith("/usr/local/bin/usbipd bind")
                for cmd_item in ssh_manager.calls
            ) and not state["candidates"]:
                state["candidates"] = [(
                    "10",
                    "usbipd bind --stop-adb --serial S1 --vid 18d1",
                )]
            return outcome

        ssh_manager.execute_command.side_effect = execute
        result = usbip_linux_source.ensure_ubuntu_usbip_server(
            ssh_manager, MagicMock(), serials=[], vids=["18d1"],
            allow_worker_hosts=["wlq@172.16.10.20"],
            worker_ssh_factory=worker_factory,
        )
        self.assertTrue(result["success"])
        self.assertTrue(result["started"])
        start_cmd = next(
            cmd for cmd in ssh_manager.calls
            if cmd.startswith("/usr/local/bin/usbipd bind")
        )
        self.assertIn("--vid 18d1", start_cmd)

    def test_vid_addition_merges_and_restarts(self):
        # P1-4 主用例：旧实例 --vid 2207，请求 --vid 18d1，必须判定为
        # 覆盖不足并合并重启，而不是错误复用。
        responses = {
            "for b in": ("/usr/local/bin/usbipd\n", "", 0),
            "/usr/local/bin/usbipd --version": ("usbipd 0.9.5", "", 0),
            "kill": ("", "", 0),
            **WORKER_EGRESS_SETUP,
        }
        ssh_manager = _fake_ssh_manager(responses)
        worker_factory, _opened = _worker_ssh_factory()
        state = _pgrep_protocol_state([
            ("7", "usbipd bind --vid 2207"),
        ])

        def execute(target, cmd, timeout=None, get_pty=False):
            outcome = _execute_with_pgrep_protocol(
                state, ssh_manager, responses
            )(target, cmd, timeout, get_pty)
            if cmd.startswith("kill "):
                state["candidates"] = []
            elif any(
                cmd_item.startswith("/usr/local/bin/usbipd bind")
                for cmd_item in ssh_manager.calls
            ) and not state["candidates"]:
                state["candidates"] = [
                    ("8", "usbipd bind --vid 2207 --vid 18d1"),
                ]
            return outcome

        ssh_manager.execute_command.side_effect = execute
        result = usbip_linux_source.ensure_ubuntu_usbip_server(
            ssh_manager, MagicMock(), serials=[], vids=["18d1"],
            allow_worker_hosts=["wlq@172.16.10.20"],
            worker_ssh_factory=worker_factory,
        )
        self.assertTrue(result["success"])
        self.assertTrue(result["started"])
        self.assertEqual(result["vids"], ["18d1", "2207"])
        start_cmd = next(
            cmd for cmd in ssh_manager.calls
            if cmd.startswith("/usr/local/bin/usbipd bind")
        )
        self.assertIn("--vid 2207", start_cmd)
        self.assertIn("--vid 18d1", start_cmd)

    def test_start_passes_listen_for_new_usbipd(self):
        # usbipd 0.9.3+ 默认只监听回环；平台启动时必须显式传
        # --listen <SSH可达IP>:3240，否则 Worker 无法 attach。
        responses = {
            "for b in": ("/usr/local/bin/usbipd\n", "", 0),
            "/usr/local/bin/usbipd --version": ("usbipd 0.9.5", "", 0),
            **WORKER_EGRESS_SETUP,
        }
        ssh_manager = _fake_ssh_manager(responses)
        worker_factory, _opened = _worker_ssh_factory()
        state = _pgrep_protocol_state([])

        def execute(target, cmd, timeout=None, get_pty=False):
            if any(
                cmd_item.startswith("/usr/local/bin/usbipd bind")
                for cmd_item in ssh_manager.calls
            ) and not state["candidates"]:
                state["candidates"] = [("77", "usbipd bind")]
            return _execute_with_pgrep_protocol(
                state, ssh_manager, responses
            )(target, cmd, timeout, get_pty)

        ssh_manager.execute_command.side_effect = execute
        result = usbip_linux_source.ensure_ubuntu_usbip_server(
            ssh_manager, MagicMock(), serials=["S1"],
            allow_worker_hosts=["wlq@172.16.10.20"],
            worker_ssh_factory=worker_factory,
        )
        self.assertTrue(result["success"])
        start_cmd = next(
            cmd for cmd in ssh_manager.calls
            if cmd.startswith("/usr/local/bin/usbipd bind")
        )
        self.assertIn("--listen 10.0.0.5:3240", start_cmd)

    def test_missing_worker_credentials_fails_closed(self):
        # P0 回归：Worker 出口 IP 无法解析（无 SSH factory / 连接失败 /
        # ip route get 失败）时，绝不能退化为无 --allow-client 的开放
        # 实例——必须 fail-closed 拒绝启动。
        base_responses = {
            "for b in": ("/usr/local/bin/usbipd\n", "", 0),
            "/usr/local/bin/usbipd --version": ("usbipd 0.9.5", "", 0),
            **WORKER_EGRESS_SETUP,
        }
        # 1) 无 factory。
        ssh_manager = _fake_ssh_manager(base_responses)
        result = usbip_linux_source.ensure_ubuntu_usbip_server(
            ssh_manager, MagicMock(), serials=["S1"],
            allow_worker_hosts=["wlq@172.16.10.20"],
        )
        self.assertFalse(result["success"])
        self.assertFalse(any(
            cmd.startswith("/usr/local/bin/usbipd bind")
            for cmd in ssh_manager.calls
        ))
        self.assertIn("fail-closed", result["error"])
        # 2) factory 返回 None（连接失败）。
        ssh_manager = _fake_ssh_manager(base_responses)
        result = usbip_linux_source.ensure_ubuntu_usbip_server(
            ssh_manager, MagicMock(), serials=["S1"],
            allow_worker_hosts=["wlq@172.16.10.20"],
            worker_ssh_factory=lambda host: None,
        )
        self.assertFalse(result["success"])
        self.assertFalse(any(
            cmd.startswith("/usr/local/bin/usbipd bind")
            for cmd in ssh_manager.calls
        ))
        # 3) ip route get 解析失败。
        no_route = dict(base_responses)
        no_route["ip route get"] = ("", "RTNETLINK answers: Network is unreachable", 1)
        ssh_manager = _fake_ssh_manager(no_route)
        worker_factory, _opened = _worker_ssh_factory()
        result = usbip_linux_source.ensure_ubuntu_usbip_server(
            ssh_manager, MagicMock(), serials=["S1"],
            allow_worker_hosts=["wlq@172.16.10.20"],
            worker_ssh_factory=worker_factory,
        )
        self.assertFalse(result["success"])
        self.assertFalse(any(
            cmd.startswith("/usr/local/bin/usbipd bind")
            for cmd in ssh_manager.calls
        ))

    def test_missing_allow_worker_hosts_fails_closed(self):
        # P0 回归：完全没有 Worker 白名单输入时同样 fail-closed，不再
        # 以"网络防火墙兜底"为由启动 allow-all 实例。
        responses = {
            "for b in": ("/usr/local/bin/usbipd\n", "", 0),
            "/usr/local/bin/usbipd --version": ("usbipd 0.9.5", "", 0),
            **WORKER_EGRESS_SETUP,
        }
        ssh_manager = _fake_ssh_manager(responses)
        result = usbip_linux_source.ensure_ubuntu_usbip_server(
            ssh_manager, MagicMock(), serials=["S1"],
        )
        self.assertFalse(result["success"])
        self.assertFalse(any(
            cmd.startswith("/usr/local/bin/usbipd bind")
            for cmd in ssh_manager.calls
        ))

    def test_start_without_ssh_connection_binds_all_interfaces_with_warning_path(self):
        # SSH_CONNECTION 不可读时退化为 0.0.0.0:3240（仍显式 --listen，
        # 不依赖 usbipd 默认值）；白名单改用 Worker 上 ip route get 解析
        # 来源地址失败 → fail-closed，而不是 allow-all。
        responses = {
            "for b in": ("/usr/local/bin/usbipd\n", "", 0),
            "/usr/local/bin/usbipd --version": ("usbipd 0.9.5", "", 0),
            "echo $SSH_CONNECTION": ("", "", 0),
        }
        ssh_manager = _fake_ssh_manager(responses)
        result = usbip_linux_source.ensure_ubuntu_usbip_server(
            ssh_manager, MagicMock(), serials=["S1"],
            allow_worker_hosts=["wlq@172.16.10.20", "172.16.10.21"],
        )
        self.assertFalse(result["success"])
        self.assertFalse(any(
            cmd.startswith("/usr/local/bin/usbipd bind")
            for cmd in ssh_manager.calls
        ))


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


class WorkerEgressResolutionTests(unittest.TestCase):
    def test_resolve_uses_worker_ssh_not_source_ssh(self):
        # P0 回归：ip route get 必须在 Worker 自己的 SSH 会话上执行，
        # 不能复用来源主机的连接（那只会解析到来源自己的路由）。
        source_responses = {
            "echo $SSH_CONNECTION": ("10.0.0.9 54321 10.0.0.5 22\n", "", 0),
        }
        ssh_manager = _fake_ssh_manager(source_responses)
        worker_ssh = MagicMock()
        worker_targets = []

        def execute(target, cmd, timeout=None, get_pty=False):
            ssh_manager.calls.append(cmd)
            # 区分来源会话与 Worker 会话：target 即传入的 ssh 对象。
            if target is worker_ssh:
                worker_targets.append(cmd)
                if cmd.startswith("ip route get"):
                    return _cr(("10.0.0.5 dev tailscale0 src 100.64.0.7\n", "", 0))
                return _cr(("", "", 0))
            for key, value in source_responses.items():
                if cmd.startswith(key) or cmd == key:
                    return _cr(value)
            return _cr(("", "", 0))

        ssh_manager.execute_command.side_effect = execute
        resolved = usbip_linux_source.resolve_worker_egress_ips(
            ssh_manager, MagicMock(), ["wlq@172.16.10.20"],
            worker_ssh_factory=lambda host: worker_ssh,
        )
        self.assertEqual(resolved, ["100.64.0.7"])
        # ip route get 只能出现在 Worker 会话上。
        self.assertTrue(any("ip route get" in cmd for cmd in worker_targets))

    def test_resolve_returns_empty_without_factory(self):
        ssh_manager = _fake_ssh_manager({
            "echo $SSH_CONNECTION": ("10.0.0.9 54321 10.0.0.5 22\n", "", 0),
        })
        resolved = usbip_linux_source.resolve_worker_egress_ips(
            ssh_manager, MagicMock(), ["wlq@172.16.10.20"],
        )
        self.assertEqual(resolved, [])

    def test_resolve_closes_worker_connection(self):
        ssh_manager = _fake_ssh_manager({
            "echo $SSH_CONNECTION": ("10.0.0.9 54321 10.0.0.5 22\n", "", 0),
            "ip route get": ("10.0.0.5 dev eth0 src 172.16.10.20\n", "", 0),
        })
        worker_ssh = MagicMock()
        usbip_linux_source.resolve_worker_egress_ips(
            ssh_manager, MagicMock(), ["wlq@172.16.10.20"],
            worker_ssh_factory=lambda host: worker_ssh,
        )
        worker_ssh.close.assert_called_once()


class AutoBindUbuntuTests(unittest.TestCase):
    def test_ensure_ubuntu_export_uses_assignment_serials(self):
        responses = {
            "for b in": ("/usr/local/bin/usbipd\n", "", 0),
            "/usr/local/bin/usbipd --version": ("usbipd 0.9.5", "", 0),
            **WORKER_EGRESS_SETUP,
        }
        ssh_manager = _fake_ssh_manager(responses)
        state = _pgrep_protocol_state([
            ("7", "usbipd bind --stop-adb --serial RKTEST123"),
        ])
        ssh_manager.execute_command.side_effect = _execute_with_pgrep_protocol(
            state, ssh_manager, responses,
        )
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
