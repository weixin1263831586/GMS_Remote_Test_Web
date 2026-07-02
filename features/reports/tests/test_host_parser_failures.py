import unittest

from features.reports.host_parser import HostLogParser


def _ts_line(body: str) -> str:
    """A normal tradefed host_log line with a timestamp + level/tag prefix."""
    return f"07-02 09:20:17 I/ModuleListener: {body}"


class HostLogFailureExtractionTests(unittest.TestCase):
    """Regression coverage for failure-body extraction in host_log parsing.

    The parser must collect the *full* multi-line failure body, including blank
    lines that separate paragraphs of the failure message, and stop only at the
    next timestamped log line (the real boundary between log entries).
    """

    def test_multiline_failure_with_blank_lines_is_kept_intact(self):
        # Reproduces the SuspendSepolicyTests SELinux report whose body is split
        # into paragraphs by blank lines. Previously the first blank line
        # truncated the message, losing the genfscon rules and exit code.
        log = "\n".join([
            _ts_line("[1/1] RK3576GMS1 SuspendSepolicyTests#SuspendSepolicyTests FAILURE: "),
            "Unlabeled wakeup nodes found, your device is likely missing",
            "device/oem specific selinux genfscon rules for suspend.",
            "",
            "Please review and add the following generated rules to the",
            "device specific genfs_contexts:",
            "",
            "genfscon sysfs devices/platform/2ac90000.i2c/i2c-6/6-004e/power_supply/tcpm-source-psy-6-004e/wakeup7 u:object_r:sysfs_wakeup:s0",
            "",
            "Missing sysfs_wakeup labels",
            "",
            "Exit Code: 1",
            "07-02 09:20:17 D/BackgroundDeviceAction: next unrelated log entry",
        ])
        report = HostLogParser().parse_content(log, "android-vts-17_r1/android-vts")
        self.assertEqual(len(report.failures), 1)
        failure = report.failures[0]
        self.assertIn("SuspendSepolicyTests", failure.name)
        # The full body survived — including the parts after the blank lines.
        self.assertIn("genfscon sysfs devices/platform", failure.reason)
        self.assertIn("device specific genfs_contexts:", failure.reason)
        self.assertIn("Missing sysfs_wakeup labels", failure.reason)
        self.assertIn("Exit Code: 1", failure.reason)
        # And nothing from the following unrelated log line leaked in.
        self.assertNotIn("BackgroundDeviceAction", failure.reason)

    def test_short_failure_does_not_consume_next_entry(self):
        # A single-line failure followed immediately by another timestamped log
        # line must capture only the failure, not the next entry.
        log = "\n".join([
            _ts_line("[1/1] dev SomeModule#someTest FAILURE: short error"),
            "07-02 09:20:18 I/ConsoleReporter: SomeModule completed in 1s. 0 passed, 1 failed",
        ])
        report = HostLogParser().parse_content(log, "android-vts")
        self.assertEqual(len(report.failures), 1)
        self.assertIn("short error", report.failures[0].reason)
        self.assertNotIn("completed in", report.failures[0].reason)

    def test_two_consecutive_failures_are_split_correctly(self):
        # Two failures in a row, each with its own body, separated only by the
        # next FAILURE line. Neither should swallow the other.
        log = "\n".join([
            _ts_line("[1/2] dev Mod#t1 FAILURE: first error detail"),
            "  extra line for first",
            _ts_line("[2/2] dev Mod#t2 FAILURE: second error detail"),
            "  extra line for second",
            "07-02 09:20:20 D/Done: finished",
        ])
        report = HostLogParser().parse_content(log, "android-vts")
        self.assertEqual(len(report.failures), 2)
        self.assertIn("first error detail", report.failures[0].reason)
        self.assertIn("second error detail", report.failures[1].reason)
        # First failure did not bleed into the second.
        self.assertNotIn("second error", report.failures[0].reason)


if __name__ == "__main__":
    unittest.main()
