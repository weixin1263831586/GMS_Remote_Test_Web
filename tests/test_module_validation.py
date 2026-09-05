"""工单 P0-1/P0-2/P1-1/P1-2 相关模块的单元测试（全 mock，不碰真设备）。"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from worker_agent.module_validation import (
    detect_no_matched_modules,
    fuzzy_match_candidates,
    list_suite_modules,
    validate_module_name,
)


def _make_suite(tmp: str, modules: list[str]) -> str:
    """构造 <root>/tools + <suite>/testcases 布局，返回 tools 路径。"""
    suite = Path(tmp) / "android-cts"
    tools = suite / "tools"
    testcases = suite / "testcases"
    testcases.mkdir(parents=True)
    for name in modules:
        (testcases / name).mkdir()
        (testcases / f"{name}.config").write_text("{}", encoding="utf-8")
    tools.mkdir(exist_ok=True)
    return str(tools)


class ListSuiteModulesTests(unittest.TestCase):
    def test_lists_dirs_and_config_files_deduped(self):
        with tempfile.TemporaryDirectory() as tmp:
            tools = _make_suite(tmp, ["CtsHardwareTestCases", "CtsNetTestCases"])
            modules = list_suite_modules(tools)
            self.assertEqual(modules, ["CtsHardwareTestCases", "CtsNetTestCases"])

    def test_missing_testcases_returns_empty(self):
        self.assertEqual(list_suite_modules("/nonexistent/tools"), [])


class ValidateModuleNameTests(unittest.TestCase):
    def test_exact_match_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            tools = _make_suite(tmp, ["CtsHardwareTestCases"])
            self.assertIsNone(validate_module_name("CtsHardwareTestCases", tools))

    def test_case_insensitive_match_passes_with_correction_hint(self):
        with tempfile.TemporaryDirectory() as tmp:
            tools = _make_suite(tmp, ["CtsHardwareTestCases"])
            # 大小写不敏感命中 → 放行（返回 None），由脚本侧修正名称。
            self.assertIsNone(validate_module_name("ctshardwaretestcases", tools))

    def test_unknown_module_returns_error_with_candidates(self):
        with tempfile.TemporaryDirectory() as tmp:
            tools = _make_suite(tmp, ["CtsHardwareTestCases", "CtsNetTestCases"])
            error = validate_module_name("CtsHardwareTest", tools)
            self.assertIsNotNone(error)
            self.assertIn("module not found in suite: CtsHardwareTest", error)
            self.assertIn("CtsHardwareTestCases", error)

    def test_no_testcases_dir_skips_validation(self):
        self.assertIsNone(validate_module_name("Anything", "/nonexistent/tools"))

    def test_empty_module_skips_validation(self):
        self.assertIsNone(validate_module_name("", "/nonexistent/tools"))


class DetectNoMatchedModulesTests(unittest.TestCase):
    def test_detects_no_matched_modules(self):
        stdout = (
            "04-04 10:00:00 D/ConfigurationFactory: \n"
            "No matched tradefed modules from the given modules:"
            " [android.hardware.cts]\n"
        )
        error = detect_no_matched_modules(stdout)
        self.assertIsNotNone(error)
        self.assertIn("module not found in suite", error)
        self.assertIn("android.hardware.cts", error)

    def test_normal_stdout_returns_none(self):
        self.assertIsNone(detect_no_matched_modules("RESULT DIRECTORY: /tmp/x"))

    def test_empty_returns_none(self):
        self.assertIsNone(detect_no_matched_modules(""))


class FuzzyCandidatesTests(unittest.TestCase):
    def test_substring_match_first(self):
        candidates = fuzzy_match_candidates(
            "CtsHardware", ["CtsHardwareTestCases", "CtsNetTestCases", "CtsOsTestCases"]
        )
        self.assertEqual(candidates, ["CtsHardwareTestCases"])

    def test_fallback_to_edit_distance(self):
        candidates = fuzzy_match_candidates(
            "CtsHardwarTestCases", ["CtsHardwareTestCases", "CtsNetTestCases"]
        )
        self.assertIn("CtsHardwareTestCases", candidates)


class FailureEvidenceTests(unittest.TestCase):
    def test_collect_failure_evidence_uploads_artifacts(self):
        from worker_agent.failure_evidence import collect_failure_evidence

        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            uploads = []

            def fake_upload(job_id, attempt_id, path, kind):
                uploads.append((job_id, attempt_id, path.name, kind))

            def fake_adb(serial, *args, timeout=30):
                command = " ".join(args)
                if command.startswith("logcat"):
                    return "04-04 10:00:00 log line\n"
                if command == "getprop " or command == "getprop ":
                    return ""
                return "snapshot-output\n"

            with mock.patch(
                "worker_agent.failure_evidence._adb", side_effect=fake_adb
            ):
                result = collect_failure_evidence(
                    "job-1", "attempt-1", ["SERIAL1"], work_dir,
                    fake_upload, lambda action: action(),
                )
            names = [item["filename"] for item in result]
            self.assertIn("snapshot_getprop.txt", names)
            self.assertIn("logcat_before.txt", names)
            self.assertTrue(
                any(kind == "failure-evidence" for _, _, _, kind in uploads)
            )

    def test_collect_failure_evidence_survives_adb_failures(self):
        from worker_agent.failure_evidence import collect_failure_evidence

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch(
                "worker_agent.failure_evidence._adb", return_value=""
            ):
                result = collect_failure_evidence(
                    "job-1", "attempt-1", ["SERIAL1"], Path(tmp),
                    lambda *a, **k: None, lambda action: action(),
                )
            self.assertEqual(result, [])


if __name__ == "__main__":
    unittest.main()
