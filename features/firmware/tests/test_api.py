import threading
import unittest
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from features.firmware import api, runtime


class FirmwareApiTests(unittest.TestCase):
    def setUp(self):
        runtime.configure_runtime(
            global_state=SimpleNamespace(
                apk_analysis_tasks={},
                apk_analysis_tasks_lock=threading.RLock(),
            ),
            generate_help_or_continue=lambda help, *_args: (
                JSONResponse({"help": True}) if help else None
            ),
        )
        app = FastAPI()
        app.include_router(api.router)
        self.client = TestClient(app)

    def test_firmware_help_does_not_execute_host_command(self):
        response = self.client.post("/api/burn/firmware?help=true")

        self.assertEqual(response.status_code, 200)

    def test_apk_missing_task_returns_not_found(self):
        response = self.client.get("/api/apk/status/missing-task")

        self.assertEqual(response.status_code, 404)
        self.assertFalse(response.json()["success"])
