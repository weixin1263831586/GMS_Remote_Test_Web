import unittest
from unittest.mock import patch

from features.test_execution.runner import TestRunner as GmsTestRunner


class TestRunnerCommandTests(unittest.TestCase):
    def test_build_test_command_quotes_script_directory(self):
        runner = GmsTestRunner()

        with patch("features.test_execution.suites.runtime.config_manager") as config_manager:
            config_manager.get_ubuntu_user.return_value = "test user"
            command = runner._build_test_command(
                {
                    "test_type": "cts",
                    "devices": ["SERIAL01"],
                    "test_suite": "/suite path/android-cts/tools",
                    "local_server": "host@127.0.0.1",
                },
                {"suites_path": "/home/test user/GMS-Suite"},
                "pgid",
                lambda *_args, **_kwargs: None,
            )

        self.assertIsNotNone(command)
        self.assertTrue(command.startswith("cd '/home/test user/GMS-Suite' && "))
        self.assertIn("'/home/test user/GMS-Suite/run_GMS_Test_Auto.sh'", command)
        self.assertIn("--test-suite '/suite path/android-cts/tools'", command)


if __name__ == "__main__":
    unittest.main()
