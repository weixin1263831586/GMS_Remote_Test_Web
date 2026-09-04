import tempfile
import unittest
from pathlib import Path

from features.test_execution import tradefed_results
from foundation.command_result import CommandResult


class TradefedResultCacheTests(unittest.TestCase):
    def setUp(self):
        tradefed_results._result_cache.clear()

    def tearDown(self):
        tradefed_results._result_cache.clear()

    def test_remote_cache_key_is_scoped_to_host_and_user(self):
        first = tradefed_results._build_cache_key(
            {"ubuntu_user": "one", "ubuntu_host": "worker-a"},
            "/srv/suite/tools",
            False,
        )
        second = tradefed_results._build_cache_key(
            {"ubuntu_user": "two", "ubuntu_host": "worker-b"},
            "/srv/suite/tools",
            False,
        )

        self.assertNotEqual(first, second)

    def test_cached_payload_is_isolated_from_callers(self):
        payload = {"success": True, "results": [{"pass": 1}], "cached": False}
        tradefed_results._cache_put("key", payload)

        first = tradefed_results._cache_get("key")
        first["results"][0]["pass"] = 99
        second = tradefed_results._cache_get("key")

        self.assertEqual(second["results"][0]["pass"], 1)
        self.assertTrue(second["cached"])
        self.assertFalse(payload["cached"])

    def test_local_cache_key_changes_when_latest_xml_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            suite = Path(directory) / "android-cts/tools"
            result = Path(directory) / "android-cts/results/2026.08.10_10.00.00"
            suite.mkdir(parents=True)
            result.mkdir(parents=True)
            xml = result / "test_result.xml"
            xml.write_text("first", encoding="utf-8")
            config = {"ubuntu_host": "127.0.0.1", "ubuntu_user": "tester"}
            first = tradefed_results._build_cache_key(config, str(suite), True)

            xml.write_text("second-version", encoding="utf-8")
            second = tradefed_results._build_cache_key(config, str(suite), True)

        self.assertNotEqual(first, second)


class TradefedResultEnrichmentTests(unittest.TestCase):
    def test_remote_paths_are_shell_quoted_and_unsafe_result_names_are_skipped(self):
        calls = []

        class Manager:
            @staticmethod
            def execute_command(_ssh, command, timeout):
                calls.append((command, timeout))
                return CommandResult(stdout="", stderr="", code=0)

        original = tradefed_results.runtime.ssh_manager
        tradefed_results.runtime.ssh_manager = Manager()
        try:
            tradefed_results._enrich_remote_results(
                [
                    {"result_directory": "2026.08.10_10.00.00"},
                    {"result_directory": "../injected"},
                ],
                object(),
                "/srv/GMS Suite/android-cts/tools",
            )
        finally:
            tradefed_results.runtime.ssh_manager = original

        command, timeout = calls[0]
        self.assertIn(
            "'/srv/GMS Suite/android-cts/results/2026.08.10_10.00.00'",
            command,
        )
        self.assertNotIn("injected", command)
        self.assertEqual(timeout, 30)


if __name__ == "__main__":
    unittest.main()
