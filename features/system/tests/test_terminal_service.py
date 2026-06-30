import unittest
from itertools import count
from unittest.mock import patch

from features.system.terminal_service import LocalPtyChannel


class LocalPtyChannelTests(unittest.TestCase):
    def test_close_reaps_child_during_grace_period(self):
        channel = object.__new__(LocalPtyChannel)
        channel.pid = 12345
        channel.fd = 99
        channel.closed = False
        channel._reaped = False

        wait_results = [(0, 0), (12345, 15)]

        def fake_waitpid(_pid, options):
            return wait_results.pop(0)

        with (
            patch("features.system.terminal_service.os.close"),
            patch("features.system.terminal_service.os.kill") as kill,
            patch("features.system.terminal_service.os.waitpid", side_effect=fake_waitpid) as waitpid,
            patch("features.system.terminal_service.time.sleep"),
        ):
            channel.close()

        self.assertTrue(channel.closed)
        self.assertTrue(channel._reaped)
        kill.assert_called()
        self.assertEqual(waitpid.call_args_list[-1].args, (12345, 1))

    def test_close_uses_blocking_reap_after_kill(self):
        channel = object.__new__(LocalPtyChannel)
        channel.pid = 12345
        channel.fd = 99
        channel.closed = False
        channel._reaped = False

        wait_results = [(0, 0), (0, 0), (0, 0), (12345, 9)]
        monotonic_results = count()

        def fake_waitpid(_pid, options):
            return wait_results.pop(0)

        with (
            patch("features.system.terminal_service.os.close"),
            patch("features.system.terminal_service.os.kill"),
            patch("features.system.terminal_service.os.waitpid", side_effect=fake_waitpid) as waitpid,
            patch("features.system.terminal_service.time.monotonic", side_effect=monotonic_results),
            patch("features.system.terminal_service.time.sleep"),
        ):
            channel.close()

        self.assertTrue(channel._reaped)
        self.assertEqual(waitpid.call_args_list[-1].args, (12345, 0))


if __name__ == "__main__":
    unittest.main()
