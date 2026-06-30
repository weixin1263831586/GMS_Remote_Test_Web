import io
import tarfile
import unittest
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from features.reports.archive import ReportAnalyzer, ReportFileHandler


class ReportFileHandlerTests(unittest.TestCase):
    def test_report_analyzer_default_temp_dir_can_be_configured(self):
        with TemporaryDirectory() as tmp:
            expected = str(Path(tmp) / "reports")
            with patch.dict("os.environ", {"GMS_REPORT_TEMP_DIR": expected}):
                analyzer = ReportAnalyzer()

        self.assertEqual(analyzer.temp_dir, expected)

    def test_zip_extract_rejects_parent_path(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "unsafe.zip"
            with zipfile.ZipFile(archive, "w") as zf:
                zf.writestr("../escape.txt", "bad")

            self.assertFalse(ReportFileHandler(str(root / "extract")).extract_archive(str(archive)))
            self.assertFalse((root / "escape.txt").exists())

    def test_tar_extract_rejects_parent_path(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "unsafe.tar"
            payload = b"bad"
            info = tarfile.TarInfo("../escape.txt")
            info.size = len(payload)
            with tarfile.open(archive, "w") as tf:
                tf.addfile(info, io.BytesIO(payload))

            self.assertFalse(ReportFileHandler(str(root / "extract")).extract_archive(str(archive)))
            self.assertFalse((root / "escape.txt").exists())


if __name__ == "__main__":
    unittest.main()
