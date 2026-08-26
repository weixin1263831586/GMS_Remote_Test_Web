"""Short suite-name resolution for parse-args and suites-result."""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from features.test_execution import parse_api, transfers_api
from features.test_execution.models import (
    TestParseArgsRequest as ParseArgsRequest,
)
from features.test_execution.models import (
    TradefedListResultsRequest,
)
from features.test_execution.suite_helpers import resolve_suite_reference
from features.test_execution.suites import list_local_test_suites


def _make_suite_tree(root: Path, suite_dir: str, inner: str, launcher: str) -> None:
    tools = root / suite_dir / inner / "tools"
    tools.mkdir(parents=True)
    launcher_path = tools / launcher
    launcher_path.write_text("#!/bin/sh\n")
    launcher_path.chmod(0o755)


class LocalConfigManager:
    def __init__(self, suites_path: str):
        self.suites_path = suites_path

    def load_config(self):
        return {
            "ubuntu_host": "127.0.0.1",
            "ubuntu_user": "tester",
            "suites_path": self.suites_path,
        }

    def is_config_host_local(self, _config):
        return True

    def get_ubuntu_user(self, _config):
        return "tester"


class FailingSshManager:
    def __getattr__(self, name):
        raise AssertionError(f"Controller-local suite operation attempted SSH: {name}")


class ResolveSuiteReferenceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name) / "GMS-Suite"
        _make_suite_tree(self.root, "android-cts-17_r1", "android-cts", "cts-tradefed")
        _make_suite_tree(self.root, "android-gts-14-R2", "android-gts", "gts-tradefed")
        gts_root = self.root / "android-gts-14-R2" / "android-gts" / "tools" / "gts-root-tradefed"
        gts_root.write_text("#!/bin/sh\n")
        gts_root.chmod(0o755)
        self.config = {
            "ubuntu_host": "127.0.0.1",
            "ubuntu_user": "tester",
            "suites_path": str(self.root),
        }

    def tearDown(self):
        self._tmp.cleanup()

    def test_exact_short_name_resolves_to_tools_path(self):
        suite, message = resolve_suite_reference(self.config, "android-cts-17_r1")
        self.assertEqual(message, "")
        self.assertEqual(suite["tools_path"], str(self.root / "android-cts-17_r1" / "android-cts" / "tools"))

    def test_case_insensitive_substring_resolves_uniquely(self):
        suite, message = resolve_suite_reference(self.config, "CTS-17")
        self.assertEqual(message, "")
        self.assertEqual(suite["version"], "android-cts-17_r1")

    def test_multiple_launchers_in_one_tools_directory_are_one_suite(self):
        suite, message = resolve_suite_reference(self.config, "android-gts-14-R2")
        self.assertEqual(message, "")
        self.assertEqual(
            suite["tools_path"],
            str(self.root / "android-gts-14-R2" / "android-gts" / "tools"),
        )

    def test_ambiguous_reference_reports_candidates(self):
        suite, message = resolve_suite_reference(self.config, "android")
        self.assertIsNone(suite)
        self.assertIn("ambiguous", message)
        self.assertIn("android-cts-17_r1", message)
        self.assertIn("android-gts-14-R2", message)

    def test_unknown_reference_suggests_listing_suites(self):
        suite, message = resolve_suite_reference(self.config, "android-cts-99_r9")
        self.assertIsNone(suite)
        self.assertIn("/api/test/suites", message)

    def test_path_reference_is_untouched(self):
        suite, message = resolve_suite_reference(self.config, "/home/x/android-cts/tools")
        self.assertIsNone(suite)
        self.assertEqual(message, "")


class ParseArgsSuiteReferenceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name) / "GMS-Suite"
        _make_suite_tree(self.root, "android-cts-17_r1", "android-cts", "cts-tradefed")
        self.manager = LocalConfigManager(str(self.root))

    def tearDown(self):
        self._tmp.cleanup()

    async def test_short_suite_name_becomes_tools_path(self):
        previous_config = parse_api.runtime.config_manager
        previous_ssh = parse_api.runtime.ssh_manager
        parse_api.runtime.config_manager = self.manager
        parse_api.runtime.ssh_manager = FailingSshManager()
        try:
            response = await parse_api.parse_test_args(
                request=None,
                help=False,
                req=ParseArgsRequest(
                    params=["DEVICE-1", "CTS", "CtsCamera", "android-cts-17_r1"]
                ),
            )
        finally:
            parse_api.runtime.config_manager = previous_config
            parse_api.runtime.ssh_manager = previous_ssh
        expected = str(self.root / "android-cts-17_r1" / "android-cts" / "tools")
        self.assertEqual(response.test_suite, expected)
        self.assertEqual(response.test_module, "CtsCamera")
        self.assertEqual(response.warnings, [])

    async def test_unknown_short_name_keeps_positional_semantics(self):
        previous_config = parse_api.runtime.config_manager
        previous_ssh = parse_api.runtime.ssh_manager
        parse_api.runtime.config_manager = self.manager
        parse_api.runtime.ssh_manager = FailingSshManager()
        try:
            response = await parse_api.parse_test_args(
                request=None,
                help=False,
                req=ParseArgsRequest(
                    params=["DEVICE-1", "CTS", "android-cts-99_r9"]
                ),
            )
        finally:
            parse_api.runtime.config_manager = previous_config
            parse_api.runtime.ssh_manager = previous_ssh
        self.assertEqual(response.test_suite, "")
        self.assertEqual(response.test_module, "android-cts-99_r9")
        self.assertTrue(any("/api/test/suites" in warning for warning in response.warnings))


class SuitesResultShortNameTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name) / "GMS-Suite"
        _make_suite_tree(self.root, "android-cts-17_r1", "android-cts", "cts-tradefed")
        self.manager = LocalConfigManager(str(self.root))

    def tearDown(self):
        self._tmp.cleanup()

    async def test_short_name_resolves_before_path_validation(self):
        previous_config = transfers_api.runtime.config_manager
        previous_ssh = transfers_api.runtime.ssh_manager
        transfers_api.runtime.config_manager = self.manager
        transfers_api.runtime.ssh_manager = FailingSshManager()
        expected = str(self.root / "android-cts-17_r1" / "android-cts" / "tools")
        collected = {}

        async def fake_collect(config, suite_path, tradefed_bin, *, force_refresh=False):
            collected["suite_path"] = suite_path
            return {"success": True, "results": []}

        try:
            with (
                patch.object(transfers_api.runtime, "generate_help_or_continue", return_value=None),
                patch.object(transfers_api, "collect_tradefed_results", fake_collect),
            ):
                response = await transfers_api.list_tradefed_results(
                    req=TradefedListResultsRequest(suite_path="android-cts-17_r1")
                )
        finally:
            transfers_api.runtime.config_manager = previous_config
            transfers_api.runtime.ssh_manager = previous_ssh
        self.assertEqual(collected["suite_path"], expected)
        self.assertEqual(response.status_code, 200)

    async def test_unknown_short_name_returns_404_with_hint(self):
        previous_config = transfers_api.runtime.config_manager
        previous_ssh = transfers_api.runtime.ssh_manager
        transfers_api.runtime.config_manager = self.manager
        transfers_api.runtime.ssh_manager = FailingSshManager()
        try:
            with patch.object(transfers_api.runtime, "generate_help_or_continue", return_value=None):
                response = await transfers_api.list_tradefed_results(
                    req=TradefedListResultsRequest(suite_path="android-cts-99_r9")
                )
        finally:
            transfers_api.runtime.config_manager = previous_config
            transfers_api.runtime.ssh_manager = previous_ssh
        self.assertEqual(response.status_code, 404)
        body = bytes(response.body).decode("utf-8")
        self.assertIn("/api/test/suites", body)

    async def test_local_inventory_lists_the_planted_suite(self):
        suites = list_local_test_suites(str(self.root))
        versions = {suite["version"] for suite in suites}
        self.assertIn("android-cts-17_r1", versions)


if __name__ == "__main__":
    unittest.main()
