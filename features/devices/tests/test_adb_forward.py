import unittest
from unittest.mock import patch

from features.devices.adb_forward import ADBForwardManager, _adb_tunnel_kill_command


class ADBForwardManagerTests(unittest.TestCase):
    def test_tunnel_cleanup_does_not_kill_all_adb_processes(self):
        command = _adb_tunnel_kill_command()

        self.assertIn("pkill -f", command)
        self.assertIn("5037:localhost:5037", command)
        self.assertNotIn("pkill -f adb", command)
        self.assertNotIn("adb.*forward", command)

    def test_start_forward_uses_scoped_cleanup_and_quoted_host(self):
        commands = []

        class FakeConfigManager:
            def load_config(self):
                return {"ubuntu_host": "127.0.0.1"}

        class FakeSshManager:
            def get_connection(self, _config):
                return object()

            def execute_command(self, _ssh, cmd, timeout=None):
                commands.append(cmd)
                if cmd == "adb devices":
                    return "List of devices attached\nSERIAL01\tdevice\n", "", 0
                return "", "", 0

            def return_connection(self, _ssh):
                commands.append("__returned__")

        manager = ADBForwardManager(
            ssh_manager=FakeSshManager(),
            config_manager=FakeConfigManager(),
        )

        with patch("features.devices.adb_forward.time.sleep"):
            result = manager.start_forward("user@192.168.0.2", "pw with spaces")

        self.assertTrue(result["success"])
        self.assertEqual(commands[0], _adb_tunnel_kill_command())
        self.assertTrue(any("-o ExitOnForwardFailure=yes" in cmd for cmd in commands))
        self.assertFalse(any("pkill -f adb" in cmd for cmd in commands))
        self.assertEqual(commands[-1], "__returned__")


if __name__ == "__main__":
    unittest.main()
