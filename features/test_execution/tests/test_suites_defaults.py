import unittest
from unittest.mock import patch

from features.test_execution.suites import get_default_suites_path


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


if __name__ == "__main__":
    unittest.main()
