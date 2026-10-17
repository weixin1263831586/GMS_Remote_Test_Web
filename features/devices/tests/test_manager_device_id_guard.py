"""Device ids must pass the whitelist before entering adb command strings."""

import unittest
from unittest.mock import MagicMock

from features.devices.manager import DeviceManager


class DeviceIdGuardTests(unittest.TestCase):
    def test_unsafe_ids_are_rejected_before_any_shell_command(self):
        manager = DeviceManager()
        for unsafe in ("SERIAL;rm -rf", "$(id)", "SERIAL`x`", "SERIAL$(x)", "with space", "", "SERIAL\nX"):
            with self.subTest(unsafe=repr(unsafe)):
                with self.assertRaises(ValueError):
                    manager.get_device_info(unsafe)
                with self.assertRaises(ValueError):
                    manager.reboot_device(unsafe)
                with self.assertRaises(ValueError):
                    manager.remount_device(unsafe)

    def test_safe_ids_reach_the_ssh_channel(self):
        ssh_manager = MagicMock()
        manager = DeviceManager(ssh_manager=ssh_manager, config_manager=MagicMock(load_config=lambda: {}))
        ssh = MagicMock()
        manager.get_device_info("R58M1234567", ssh=ssh)
        manager.remount_device("R58M1234567", ssh=ssh)
        manager.reboot_device("R58M1234567", ssh=ssh, wait_for_online=False)
        commands = [call.args[1] for call in ssh_manager.execute_command.call_args_list]
        self.assertTrue(commands)
        self.assertTrue(all(command.split()[2] == "R58M1234567" for command in commands if command.startswith("adb -s ")))


if __name__ == "__main__":
    unittest.main()
