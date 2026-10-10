from __future__ import annotations

import types
import unittest
from unittest.mock import patch

from foundation.command_result import CommandResult


class _StubSSHManager:
    def __init__(self, results):
        self._results = list(results)
        self.commands = []

    def execute_command(self, ssh, command, timeout=30, get_pty=False):
        self.commands.append(command)
        return self._results.pop(0)


class WaitForAdbSerialReadyTests(unittest.TestCase):
    def test_returns_ready_when_adb_reports_device(self):
        import features.devices.usbip as usbip_module

        stub = _StubSSHManager([
            CommandResult(),
            CommandResult(stdout="device\n", code=0),
            CommandResult(stdout="ready\n", code=0),
        ])
        manager = types.SimpleNamespace(ssh_manager=stub)
        with patch.object(usbip_module, "usbip_manager", manager):
            result = usbip_module.wait_for_adb_serial_ready(object(), "RK3562GMS7", timeout=10)

        self.assertEqual(result, {"ready": True})
        self.assertEqual(stub.commands[1], "adb -s RK3562GMS7 get-state")

    def test_reports_state_and_devices_when_not_ready(self):
        import features.devices.usbip as usbip_module

        stub = _StubSSHManager([
            CommandResult(),
            CommandResult(stdout="offline\n", code=1),
        ])
        manager = types.SimpleNamespace(ssh_manager=stub)
        with patch.object(usbip_module, "usbip_manager", manager):
            result = usbip_module.wait_for_adb_serial_ready(object(), "RK3562GMS7", timeout=0)

        self.assertFalse(result["ready"])
        self.assertEqual(result["state"], "")
        self.assertEqual(result["devices"], "offline")


if __name__ == "__main__":
    unittest.main()
