"""Decompile pulls are bounded by suffix and size before any transfer."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from features.devices import config_explorer_api as api
from foundation.config import APK_MAX_FILE_SIZE


class DecompileBoundaryTests(unittest.TestCase):
    def setUp(self):
        api.configure_config_explorer_dependencies(
            generate_help_or_continue=lambda *_args: None,
            create_apk_task=lambda *_args: None,
            normalize_apk_filename=lambda name: name,
            safe_join=lambda directory, name: os.path.join(directory, name),
            cleanup_files=lambda _files: None,
        )
        self.app = FastAPI()
        self.app.include_router(api.router)
        self.client = TestClient(self.app)
        locks = patch.object(api, "device_lock_manager")
        locks_mock = locks.start()
        locks_mock.get_lock_status.return_value = None
        self.addCleanup(locks.stop)

    def post(self, path):
        return self.client.post(
            "/api/config-explorer/decompile",
            json={"device_id": "SERIAL", "path": path},
        )

    def test_rejects_files_without_apk_or_jar_suffix(self):
        with patch.object(api, "device_file_size") as size, patch.object(api, "pull_device_file") as pull:
            response = self.post("/data/local/tmp/payload.bin")
        self.assertEqual(response.status_code, 400)
        self.assertIn("apk", response.json()["error"])
        size.assert_not_called()
        pull.assert_not_called()

    def test_rejects_oversized_file_before_any_bytes_move(self):
        with patch.object(api, "device_file_size", return_value=APK_MAX_FILE_SIZE + 1) as size, \
                patch.object(api, "pull_device_file") as pull:
            response = self.post("/data/app/~~abc/base.apk")
        self.assertEqual(response.status_code, 400)
        self.assertIn("文件过大", response.json()["error"])
        size.assert_called_once()
        pull.assert_not_called()

    def test_reports_unreadable_device_size_as_client_error(self):
        with patch.object(api, "device_file_size", side_effect=RuntimeError("stat failed")), \
                patch.object(api, "pull_device_file") as pull:
            response = self.post("/data/app/~~abc/base.apk")
        self.assertEqual(response.status_code, 400)
        pull.assert_not_called()

    def test_in_range_file_is_pulled_and_registered(self):
        with tempfile.TemporaryDirectory() as upload_dir, \
                patch.object(api, "APK_UPLOAD_DIR", upload_dir), \
                patch.object(api, "device_file_size", return_value=4096), \
                patch.object(api, "pull_device_file",
                             side_effect=lambda _device, _remote, local: Path(local).write_bytes(b"x" * 4096)) as pull, \
                patch.object(api, "_create_apk_task_fn") as task:
            response = self.post("/data/app/~~abc/base.apk")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["data"]["size"], 4096)
        pull.assert_called_once()
        task.assert_called_once()


if __name__ == "__main__":
    unittest.main()
