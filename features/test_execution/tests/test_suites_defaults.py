import os
import tempfile
import unittest
from unittest.mock import patch

from features.test_execution.suites import get_default_suites_path, list_local_test_suites


class SuiteDefaultsTests(unittest.TestCase):
    def test_configured_suites_path_wins(self):
        self.assertEqual(
            get_default_suites_path({"suites_path": "/mnt/suites", "ubuntu_user": "ignored"}),
            "/mnt/suites",
        )

    def test_default_suites_path_uses_config_manager_user(self):
        class FakeConfigManager:
            def get_ubuntu_user(self, config):
                return config.get("ubuntu_user") or "gms"

        with patch("features.test_execution.suites.runtime.config_manager", FakeConfigManager()):
            self.assertEqual(get_default_suites_path({"ubuntu_user": "tester"}), "/home/tester/GMS-Suite")

    def test_local_suite_discovery_expands_home_path(self):
        with tempfile.TemporaryDirectory() as root:
            suite_tools = os.path.join(root, "android-cts-17_r1", "android-cts", "tools")
            os.makedirs(suite_tools)
            launcher = os.path.join(suite_tools, "cts-tradefed")
            with open(launcher, "w", encoding="utf-8") as handle:
                handle.write("#!/bin/sh\n")
            os.chmod(launcher, 0o755)
            with patch("features.test_execution.suites.os.path.expanduser", return_value=root):
                suites = list_local_test_suites("~/GMS-Suite")
            self.assertEqual(len(suites), 1)
            self.assertEqual(suites[0]["test_type"], "cts")


if __name__ == "__main__":
    unittest.main()
