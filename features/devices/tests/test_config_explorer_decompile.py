"""Decompile pulls are bounded by suffix and size before any transfer."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from features.auth import CurrentUser
from features.devices.configuration import config_explorer_api as api
from foundation.config import APK_MAX_FILE_SIZE


class DecompileBoundaryTests(unittest.TestCase):
    def setUp(self):
        upload = tempfile.TemporaryDirectory()
        self.addCleanup(upload.cleanup)
        self.upload_dir = Path(upload.name)
        dependencies = patch.multiple(
            api,
            APK_UPLOAD_DIR=upload.name,
            _generate_help_or_continue=lambda *_args: None,
            _create_apk_task_fn=lambda *_args: None,
            _normalize_apk_filename_fn=lambda name: name,
            _safe_join_fn=os.path.join,
            _cleanup_files_fn=lambda files: [Path(path).unlink(missing_ok=True) for path in files],
        )
        dependencies.start()
        self.addCleanup(dependencies.stop)
        self.app = FastAPI()

        @self.app.middleware("http")
        async def authenticate(request, call_next):
            request.state.current_user = CurrentUser(id="tester", username="tester", role="user")
            request.state.auth_method = "session"
            return await call_next(request)

        self.app.include_router(api.router)
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)
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
        self.assertEqual(list(self.upload_dir.iterdir()), [])

    def test_reports_unreadable_device_size_as_client_error(self):
        with patch.object(api, "device_file_size", side_effect=RuntimeError("stat failed")), \
                patch.object(api, "pull_device_file") as pull:
            response = self.post("/data/app/~~abc/base.apk")
        self.assertEqual(response.status_code, 400)
        pull.assert_not_called()
        self.assertEqual(list(self.upload_dir.iterdir()), [])

    def test_in_range_file_is_pulled_and_registered(self):
        with patch.object(api, "device_file_size", return_value=4096), \
                patch.object(api, "pull_device_file",
                             side_effect=lambda _device, _remote, local: Path(local).write_bytes(b"x" * 4096)) as pull, \
                patch.object(api, "_create_apk_task_fn") as task:
            response = self.post("/data/app/~~abc/base.apk")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["data"]["size"], 4096)
        pull.assert_called_once()
        task.assert_called_once()
        self.assertEqual(task.call_args.args[-1], "tester")

    def test_incomplete_transfer_removes_staging_directory(self):
        def incomplete_pull(_device, _remote, local):
            Path(local).write_bytes(b"partial")
            raise RuntimeError("transfer failed")

        with patch.object(api, "device_file_size", return_value=4096), \
                patch.object(api, "pull_device_file", side_effect=incomplete_pull):
            response = self.post("/data/app/~~abc/base.apk")
        self.assertEqual(response.status_code, 502)
        self.assertEqual(list(self.upload_dir.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
