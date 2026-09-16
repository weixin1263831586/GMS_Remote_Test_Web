from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from features.firmware import runtime as firmware_runtime
from features.firmware.usbip_transport import (
    device_flash_protocols,
    partition_devices_by_flash_state,
    wait_for_rockusb_loader_exit,
    wait_for_single_rockusb_loader,
)
from foundation.command_result import CommandResult
from foundation.error_model import ApiError


PROBE_OUTPUT = (
    "351a\tRK3562GMS7\tUSB download gadget\n"
    "0006\tHEALTHY-0006\tSSI 17 on ARM64\n"
    "0007\tHEALTHY-0007\tSSI 17 Go on ARM64\n"
    "011d\tIN-ADB\tAndroid Device\n"
)


class FlashProtocolTests(unittest.TestCase):
    def _protocols(self):
        def execute_command(_ssh, cmd, timeout=None):
            if cmd.startswith("adb devices"):
                return CommandResult(
                    stdout="List of devices attached\nADB-DEV\tdevice\nBAD-ADB\tunauthorized\n",
                    stderr="", code=0,
                )
            if cmd.startswith("fastboot devices"):
                return CommandResult(
                    stdout="FB-DEV\tfastboot\n", stderr="", code=0,
                )
            return CommandResult(stdout=PROBE_OUTPUT, stderr="", code=0)

        ssh_manager = SimpleNamespace(execute_command=execute_command)
        with patch.object(firmware_runtime, "ssh_manager", ssh_manager):
            return device_flash_protocols(object(), [
                "ADB-DEV", "BAD-ADB", "FB-DEV", "RK3562GMS7", "MISSING",
                "HEALTHY-0006", "HEALTHY-0007",
            ])

    def test_loader_device_gets_rockusb_protocol(self):
        protocols = self._protocols()
        self.assertEqual(protocols["ADB-DEV"], "adb")
        self.assertEqual(protocols["FB-DEV"], "fastboot")
        self.assertEqual(protocols["RK3562GMS7"], "rockusb-loader")
        # 健康功能枚举（2207:0006/0007）不在烧写模式 PID 集合内，
        # 即使从未出现在 adb 枚举中也不得误判为 Loader。
        self.assertEqual(protocols["HEALTHY-0006"], "")
        self.assertEqual(protocols["HEALTHY-0007"], "")
        self.assertEqual(protocols["BAD-ADB"], "")
        self.assertEqual(protocols["MISSING"], "")

    def test_partition_keeps_loader_out_of_gsi_ready_set(self):
        protocols = self._protocols()
        with patch(
            "features.firmware.usbip_transport.device_flash_protocols",
            return_value=protocols,
        ):
            ready, offline = partition_devices_by_flash_state(
                object(), ["ADB-DEV", "FB-DEV", "RK3562GMS7"]
            )
        self.assertEqual(ready, ["ADB-DEV", "FB-DEV"])
        self.assertEqual(offline, ["RK3562GMS7"])

    def test_wait_for_single_loader_rejects_ambiguous_selection(self):
        probes = iter([
            "351a\tRK3562GMS7\tUSB download gadget\n"
            "351a\tOTHER\tUSB download gadget\n",
            "351a\tRK3562GMS7\tUSB download gadget\n",
        ])

        def execute_command(_ssh, cmd, timeout=None):
            if cmd.startswith("adb devices"):
                return CommandResult(stdout="List of devices attached\n", code=0)
            return CommandResult(stdout=next(probes), code=0)

        ssh_manager = SimpleNamespace(execute_command=execute_command)
        with patch.object(firmware_runtime, "ssh_manager", ssh_manager):
            ready, _detail = asyncio.run(
                wait_for_single_rockusb_loader(
                    object(), "RK3562GMS7", timeout=1, interval=0,
                )
            )
        self.assertTrue(ready)

    def test_wait_for_loader_exit_does_not_require_adb(self):
        probes = iter([
            "351a\tRK3562GMS7\tUSB download gadget\n",
            "",
        ])

        def execute_command(_ssh, cmd, timeout=None):
            if cmd.startswith("adb devices"):
                return CommandResult(stdout="List of devices attached\n", code=0)
            return CommandResult(stdout=next(probes), code=0)

        ssh_manager = SimpleNamespace(execute_command=execute_command)
        with patch.object(firmware_runtime, "ssh_manager", ssh_manager):
            exited, _detail = asyncio.run(
                wait_for_rockusb_loader_exit(
                    object(), "RK3562GMS7", timeout=1, interval=0,
                )
            )
        self.assertTrue(exited)

    def test_config_only_loader_pid_is_honored_by_flash_gate(self):
        """新增 SoC 的 Loader PID 只需写进 configs usbip_vid_pids。

        回归背景（2026-09-15）：RK3576（2207:350e）烧写时烧写门只认内置
        默认 PID（351a），配置里的新 PID 不生效，导致"未能确认目标设备
        是唯一 Loader"。烧写门必须与 manager 一样读运行时配置。
        产品名不带烧写标记（个别 SoC 的 iProduct 读取为空/自定义），
        该用例专门检验 PID 配置路径。
        """
        probes = iter([
            "350a\tNEW-SOC-DEV\tNew SoC Loader\n",
        ])

        def execute_command(_ssh, cmd, timeout=None):
            if cmd.startswith("adb devices"):
                return CommandResult(stdout="List of devices attached\n", code=0)
            return CommandResult(stdout=next(probes), code=0)

        ssh_manager = SimpleNamespace(execute_command=execute_command)
        config_manager = SimpleNamespace(load_config=lambda: {
            "usbip_vid_pids": ["2207:0006", "18d1:4d00", "2207:350a"],
        })
        with patch.object(firmware_runtime, "ssh_manager", ssh_manager), \
                patch.object(firmware_runtime, "config_manager", config_manager):
            ready, _detail = asyncio.run(
                wait_for_single_rockusb_loader(
                    object(), "NEW-SOC-DEV", timeout=1, interval=0,
                )
            )
        self.assertTrue(ready)

    def test_unknown_loader_pid_recognized_by_product_marker(self):
        """未知 SoC 的 Loader 靠 BootROM 产品名标记兜底，无需补 PID。

        回归背景（2026-09-16）：RK3562GMS1（Loader PID 不在默认清单
        351a/350e 中，也未写入部署配置）烧写时反复超时并误报
        "未能确认目标设备是唯一 Loader"。标记层让新 SoC 开箱即用。
        """
        probes = iter([
            "0006\tHEALTHY-0006\tSSI 17 on ARM64\n"
            "350f\tRK3562GMS1\tUSB download gadget\n",
        ])

        def execute_command(_ssh, cmd, timeout=None):
            if cmd.startswith("adb devices"):
                return CommandResult(stdout="List of devices attached\n", code=0)
            return CommandResult(stdout=next(probes), code=0)

        ssh_manager = SimpleNamespace(execute_command=execute_command)
        config_manager = SimpleNamespace(load_config=lambda: {
            "usbip_vid_pids": ["2207:0006", "18d1:4d00", "2207:351a", "2207:350e"],
        })
        with patch.object(firmware_runtime, "ssh_manager", ssh_manager), \
                patch.object(firmware_runtime, "config_manager", config_manager):
            ready, _detail = asyncio.run(
                wait_for_single_rockusb_loader(
                    object(), "RK3562GMS1", timeout=1, interval=0,
                )
            )
        self.assertTrue(ready)

    def test_gate_fails_closed_when_worker_probe_errors(self):
        """Worker 探测失败时烧写门必须保持关闭（fail-closed）。

        回归背景：sysfs/adb 探测返回非零（sshd 抖动、adb server 故障）
        曾被当作"无 Loader"继续判定——空排除集会把健康的 marker 命中
        设备暴露成唯一 Loader，打开错烧路径。
        """
        def execute_command(_ssh, cmd, timeout=None):
            if cmd.startswith("adb devices"):
                return CommandResult(stdout="", stderr="adb: failed", code=1)
            return CommandResult(stdout="", code=0)

        ssh_manager = SimpleNamespace(execute_command=execute_command)
        with patch.object(firmware_runtime, "ssh_manager", ssh_manager):
            ready, _detail = asyncio.run(
                wait_for_single_rockusb_loader(
                    object(), "ANY-DEV", timeout=1, interval=0,
                )
            )
        self.assertFalse(ready)

    def test_gate_rejects_ambiguous_multi_loader_state(self):
        """第二块板同时处于 Loader 时必须拒绝，不得赌目标板被选中。"""
        def execute_command(_ssh, cmd, timeout=None):
            if cmd.startswith("adb devices"):
                return CommandResult(stdout="List of devices attached\n", code=0)
            return CommandResult(
                stdout=(
                    "351a\tTARGET-DEV\tUSB download gadget\n"
                    "350f\tOTHER-DEV\tUSB download gadget\n"
                ),
                code=0,
            )

        ssh_manager = SimpleNamespace(execute_command=execute_command)
        with patch.object(firmware_runtime, "ssh_manager", ssh_manager):
            ready, _detail = asyncio.run(
                wait_for_single_rockusb_loader(
                    object(), "TARGET-DEV", timeout=1, interval=0,
                )
            )
        self.assertFalse(ready)

    def test_loader_exit_never_fakes_success_on_probe_error(self):
        """VERIFY 阶段探测失败不得被当作"设备已离开 Loader"的假成功。"""
        def execute_command(_ssh, cmd, timeout=None):
            if cmd.startswith("adb devices"):
                return CommandResult(stdout="List of devices attached\n", code=0)
            return CommandResult(stdout="", stderr="probe failed", code=1)

        ssh_manager = SimpleNamespace(execute_command=execute_command)
        with patch.object(firmware_runtime, "ssh_manager", ssh_manager):
            exited, _detail = asyncio.run(
                wait_for_rockusb_loader_exit(
                    object(), "TARGET-DEV", timeout=1, interval=0,
                )
            )
        self.assertFalse(exited)

    def test_device_flash_protocols_maps_probe_failure_to_502(self):
        """协议探测失败映射 UPSTREAM_FAILURE(502)，不得落回 500。"""
        def execute_command(_ssh, cmd, timeout=None):
            if cmd.startswith("adb devices"):
                return CommandResult(stdout="List of devices attached\n", code=0)
            return CommandResult(stdout="", stderr="fastboot: not found", code=127)

        ssh_manager = SimpleNamespace(execute_command=execute_command)
        with patch.object(firmware_runtime, "ssh_manager", ssh_manager), self.assertRaises(ApiError) as ctx:
            device_flash_protocols(object(), ["D1"])
        self.assertEqual(ctx.exception.status_code, 502)


if __name__ == "__main__":
    unittest.main()
