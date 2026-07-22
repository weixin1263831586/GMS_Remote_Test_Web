import unittest
from unittest.mock import Mock

from features.devices.utils import DeviceUtils


class DeviceUtilsTests(unittest.TestCase):
    def test_scrcpy_helpers_reject_invalid_device_id(self):
        with self.assertRaises(ValueError):
            DeviceUtils.scrcpy_log_path("SERIAL;rm -rf /")

        with self.assertRaises(ValueError):
            DeviceUtils.scrcpy_process_pattern("bad id")

    def test_build_scrcpy_command_quotes_paths_and_device(self):
        command = DeviceUtils.build_scrcpy_command(
            scrcpy_path="/opt/scrcpy bin/scrcpy",
            device_id="ABC-123",
            ubuntu_user="test user",
            x_offset=1,
            y_offset=2,
            window_width=300,
            window_height=400,
            use_gdm_xauthority_fallback=True,
        )

        self.assertIn("nohup '/opt/scrcpy bin/scrcpy' -s ABC-123", command)
        self.assertIn("--no-control", command)
        self.assertNotIn("--stay-awake", command)
        self.assertIn("--window-title ABC-123", command)
        self.assertIn("> /tmp/scrcpy_ABC-123.log 2>&1 &", command)
        self.assertIn("/home/'test user'/.Xauthority", command)

    def test_kill_process_uses_safe_pkill_pattern(self):
        ssh = Mock()

        self.assertTrue(DeviceUtils.kill_process(ssh, "scrcpy.*-s ABC-123"))

        ssh.exec_command.assert_called_once_with("pkill -f -- 'scrcpy.*-s ABC-123'")

    def test_check_scrcpy_healthy_uses_quoted_pattern_and_log_path(self):
        stdout = Mock()
        stdout.read.return_value = b"1234\n"
        ssh = Mock()
        ssh.exec_command.return_value = (stdout, None, None)

        healthy, pid = DeviceUtils.check_scrcpy_healthy(ssh, "ABC-123")

        self.assertTrue(healthy)
        self.assertEqual(pid, "1234")
        command = ssh.exec_command.call_args.args[0]
        self.assertIn("pgrep -f -- 'scrcpy.*-s ABC-123'", command)
        self.assertIn("tail -c 2048 /tmp/scrcpy_ABC-123.log", command)


if __name__ == "__main__":
    unittest.main()
