"""invocation_summary.txt LOG DIRECTORY 收容回归。

summary 文本是设备/套件运行环境产出的不可信证据：其中声明的日志目录
必须限制在报告套件根内（优先 report.suite_path，参照
downloads.report_suite_location 的 tools 剥离语义；无 suite_path 的
旧记录回退到 result_dir 布局推导），越界目录一律忽略。
"""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from features.reports.service import _contained_log_dir, _report_suite_root


class ReportSuiteRootTests(unittest.TestCase):
    def test_prefers_recorded_suite_path_with_tools_stripped(self):
        report = {"suite_path": "/opt/suite/android-cts/tools"}
        self.assertEqual(
            _report_suite_root(report, "/unrelated/results/run"),
            str(Path("/opt/suite/android-cts").resolve()),
        )

    def test_falls_back_to_result_dir_layout(self):
        report = {}
        self.assertEqual(
            _report_suite_root(report, "/opt/suite/results/2026.10.01_10.00.00"),
            str(Path("/opt/suite").resolve()),
        )

    def test_relative_suite_path_falls_back_to_result_dir(self):
        report = {"suite_path": "relative/suite"}
        root = _report_suite_root(
            report, "/opt/suite/results/2026.10.01_10.00.00"
        )
        self.assertEqual(root, str(Path("/opt/suite").resolve()))

    def test_missing_result_dir_returns_none(self):
        self.assertIsNone(_report_suite_root({}, ""))


class ContainedLogDirTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.suite_root = Path(self.tmp.name) / "android-cts"
        (self.suite_root / "results" / "run").mkdir(parents=True)
        (self.suite_root / "logs" / "run" / "inv_1").mkdir(parents=True)
        self.result_dir = str(self.suite_root / "results" / "run")
        self.report = {"suite_path": str(self.suite_root)}

    def tearDown(self):
        self.tmp.cleanup()

    def test_log_dir_inside_suite_root_is_kept(self):
        candidate = str(self.suite_root / "logs" / "run")
        self.assertEqual(
            _contained_log_dir(self.report, self.result_dir, candidate),
            str(Path(candidate).resolve()),
        )

    def test_log_dir_outside_suite_root_is_ignored(self):
        outside = str(Path(self.tmp.name) / "elsewhere" / "logs")
        self.assertIsNone(
            _contained_log_dir(self.report, self.result_dir, outside)
        )
        self.assertIsNone(
            _contained_log_dir(self.report, self.result_dir, "/etc")
        )

    def test_symlink_escape_resolves_outside_and_is_ignored(self):
        target = Path(self.tmp.name) / "outside"
        target.mkdir()
        link = self.suite_root / "logs-link"
        link.symlink_to(target, target_is_directory=True)
        self.assertIsNone(
            _contained_log_dir(self.report, self.result_dir, str(link))
        )

    def test_suite_root_itself_is_allowed(self):
        self.assertEqual(
            _contained_log_dir(
                self.report, self.result_dir, str(self.suite_root)
            ),
            str(self.suite_root.resolve()),
        )

    def test_legacy_report_without_suite_path_uses_result_dir_root(self):
        candidate = str(self.suite_root / "logs" / "run")
        self.assertEqual(
            _contained_log_dir({}, self.result_dir, candidate),
            str(Path(candidate).resolve()),
        )
        # 无 suite_path 兜底时，套件外路径同样被忽略。
        outside = Path(self.tmp.name) / "elsewhere"
        outside.mkdir(exist_ok=True)
        self.assertIsNone(
            _contained_log_dir({}, self.result_dir, str(outside))
        )

    def test_empty_candidate_returns_none(self):
        self.assertIsNone(_contained_log_dir(self.report, self.result_dir, ""))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
