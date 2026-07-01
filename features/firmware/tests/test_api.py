import threading
import unittest
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from features.firmware import api, runtime


class FakeConfigManager:
    def load_config(self):
        return {"ubuntu_user": "tester", "client_username": "codex"}

    def get_ubuntu_user(self, config):
        return config.get("ubuntu_user", "tester")


class FakeSshManager:
    def __init__(self, file_check_output):
        self.file_check_output = file_check_output
        self.commands = []

    def optional_connection(self, _config):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def get_transport(self):
        return object()

    def execute_command(self, _ssh, cmd, timeout=None):
        self.commands.append((cmd, timeout))
        if "test -f" in cmd:
            return self.file_check_output, "", 0
        return "", "", 0


async def fake_lock_firmware_devices(**_kwargs):
    return ["D1"], None


async def fake_release_firmware_devices(*_args, **_kwargs):
    return None


class FirmwareApiTests(unittest.TestCase):
    def setUp(self):
        runtime.configure_runtime(
            config_manager=FakeConfigManager(),
            ssh_manager=FakeSshManager("__GMS_REMOTE_FILE_MISSING__\n"),
            global_state=SimpleNamespace(
                apk_analysis_tasks={},
                apk_analysis_tasks_lock=threading.RLock(),
                apk_upload_locks={},
                apk_upload_locks_lock=threading.RLock(),
                firmware_upload_progress={},
                firmware_upload_progress_lock=threading.RLock(),
                websocket_connections={},
            ),
            generate_help_or_continue=lambda help, *_args: (
                JSONResponse({"help": True}) if help else None
            ),
            get_client_id_from_request=lambda _request: "codex@127.0.0.1",
            lock_firmware_devices=fake_lock_firmware_devices,
            release_firmware_devices=fake_release_firmware_devices,
            project_root=".",
        )
        app = FastAPI()
        app.include_router(api.router)
        self.client = TestClient(app)

    def test_firmware_help_does_not_execute_host_command(self):
        response = self.client.post("/api/burn/firmware?help=true")

        self.assertEqual(response.status_code, 200)

    def test_apk_missing_task_returns_not_found(self):
        response = self.client.get("/api/apk/status/missing-task")

        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.json()["success"])

    def test_apk_unknown_uuid_task_returns_not_found(self):
        response = self.client.get("/api/apk/status/00000000-0000-0000-0000-000000000000")

        self.assertEqual(response.status_code, 404)
        self.assertFalse(response.json()["success"])

    def test_apk_delete_rejects_non_uuid_task_id(self):
        response = self.client.delete("/api/apk/task/not-a-uuid")

        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.json()["success"])

    def test_missing_remote_firmware_path_does_not_enter_loader(self):
        fake_ssh = FakeSshManager("__GMS_REMOTE_FILE_MISSING__\n")
        runtime.configure_runtime(ssh_manager=fake_ssh)

        response = self.client.post(
            "/api/burn/firmware?devices=D1",
            data={"firmware_path": "/tmp/not-found.img"},
        )

        self.assertEqual(
            response.json(),
            {"success": False, "error": "Firmware not found: /tmp/not-found.img"},
        )
        self.assertFalse(any("reboot loader" in command for command, _ in fake_ssh.commands))

    def test_firmware_chunk_upload_can_resume_without_locking_devices(self):
        lock_calls = []

        async def record_lock(**kwargs):
            lock_calls.append(kwargs)
            return ["D1"], None

        runtime.configure_runtime(lock_firmware_devices=record_lock)

        with TemporaryDirectory() as tmp, patch("features.firmware.firmware_api._FIRMWARE_CHUNK_ROOT", tmp):
            response = self.client.post(
                "/api/burn/firmware?devices=D1",
                data={
                    "chunk_index": "0",
                    "total_chunks": "2",
                    "upload_id": "upload-1",
                    "file_name": "update.img",
                    "file_size": "8",
                },
                files={"file": ("update.img", b"1234")},
            )

            self.assertEqual(response.status_code, 200)
            payload = response.json()
            self.assertTrue(payload["success"])
            self.assertFalse(payload["upload_complete"])
            self.assertEqual(payload["chunks_uploaded"], 1)
            self.assertEqual(lock_calls, [])

            resume = self.client.post(
                "/api/burn/firmware?devices=D1",
                data={
                    "check_chunks": "1",
                    "total_chunks": "2",
                    "upload_id": "upload-1",
                    "file_name": "update.img",
                    "file_size": "8",
                },
            )
            resume_payload = resume.json()
            self.assertEqual(resume_payload["uploaded_chunks"], [0])
            self.assertEqual(resume_payload["chunks_uploaded"], 1)
            self.assertEqual(resume_payload["total_chunks"], 2)
            self.assertEqual(resume_payload["progress"], 50.0)
            self.assertEqual(resume_payload["uploaded_size"], 4)
            self.assertEqual(resume_payload["total_size"], 8)
