from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from features.auth import CurrentUser
from features.test_execution import logs_api
from features.test_execution.logs import TestLogsManager as GmsTestLogsManager


class TestLogAccessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.manager = GmsTestLogsManager()
        self.manager.saved_logs_dir = root / "saved"
        self.manager.downloads_dir = root / "downloads"
        self.manager.log_dirs = [root]
        self.user_states: dict[str, dict] = {}
        self.global_state = SimpleNamespace(last_saved_log_file={})

        app = FastAPI()

        @app.middleware("http")
        async def test_identity(request: Request, call_next):
            username = request.headers.get("X-Test-User", "alice")
            request.state.current_user = CurrentUser(
                id=f"id-{username}",
                username=username,
                role=request.headers.get("X-Test-Role", "user"),
            )
            return await call_next(request)

        app.include_router(logs_api.router)
        self.patches = [
            patch.object(logs_api, "test_logs_manager", self.manager),
            patch.object(logs_api.runtime, "global_state", self.global_state),
            patch.object(
                logs_api.runtime,
                "get_client_id_from_request",
                side_effect=lambda request: request.state.current_user.id,
            ),
            patch.object(
                logs_api,
                "get_or_create_user_state",
                side_effect=lambda owner: self.user_states.setdefault(owner, {}),
            ),
            patch.object(
                logs_api,
                "update_user_state_field",
                side_effect=lambda owner, values: self.user_states.setdefault(owner, {}).update(values),
            ),
        ]
        for item in self.patches:
            item.start()
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        for item in reversed(self.patches):
            item.stop()
        self.tmp.cleanup()

    def _save(self, username: str, content: str, **extra):
        return self.client.post(
            "/api/test/logs/save",
            headers={"X-Test-User": username},
            json={"content": content, **extra},
        )

    def test_save_ignores_spoofed_client_id_and_lists_only_owner(self):
        alice = self._save("alice", "alice-secret", client_id="bob").json()
        bob = self._save("bob", "bob-secret").json()

        alice_list = self.client.get("/api/test/logs/list").json()
        admin_list = self.client.get(
            "/api/test/logs/list",
            headers={"X-Test-User": "admin", "X-Test-Role": "admin"},
        ).json()

        self.assertTrue(alice["log_id"].startswith("id-alice/"))
        self.assertNotIn("id-bob/", alice["log_id"])
        self.assertEqual([item["name"] for item in alice_list["files"]], [alice["filename"]])
        self.assertEqual(
            {item["name"] for item in admin_list["files"]},
            {alice["filename"], bob["filename"]},
        )

    def test_batch_download_rejects_another_owners_path(self):
        bob = self._save("bob", "bob-secret").json()

        response = self.client.post(
            "/api/test/logs/batch",
            json={"log_ids": [bob["log_id"]]},
        )

        self.assertEqual(response.status_code, 404)

    def test_list_and_save_do_not_expose_server_paths(self):
        saved = self._save("alice", "secret").json()
        listed = self.client.get("/api/test/logs/list").json()["files"]

        self.assertNotIn("log_file", saved)
        self.assertNotIn("path", listed[0])
        self.assertEqual(listed[0]["id"], saved["log_id"])

    def test_router_rejects_anonymous_access_without_global_middleware(self):
        app = FastAPI()
        app.include_router(logs_api.router)
        with patch("features.auth.access.authentication_required", return_value=True):
            with TestClient(app) as anonymous:
                response = anonymous.get("/api/test/logs/list")
        self.assertEqual(response.status_code, 401)

    def test_get_does_not_fall_back_to_unowned_global_latest_log(self):
        global_log = Path(self.tmp.name) / "global-secret.log"
        global_log.write_text("global-secret", encoding="utf-8")

        response = self.client.get("/api/test/logs/get")

        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
