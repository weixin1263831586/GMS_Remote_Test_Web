import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from features.reports.analysis_api import _is_safe_report_delete_dir


class ReportDeleteSafetyTests(unittest.TestCase):
    def test_rejects_root_and_plain_directory(self):
        self.assertFalse(_is_safe_report_delete_dir("/"))
        with TemporaryDirectory() as tmp:
            self.assertFalse(_is_safe_report_delete_dir(tmp))

    def test_allows_directory_with_report_marker(self):
        with TemporaryDirectory() as tmp:
            report_dir = Path(tmp) / "2026.06.30_10.00.00"
            report_dir.mkdir()
            (report_dir / "test_result.xml").write_text("<Result />", encoding="utf-8")

            self.assertTrue(_is_safe_report_delete_dir(str(report_dir)))


if __name__ == "__main__":
    unittest.main()
