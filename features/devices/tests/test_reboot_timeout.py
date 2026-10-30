"""A failed reboot command cannot be confirmed by another failed command."""
import unittest
from unittest.mock import patch

from features.devices.adb_ops import reboot_with_runner


class StubRunner:
    def __init__(self, results):
        self.results = dict(results)
        self.calls = []

    def __call__(self, device_id, args, timeout):
        self.calls.append(args)
        return self.results[args.split()[0]]


class RebootChannelDropTests(unittest.TestCase):
    TIMEOUT_MSG = "SSH command timed out after 5 seconds"

    def test_channel_drop_with_device_gone_remains_unconfirmed(self):
        runner = StubRunner({
            "reboot": (self.TIMEOUT_MSG, 255),
            "get-state": ("error: device offline", 1),
        })
        result = reboot_with_runner(runner, "serial", wait_for_online=False)
        self.assertFalse(result.success)
        self.assertEqual(result.output, self.TIMEOUT_MSG)
        self.assertEqual(runner.calls, ["reboot"])

    def test_channel_drop_with_device_still_online_is_failure(self):
        runner = StubRunner({
            "reboot": (self.TIMEOUT_MSG, 255),
            "get-state": ("device", 0),
        })
        result = reboot_with_runner(runner, "serial", wait_for_online=False)
        self.assertFalse(result.success)
        self.assertEqual(result.output, self.TIMEOUT_MSG)

    def test_clean_reboot_unaffected(self):
        runner = StubRunner({"reboot": ("", 0)})
        result = reboot_with_runner(runner, "serial", wait_for_online=False)
        self.assertTrue(result.success)
        self.assertNotIn("reboot issued", result.output)

    def test_infrastructure_and_device_failures_never_report_reboot_success(self):
        for output, code in [
            ("SSH authentication failed", 255),
            ("SSH connection refused", 255),
            ("adb: command not found", 127),
            ("error: device unauthorized", 1),
            ("error: device offline", 1),
            (self.TIMEOUT_MSG, -1),
        ]:
            for wait_for_online in (False, True):
                with self.subTest(output=output, wait_for_online=wait_for_online):
                    runner = StubRunner({"reboot": (output, code), "get-state": (output, code)})
                    result = reboot_with_runner(runner, "serial", wait_for_online=wait_for_online)
                    self.assertFalse(result.success)
                    self.assertFalse(result.back_online)
                    self.assertEqual(result.output, output)
                    self.assertEqual(runner.calls, ["reboot"])

    def test_failed_get_state_cannot_confirm_online_even_with_device_text(self):
        runner = StubRunner({"reboot": ("", 0), "get-state": ("device", 255)})
        with patch("features.devices.adb_ops.time.time", side_effect=[0, 0, 1]), \
                patch("features.devices.adb_ops.time.sleep"):
            result = reboot_with_runner(runner, "serial", wait_for_online=True, wait_timeout=0.5)
        self.assertTrue(result.success)
        self.assertFalse(result.back_online)


if __name__ == "__main__":
    unittest.main()
