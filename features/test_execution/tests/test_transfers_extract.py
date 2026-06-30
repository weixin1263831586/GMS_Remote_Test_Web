import io
import tarfile
import unittest
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory

from features.test_execution.transfers_api import _extract_archive_local_with_progress


class SuiteExtractTests(unittest.TestCase):
    def test_zip_extract_without_target_dir_rejects_parent_path(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "unsafe.zip"
            with zipfile.ZipFile(archive, "w") as zf:
                zf.writestr("../escape.txt", "bad")

            with self.assertRaises(ValueError):
                _extract_archive_local_with_progress(str(archive), str(root / "extract"), "")

            self.assertFalse((root / "escape.txt").exists())

    def test_tar_extract_without_target_dir_rejects_parent_path(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "unsafe.tar"
            payload = b"bad"
            info = tarfile.TarInfo("../escape.txt")
            info.size = len(payload)
            with tarfile.open(archive, "w") as tf:
                tf.addfile(info, io.BytesIO(payload))

            with self.assertRaises(ValueError):
                _extract_archive_local_with_progress(str(archive), str(root / "extract"), "")

            self.assertFalse((root / "escape.txt").exists())


if __name__ == "__main__":
    unittest.main()
