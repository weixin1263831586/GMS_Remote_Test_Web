import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from features.test_execution.suite_modules import search_latest_suite_modules


class SuiteModuleSearchTests(unittest.TestCase):
    def _make_suite(self, root: Path, dirname: str, inner: str, tradefed: str, modules: list[str]) -> Path:
        suite_root = root / dirname / inner
        tools = suite_root / "tools"
        testcases = suite_root / "testcases"
        tools.mkdir(parents=True)
        testcases.mkdir(parents=True)
        launcher = tools / tradefed
        launcher.write_text("#!/bin/sh\n", encoding="utf-8")
        launcher.chmod(launcher.stat().st_mode | 0o111)
        for module in modules:
            (testcases / module).write_text("", encoding="utf-8")
        return tools

    def test_search_latest_suite_modules_matches_camera_modules_by_suite_type(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            self._make_suite(base, "android-cts-16_r1", "android-cts", "cts-tradefed", ["CtsOldCameraTestCases.apk"])
            latest_cts = self._make_suite(base, "android-cts-17_r1", "android-cts", "cts-tradefed", ["CtsCameraTestCases.apk", "CtsWifiTestCases.apk"])
            self._make_suite(base, "android-vts-17_r1", "android-vts", "vts-tradefed", ["VtsHalCameraProvider_TargetTest.apk"])
            self._make_suite(base, "android-gts-17_r1", "android-gts", "gts-tradefed", ["GtsCameraTestCases.apk"])

            future = time.time() + 100
            os.utime(latest_cts.parent, (future, future))

            config = {"suites_path": str(base), "ubuntu_host": "127.0.0.1"}
            with patch("features.test_execution.suite_helpers.is_config_host_local", return_value=True), \
                    patch("features.test_execution.suite_modules.is_config_host_local", return_value=True):
                payload = search_latest_suite_modules(config, "Camera", ["cts", "vts", "gts", "sts"], per_suite_limit=10)

        modules = {(item["suite_type"], item["module"]) for item in payload["modules"]}
        self.assertIn(("CTS", "CtsCameraTestCases"), modules)
        self.assertIn(("VTS", "VtsHalCameraProvider_TargetTest"), modules)
        self.assertIn(("GTS", "GtsCameraTestCases"), modules)
        self.assertNotIn(("CTS", "CtsOldCameraTestCases"), modules)
        self.assertEqual(payload["normalized_query"], "Camera")
        self.assertEqual(len(payload["modules"]), len(modules))


if __name__ == "__main__":
    unittest.main()
