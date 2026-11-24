"""本地 adb 健康探测：常驻进程不得让设备枚举短路为空列表。"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from features.devices.manager import _blocked_adb_from_ps, has_blocked_adb_process


def _ps(*rows: str) -> str:
    header = "STAT     ELAPSED COMMAND"
    return "\n".join((header, *rows))


class BlockedAdbFromPsTests(unittest.TestCase):
    def test_resident_adb_clients_are_healthy(self):
        # 现场形态：4 个设备监听 + server 守护进程 + 一个 logcat。
        output = _ps(
            "Ssl   266574 adb -L tcp:5037 fork-server server --reply-fd 4",
            "S      11568 adb track-devices --proto-text",
            "S      10531 adb track-devices --proto-text",
            "S+       986 adb -s 66df75a34ebde4a4 shell logcat -b all -v threadtime,uid",
        )
        self.assertFalse(_blocked_adb_from_ps(output))

    def test_d_state_adb_is_blocked(self):
        output = _ps("D        120 adb devices")
        self.assertTrue(_blocked_adb_from_ps(output))

    def test_long_running_one_shot_queries_are_blocked(self):
        output = _ps(
            "S         42 adb devices",
            "S         37 /usr/bin/adb devices",
        )
        self.assertTrue(_blocked_adb_from_ps(output))

    def test_single_transient_query_is_not_blocked(self):
        output = _ps("S          1 adb devices")
        self.assertFalse(_blocked_adb_from_ps(output))

    def test_per_device_shell_commands_are_ignored(self):
        output = _ps(
            "S          1 adb -s RK3326GMS1 shell getprop ro.build.version.release",
            "S         90 adb -s RK3566EVB2GMS01 devices",
        )
        self.assertFalse(_blocked_adb_from_ps(output))

    def test_non_adb_processes_are_ignored(self):
        output = _ps("D          9 /opt/tools/adb_helper devices", "S 5 bash -c ps")
        self.assertFalse(_blocked_adb_from_ps(output))

    def test_has_blocked_adb_process_reads_ps(self):
        healthy = SimpleNamespace(
            stdout=_ps("S 11568 adb track-devices --proto-text"),
            stderr="",
            returncode=0,
        )
        with patch("features.devices.manager.subprocess.run", return_value=healthy):
            self.assertFalse(has_blocked_adb_process())

        blocked = SimpleNamespace(stdout=_ps("D 120 adb devices"), stderr="", returncode=0)
        with patch("features.devices.manager.subprocess.run", return_value=blocked):
            self.assertTrue(has_blocked_adb_process())

    def test_ps_failure_degrades_to_healthy(self):
        with patch("features.devices.manager.subprocess.run", side_effect=OSError):
            self.assertFalse(has_blocked_adb_process())


if __name__ == "__main__":
    unittest.main()
