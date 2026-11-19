import io
import stat
import tarfile
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
    def test_zip_rejects_parent_absolute_and_symlink_members(self):
        from features.reports import ReportAnalysisAgent

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ("../escape.txt", "/absolute.txt"):
                archive = root / "unsafe.zip"
                with zipfile.ZipFile(archive, "w") as zf:
                    zf.writestr(name, "escape")
                with self.assertRaises(ValueError):
                    ReportAnalysisAgent(temp_dir=tmp)._extract_zip(
                        str(archive),
                        str(root / "extract"),
                    )

            archive = root / "symlink.zip"
            link = zipfile.ZipInfo("link")
            link.create_system = 3
            link.external_attr = (stat.S_IFLNK | 0o777) << 16
            with zipfile.ZipFile(archive, "w") as zf:
                zf.writestr(link, "../outside")
            with self.assertRaises(ValueError):
                ReportAnalysisAgent(temp_dir=tmp)._extract_zip(
                    str(archive),
                    str(root / "extract"),
                )

    def test_tar_rejects_parent_absolute_and_symlink_members(self):
        from features.reports import ReportAnalysisAgent

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ("../escape.txt", "/absolute.txt"):
                archive = root / "unsafe.tar"
                with tarfile.open(archive, "w") as tf:
                    member = tarfile.TarInfo(name)
                    member.size = 1
                    tf.addfile(member, io.BytesIO(b"x"))
                with self.assertRaises(ValueError):
                    ReportAnalysisAgent(temp_dir=tmp)._extract_tar(
                        str(archive),
                        str(root / "extract"),
                    )

            archive = root / "symlink.tar"
            with tarfile.open(archive, "w") as tf:
                member = tarfile.TarInfo("link")
                member.type = tarfile.SYMTYPE
                member.linkname = "../outside"
                tf.addfile(member)
            ReportAnalysisAgent(temp_dir=tmp)._extract_tar(
                str(archive),
                str(root / "extract"),
            )
            self.assertFalse((root / "extract/link").exists())

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
        from features.reports import ReportAnalysisAgent

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
        self.assertTrue(result["failures"][0]["raw_log"].startswith(
            "03-07 08:50:02 I/ModuleListener: FAILURE: "
        ))
        self.assertIn("java.lang.AssertionError: expected true", result["failures"][0]["raw_log"])
        self.assertEqual(len(result["analysis_sources"]["host_logs"]), 2)
        self.assertEqual(len(result["analysis_sources"]["device_logs"]), 2)
        self.assertGreaterEqual(result["host_log_errors"]["total_errors"], 1)
        self.assertGreaterEqual(result["device_log_errors"]["total_errors"], 2)
        self.assertEqual(len(result["failures_html"]["failures"]), 1)

    def test_cts_module_listener_full_failure_survives_zip_analysis(self):
        """The UI receives the original timestamp/prefix AND every stack frame."""
        from xml.sax.saxutils import escape, quoteattr

        from features.reports import ReportAnalysisAgent

        case_class = "android.server.wm.activity.lifecycle.ActivityLifecycleTopResumedStateTests"
        method = "testTopPositionSwitchToActivityOnTopTimeoutDifferentProcess"
        message = (
            "java.lang.AssertionError: Transition wasn't observed in the expected position: "
            "Pair{android.server.wm.activity.lifecycle.ActivityLifecycleClientTestBase."
            "SlowActivity ON_TOP_POSITION_LOST} during transition: launchOnTop"
        )
        host_log = (
            "11-16 17:13:33 I/ModuleListener: [1/1] m10r0800015 "
            f"android.server.wm.cts {case_class}#{method} FAILURE: {message}\n"
            "\tat org.junit.Assert.fail(Assert.java:89)\n"
            "\tat android.server.wm.activity.lifecycle.TransitionVerifier."
            "assertOrder(TransitionVerifier.java:473)\n"
            f"\tat {case_class}.{method}(ActivityLifecycleTopResumedStateTests.java:179)"
        )
        xml = (
            '<Result suite_name="CTS" suite_version="17_r3" build_version_release="17">'
            '<Summary pass="0" failed="1" />'
            '<Module name="CtsWindowManagerDeviceActivity">'
            f'<TestCase name="{case_class}">'
            f'<Test name="{method}" result="fail">'
            f'<Failure message={quoteattr(message)}>{escape(message)}</Failure>'
            '</Test></TestCase></Module></Result>'
        )
        with TemporaryDirectory() as tmp:
            archive = Path(tmp) / "report.zip"
            with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
                zf.writestr("results/test_result.xml", xml)
                zf.writestr("logs/inv_cts_1/host_log_1.txt", host_log)
            result = ReportAnalysisAgent(temp_dir=tmp).analyze_path(str(archive))

        self.assertIsNotNone(result)
        self.assertEqual(result["failures"][0]["name"], f"{case_class}#{method}")
        self.assertEqual(result["failures"][0]["raw_log"], host_log)
        self.assertIn("FAILURE: java.lang.AssertionError", result["failures"][0]["raw_log"])
        self.assertIn("ActivityLifecycleTopResumedStateTests.java:179", result["failures"][0]["raw_log"])

    def test_multiple_retries_choose_latest_matching_original_entry(self):
        from features.reports.analysis_agent import ReportAnalysisAgent
        from features.reports.models import TestFailure

        case = "org.example.Test#fails"
        xml = [{"name": case, "module": "ModuleA", "reason": "expected true", "raw_log": ""}]
        first = TestFailure(
            name=case, module="ModuleA", reason="expected true",
            raw_log="11-16 17:13:30 I/ModuleListener: FAILURE: expected true",
        )
        last = TestFailure(
            name=case, module="ModuleA", reason="expected true",
            raw_log="11-16 17:13:33 I/ModuleListener: FAILURE: expected true",
        )
        other = TestFailure(
            name=case, module="ModuleA", reason="different failure",
            raw_log="11-16 17:13:50 I/ModuleListener: FAILURE: different failure",
        )
        ReportAnalysisAgent._attach_original_host_logs(
            xml, [Mock(failures=[last, other, first])],
        )
        self.assertEqual(xml[0]["raw_log"], last.raw_log)

    def test_raw_host_log_must_not_cross_module_boundary(self):
        from features.reports.analysis_agent import ReportAnalysisAgent
        from features.reports.models import TestFailure

        case = "org.example.SharedTest#fails"
        xml = [{"name": case, "module": "ModuleA",
                "reason": "expected true", "raw_log": ""}]
        wrong = TestFailure(name=case, module="ModuleB",
                            reason="expected true", raw_log="WRONG MODULE LOG")
        ReportAnalysisAgent._attach_original_host_logs(
            xml, [Mock(failures=[wrong])],
        )
        self.assertEqual(xml[0]["raw_log"], "")

        correct = TestFailure(name=case, module="ModuleA",
                              reason="expected true", raw_log="CORRECT MODULE LOG")
        ReportAnalysisAgent._attach_original_host_logs(
            xml, [Mock(failures=[wrong, correct])],
        )
        self.assertEqual(xml[0]["raw_log"], "CORRECT MODULE LOG")

    def test_generic_host_suite_name_matches_specific_xml_module(self):
        from features.reports.analysis_agent import ReportAnalysisAgent
        from features.reports.models import TestFailure

        case = "org.example.SharedTest#fails"
        xml = [{"name": case, "module": "GtsSampleTestCases",
                "reason": "assertion", "raw_log": ""}]
        host = TestFailure(name=case, module="GTS",
                           reason="assertion", raw_log="TIMESTAMPED FAILURE BLOCK")
        ReportAnalysisAgent._attach_original_host_logs(xml, [Mock(failures=[host])])
        self.assertEqual(xml[0]["raw_log"], "TIMESTAMPED FAILURE BLOCK")

    def test_file_limit_rejects_partial_bundle_before_parsing(self):
        from features.reports.analysis_agent import (
            ReportAnalysisAgent,
            ReportScanIncompleteError,
        )

        with TemporaryDirectory() as tmp:
            extra = Path(tmp) / "noise.txt"
            xml = Path(tmp) / "test_result.xml"
            extra.write_text("noise", encoding="utf-8")
            xml.write_text(TEST_XML, encoding="utf-8")
            with self.assertRaisesRegex(ReportScanIncompleteError, "1 个上限"):
                ReportAnalysisAgent(temp_dir=tmp, max_files=1).analyze_paths(
                    [str(extra), str(xml)]
                )

    def test_nested_archive_limit_is_explicit(self):
        from features.reports.analysis_agent import (
            ReportAnalysisAgent,
            ReportScanIncompleteError,
        )

        with TemporaryDirectory() as tmp:
            archive = Path(tmp) / "nested.zip"
            with zipfile.ZipFile(archive, "w") as zf:
                zf.writestr("test_result.xml", TEST_XML)
            with self.assertRaisesRegex(ReportScanIncompleteError, "0 层上限"):
                ReportAnalysisAgent(temp_dir=tmp, max_depth=0).analyze_path(
                    str(archive)
                )

    def test_xml_only_does_not_invent_host_log(self):
        from features.reports import ReportAnalysisAgent

        with TemporaryDirectory() as tmp:
            xml = Path(tmp) / "test_result.xml"
            xml.write_text(TEST_XML, encoding="utf-8")
            result = ReportAnalysisAgent(temp_dir=tmp).analyze_path(str(xml))

        self.assertIsNotNone(result)
        self.assertEqual(result["failures"][0]["raw_log"], "")
        self.assertIn("java.lang.AssertionError", result["failures"][0]["reason"])

    def test_report_analyzer_uses_agent_for_nested_archives(self):
        from features.reports import ReportAnalyzer

        with TemporaryDirectory() as tmp:
            archive_path = Path(tmp) / "uploaded_report.zip"
            self._make_nested_report_zip(archive_path)

            result = ReportAnalyzer(temp_dir=tmp).analyze_file(str(archive_path))

        self.assertIsNotNone(result)
        self.assertEqual(result["summary"]["fail"], 1)
        self.assertEqual(result["details"]["soc_platform"], "RK3588")
        self.assertEqual(len(result["analysis_sources"]["host_logs"]), 2)

    def test_saved_report_manager_analyzes_related_logs_with_agent(self):
        from features.reports import TestReportManager

        report_name = "2026.03.07_08.49.59.870_8298"
        timestamp = "cluster-job-1"
        with TemporaryDirectory() as tmp:
            suite_root = Path(tmp) / "android-gts"
            result_dir = suite_root / "results" / report_name
            log_dir_one = suite_root / "logs" / report_name / "inv_mcts_7505152775443919750"
            log_dir_two = suite_root / "logs" / report_name / "inv_static_xts_12399479541396617616"
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
            manager.test_report_db.get_report_by_timestamp.return_value = {
                "timestamp": timestamp,
                "report_name": report_name,
                "source_timestamp": report_name,
                "result_dir": str(result_dir),
            }

            result = manager.analyze_report(timestamp)

        self.assertIsNotNone(result)
        self.assertEqual(result["report_name"], report_name)
        self.assertEqual(result["details"]["soc_platform"], "RK3588")
        self.assertEqual(len(result["analysis_sources"]["host_logs"]), 2)
        self.assertEqual(len(result["analysis_sources"]["device_logs"]), 2)


if __name__ == "__main__":
    unittest.main()
