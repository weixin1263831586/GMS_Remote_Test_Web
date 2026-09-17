"""foundation.adb_binary 的钉死解析行为。"""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from foundation.adb_binary import adb_binary


class AdbBinaryResolutionTest(unittest.TestCase):
    def test_pinned_path_wins_over_path_lookup(self):
        with patch.dict(
            os.environ, {"GMS_ADB_PATH": "/bin/true"}, clear=False
        ), patch("foundation.adb_binary.shutil.which") as which:
            which.return_value = "/opt/other/adb"
            self.assertEqual(adb_binary(), "/bin/true")
        which.assert_not_called()

    def test_broken_pin_fails_loudly_instead_of_falling_back(self):
        with patch.dict(
            os.environ, {"GMS_ADB_PATH": "/nonexistent/adb"}, clear=False
        ), self.assertRaises(RuntimeError) as ctx:
            adb_binary()
        self.assertIn("GMS_ADB_PATH", str(ctx.exception))

    def test_falls_back_to_path_when_pin_absent(self):
        env = {key: value for key, value in os.environ.items() if key != "GMS_ADB_PATH"}
        with patch.dict(os.environ, env, clear=True), patch(
            "foundation.adb_binary.shutil.which", return_value="/usr/bin/adb"
        ) as which:
            self.assertEqual(adb_binary(), "/usr/bin/adb")
        which.assert_called_once_with("adb")

    def test_missing_binary_raises_runtime_error(self):
        env = {key: value for key, value in os.environ.items() if key != "GMS_ADB_PATH"}
        with patch.dict(os.environ, env, clear=True), patch(
            "foundation.adb_binary.shutil.which", return_value=None
        ), self.assertRaises(RuntimeError) as ctx:
            adb_binary()
        self.assertIn("adb not found", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
