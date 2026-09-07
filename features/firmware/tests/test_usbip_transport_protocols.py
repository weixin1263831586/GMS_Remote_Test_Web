from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from features.firmware import runtime as firmware_runtime
from features.firmware.usbip_transport import (
    device_flash_protocols,
    partition_devices_by_flash_state,
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


if __name__ == "__main__":
    unittest.main()
