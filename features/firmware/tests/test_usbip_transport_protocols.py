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
        """
        probes = iter([
            "350a\tNEW-SOC-DEV\tUSB download gadget\n",
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


if __name__ == "__main__":
    unittest.main()
