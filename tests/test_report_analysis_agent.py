import io
import unittest
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock


TEST_XML = """<?xml version="1.0" encoding="UTF-8"?>
<Result suite_name="GTS" suite_version="16.1_r2" build_version_release="16" devices="c3d9b8674f4b94f6" start_display="2026.03.07_08.49.59.870_8298">
  <Build build_version_release="16" suite_version="16.1_r2" device_serial="c3d9b8674f4b94f6" />
  <Summary pass="1" failed="1" />
  <Module name="GtsSampleTestCases">
    <TestCase name="com.google.android.gts.SampleTest">
      <Test name="testFailure" result="fail">
        <Failure message="java.lang.AssertionError: expected true">java.lang.AssertionError: expected true
at com.google.android.gts.SampleTest.testFailure(SampleTest.java:42)</Failure>
      </Test>
    </TestCase>
  </Module>
</Result>
"""


FAILURES_HTML = """
<html><body>
<td class="testname">com.google.android.gts.SampleTest#testFailure</td>
<div class="details">java.lang.AssertionError: expected true</div>
</body></html>
"""


HOST_LOG_ONE = """03-07 08:49:59 I/TestInvocation: Starting invocation for 'GTS'
03-07 08:50:00 I/BuildInfo: ro.product.board=RK3588
03-07 08:50:01 I/BuildInfo: ro.build.version.release=16
03-07 08:50:02 I/ModuleListener: FAILURE: com.google.android.gts.SampleTest#testFailure
java.lang.AssertionError: expected true
03-07 08:50:03 I/ConsoleReporter: 1 passed, 1 failed
"""


DEVICE_LOG_ONE = """03-07 08:50:04 E/AndroidRuntime: FATAL EXCEPTION: main
java.lang.RuntimeException: device crash
    at com.example.Device.run(Device.java:10)
"""


HOST_LOG_TWO = """03-07 08:51:00 I/TestInvocation: Starting invocation for 'GTS'
03-07 08:51:01 I/BuildInfo: ro.board.platform=rk3588
03-07 08:51:02 E/TestInvocation: HarnessRuntimeException[DEVICE_UNAVAILABLE|1|ERROR]: device offline
"""


DEVICE_LOG_TWO = """03-07 08:51:04 E/AndroidRuntime: FATAL EXCEPTION: worker
java.lang.IllegalStateException: second device crash
"""


class ReportAnalysisAgentTests(unittest.TestCase):
    def _make_nested_report_zip(self, target: Path) -> None:
        inner_buffer = io.BytesIO()
        with zipfile.ZipFile(inner_buffer, "w", zipfile.ZIP_DEFLATED) as inner:
            base = "2026.03.07_08.49.59.870_8298"
            inner.writestr(f"results/{base}/test_result.xml", TEST_XML)
            inner.writestr(f"results/{base}/test_result_failures_suite.html", FAILURES_HTML)
            inner.writestr(f"results/{base}/test_result.html", "<html>GTS report</html>")
            inner.writestr(
                f"logs/{base}/inv_mcts_7505152775443919750/host_log_3164372127909839935.txt",
                HOST_LOG_ONE,
            )
            inner.writestr(
                f"logs/{base}/inv_mcts_7505152775443919750/device_logcat_test_c3d9b8674f4b94f6_15713096786812764316.txt",
                DEVICE_LOG_ONE,
            )
            inner.writestr(
                f"logs/{base}/inv_static_xts_12399479541396617616/host_log_14694664358041210251.txt",
                HOST_LOG_TWO,
            )
            inner.writestr(
                f"logs/{base}/inv_static_xts_12399479541396617616/device_logcat_test_c3d9b8674f4b94f6_2153242408896980044.txt",
                DEVICE_LOG_TWO,
            )

        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as outer:
            outer.writestr("outer/readme.txt", "nested report")
            outer.writestr("outer/report_payload.zip", inner_buffer.getvalue())

    def test_agent_recursively_analyzes_nested_report_archives_and_logs(self):
        from core.agent.report_analysis_agent import ReportAnalysisAgent

        with TemporaryDirectory() as tmp:
            archive_path = Path(tmp) / "redmine_attachment.zip"
            self._make_nested_report_zip(archive_path)

            result = ReportAnalysisAgent(temp_dir=tmp).analyze_path(str(archive_path))

        self.assertIsNotNone(result)
        self.assertEqual(result["summary"]["total"], 2)
        self.assertEqual(result["summary"]["pass"], 1)
        self.assertEqual(result["summary"]["fail"], 1)
        self.assertEqual(result["details"]["android_version"], "16")
        self.assertEqual(result["details"]["suite_version"], "16.1_r2")
        self.assertEqual(result["details"]["soc_platform"], "RK3588")
        self.assertEqual(result["details"]["test_type"], "GTS")
        self.assertEqual(len(result["failures"]), 1)
        self.assertEqual(len(result["analysis_sources"]["host_logs"]), 2)
        self.assertEqual(len(result["analysis_sources"]["device_logs"]), 2)
        self.assertGreaterEqual(result["host_log_errors"]["total_errors"], 1)
        self.assertGreaterEqual(result["device_log_errors"]["total_errors"], 2)
        self.assertEqual(len(result["failures_html"]["failures"]), 1)

    def test_legacy_report_analyzer_uses_agent_for_nested_archives(self):
        from core.report_analyzer import ReportAnalyzer

        with TemporaryDirectory() as tmp:
            archive_path = Path(tmp) / "uploaded_report.zip"
            self._make_nested_report_zip(archive_path)

            result = ReportAnalyzer(temp_dir=tmp).analyze_file(str(archive_path))

        self.assertIsNotNone(result)
        self.assertEqual(result["summary"]["fail"], 1)
        self.assertEqual(result["details"]["soc_platform"], "RK3588")
        self.assertEqual(len(result["analysis_sources"]["host_logs"]), 2)

    def test_saved_report_manager_analyzes_related_logs_with_agent(self):
        from core.test_report import TestReportManager

        timestamp = "2026.03.07_08.49.59.870_8298"
        with TemporaryDirectory() as tmp:
            suite_root = Path(tmp) / "android-gts"
            result_dir = suite_root / "results" / timestamp
            log_dir_one = suite_root / "logs" / timestamp / "inv_mcts_7505152775443919750"
            log_dir_two = suite_root / "logs" / timestamp / "inv_static_xts_12399479541396617616"
            result_dir.mkdir(parents=True)
            log_dir_one.mkdir(parents=True)
            log_dir_two.mkdir(parents=True)
            (result_dir / "test_result.xml").write_text(TEST_XML, encoding="utf-8")
            (result_dir / "test_result_failures_suite.html").write_text(FAILURES_HTML, encoding="utf-8")
            (log_dir_one / "host_log_3164372127909839935.txt").write_text(HOST_LOG_ONE, encoding="utf-8")
            (log_dir_one / "device_logcat_test_c3d9b8674f4b94f6_15713096786812764316.txt").write_text(
                DEVICE_LOG_ONE,
                encoding="utf-8",
            )
            (log_dir_two / "host_log_14694664358041210251.txt").write_text(HOST_LOG_TWO, encoding="utf-8")
            (log_dir_two / "device_logcat_test_c3d9b8674f4b94f6_2153242408896980044.txt").write_text(
                DEVICE_LOG_TWO,
                encoding="utf-8",
            )

            manager = TestReportManager()
            manager.test_report_db = Mock()
            manager.test_report_db.get_report_by_timestamp.return_value = {"result_dir": str(result_dir)}

            result = manager.analyze_report(timestamp)

        self.assertIsNotNone(result)
        self.assertEqual(result["details"]["soc_platform"], "RK3588")
        self.assertEqual(len(result["analysis_sources"]["host_logs"]), 2)
        self.assertEqual(len(result["analysis_sources"]["device_logs"]), 2)


if __name__ == "__main__":
    unittest.main()
