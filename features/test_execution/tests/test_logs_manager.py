import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from features.test_execution.logs import TestLogsManager as GmsTestLogsManager


class TestLogsManagerTests(unittest.TestCase):
    def test_test_log_root_can_be_configured_by_environment(self):
        with TemporaryDirectory() as tmp, patch.dict("os.environ", {"GMS_TEST_LOG_ROOT": tmp}):
            manager = GmsTestLogsManager()

        self.assertEqual(manager.saved_logs_dir, Path(tmp) / "saved")
        self.assertEqual(manager.downloads_dir, Path(tmp) / "downloads")
        self.assertIn(Path(tmp), manager.log_dirs)

    def test_get_log_file_rejects_path_outside_allowed_roots(self):
        with TemporaryDirectory() as tmp:
            manager = GmsTestLogsManager()
            manager.log_dirs = [Path(tmp) / "logs"]

            result = manager.get_log_file(str(Path(tmp) / "outside.log"))

        self.assertFalse(result["success"])
        self.assertIn("不在允许目录", result["error"])

    def test_download_logs_rejects_path_outside_allowed_roots(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = GmsTestLogsManager()
            manager.log_dirs = [root / "logs"]
            (root / "logs").mkdir()
            outside = root / "outside.log"
            outside.write_text("secret", encoding="utf-8")

            result = manager.download_logs([str(outside)], str(root / "logs.zip"))

        self.assertFalse(result["success"])
        self.assertIn("不在允许目录", result["error"])

    def test_save_current_log_sanitizes_client_id(self):
        with TemporaryDirectory() as tmp:
            manager = GmsTestLogsManager()
            original_log_dirs = manager.log_dirs
            original_saved_dir = manager.saved_logs_dir
            manager.log_dirs = [Path(tmp)]
            manager.saved_logs_dir = Path(tmp)
            try:
                result = manager.save_current_log("hello", "../../bad/client")
            finally:
                manager.log_dirs = original_log_dirs
                manager.saved_logs_dir = original_saved_dir

        self.assertTrue(result["success"])
        self.assertNotIn("..", result["filename"])
        self.assertNotIn("/", result["filename"])

    def test_clean_old_logs_reports_failed_deletes(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            log_file = root / "old.log"
            log_file.write_text("old", encoding="utf-8")
            log_file.touch()
            manager = GmsTestLogsManager()
            manager.log_dirs = [root]
            with patch.object(Path, "unlink", side_effect=OSError("permission denied")):
                result = manager.clean_old_logs(days=0)

        self.assertEqual(result["cleaned_files"], 0)
        self.assertEqual(result["failed_files"], 1)


if __name__ == "__main__":
    unittest.main()
