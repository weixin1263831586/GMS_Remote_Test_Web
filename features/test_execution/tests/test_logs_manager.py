import unittest
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
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
        self.assertIn("无效的日志标识", result["error"])

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

    def test_save_current_log_names_file_by_test_type_and_timestamp(self):
        class FixedDatetime(datetime):
            @classmethod
            def now(cls):
                return cls(2026, 9, 5, 9, 20, 30)

        with TemporaryDirectory() as tmp, patch(
            "features.test_execution.logs.datetime", FixedDatetime
        ):
            manager = GmsTestLogsManager()
            manager.saved_logs_dir = Path(tmp)
            manager.log_dirs = [Path(tmp)]
            result = manager.save_current_log("hello", "user-a", test_type="CTS")
            first = result["filename"]
            again = manager.save_current_log("hello", "user-a", test_type="CTS")
            second = again["filename"]

        self.assertTrue(result["success"])
        self.assertEqual(first, "test_log_CTS_20260905_092030.log")
        self.assertEqual(second, "test_log_CTS_20260905_092030_1.log")

    def test_regular_user_cannot_list_or_download_another_owners_log(self):
        with TemporaryDirectory() as tmp, patch("features.test_execution.logs.datetime") as clock:
            clock.now.return_value = datetime(2026, 9, 5, 9, 20, 30)
            manager = GmsTestLogsManager()
            manager.saved_logs_dir = Path(tmp) / "saved"
            manager.downloads_dir = Path(tmp) / "downloads"
            manager.log_dirs = [Path(tmp)]
            alice = manager.save_current_log("alice", "alice")
            bob = manager.save_current_log("bob", "bob")
            alice_id = manager.log_id_for_path(alice["file_path"])
            bob_id = manager.log_id_for_path(bob["file_path"])

            listed = manager.list_log_files(owner_id="alice")
            denied = manager.download_logs(
                [bob_id],
                owner_id="alice",
            )
            admin = manager.download_logs(
                [alice_id, bob_id, alice_id],
                owner_id="admin",
                is_admin=True,
            )
            second_archive = manager.download_logs([alice_id], owner_id="admin", is_admin=True)
            self.assertNotEqual(admin["zip_path"], second_archive["zip_path"])
            with zipfile.ZipFile(admin["zip_path"]) as archive:
                self.assertEqual(archive.namelist(), [alice_id, bob_id])
                self.assertEqual(archive.read(alice_id), b"alice")
                self.assertEqual(archive.read(bob_id), b"bob")

        self.assertEqual([item["name"] for item in listed["files"]], [alice["filename"]])
        self.assertFalse(denied["success"])
        self.assertTrue(admin["success"])
        self.assertEqual(admin["file_count"], 2)

    def test_concurrent_saves_keep_every_log(self):
        with TemporaryDirectory() as tmp, patch("features.test_execution.logs.datetime") as clock:
            clock.now.return_value = datetime(2026, 9, 5, 9, 20, 30)
            manager = GmsTestLogsManager()
            manager.saved_logs_dir = Path(tmp)
            contents = [f"log-{index}" for index in range(8)]
            with ThreadPoolExecutor() as threads:
                results = list(threads.map(lambda content: manager.save_current_log(content, "alice"), contents))
            self.assertEqual(len({result["filename"] for result in results}), len(contents))
            self.assertEqual([Path(result["file_path"]).read_text() for result in results], contents)


if __name__ == "__main__":
    unittest.main()
