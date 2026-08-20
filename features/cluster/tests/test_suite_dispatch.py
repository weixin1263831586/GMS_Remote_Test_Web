"""Cluster suite dispatch/extract API hardening tests.

Split from test_api_hardening.py to keep modules reviewable
(see tests/architecture/test_file_size_rules.py limits).
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from features.auth import CurrentUser
from features.cluster import api as cluster_api
from features.cluster.repository import ClusterRepository
from features.cluster.service import ClusterService


class _ClusterApiTestBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.repo = ClusterRepository(Path(self.temp.name) / "cluster.sqlite3")
        self.repo.register_worker({
            "worker_id": "worker-246",
            "name": "remote",
            "hostname": "ats-246",
            "address": "172.16.14.246",
            "agent_version": "1",
            "max_jobs": 1,
            "capabilities": {"adb": True},
        })
        self.repo.heartbeat("worker-246", {
            "agent_version": "1",
            "running_jobs": [],
            "devices": [{"serial": "ABC", "state": "available"}],
            "suites": [],
        })
        self.previous_service = cluster_api.cluster_service
        cluster_api.cluster_service = ClusterService(self.repo)
        app = FastAPI()

        @app.middleware("http")
        async def admin_identity(request: Request, call_next):
            username = request.headers.get("X-Test-User", "admin")
            request.state.current_user = CurrentUser(
                id=f"{username}-id",
                username=username,
                role=request.headers.get("X-Test-Role", "admin"),
            )
            if request.headers.get("X-Test-Elevated"):
                request.state.is_elevated = True
            return await call_next(request)

        app.include_router(cluster_api.router)
        self.client = TestClient(app)
        self.tokens_path = Path(self.temp.name) / "cluster.json"
        self.tokens_path.write_text(
            json.dumps({"worker_tokens": {"worker-246": "token"}}),
            encoding="utf-8",
        )
        self.env = patch.dict(
            "os.environ", {"GMS_WORKER_TOKENS_FILE": str(self.tokens_path)}
        )
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.client.close()
        cluster_api.cluster_service = self.previous_service
        self.temp.cleanup()

    def worker_headers(self):
        return {
            "Authorization": "Bearer token",
            "X-GMS-Worker-ID": "worker-246",
        }


class SuiteDispatchTests(_ClusterApiTestBase):
    def test_suite_command_owner_can_poll_after_elevation_expires(self):
        created = self.client.post(
            "/api/cluster/suites/download",
            headers={"X-Test-User": "alice", "X-Test-Role": "user",
                     "X-Test-Elevated": "1"},
            json={"worker_id": "worker-246",
                  "url": "https://example.com/android-cts.zip",
                  "filename": "android-cts.zip", "size_bytes": 1},
        )
        self.assertEqual(created.status_code, 200, created.text)
        command_id = created.json()["command_id"]

        elevated_poll = self.client.get(
            f"/api/cluster/commands/{command_id}",
            headers={"X-Test-User": "alice", "X-Test-Role": "user",
                     "X-Test-Elevated": "1"},
        )
        self.assertEqual(elevated_poll.status_code, 200, elevated_poll.text)

        owner_poll = self.client.get(
            f"/api/cluster/commands/{command_id}",
            headers={"X-Test-User": "alice", "X-Test-Role": "user"},
        )
        self.assertEqual(owner_poll.status_code, 200, owner_poll.text)

        foreign_poll = self.client.get(
            f"/api/cluster/commands/{command_id}",
            headers={"X-Test-User": "bob", "X-Test-Role": "user"},
        )
        self.assertEqual(foreign_poll.status_code, 404)

    def test_suite_download_rejects_incomplete_library_archive(self):
        """残缺压缩包（缺 EOCD 的 ZIP）不得下发到 Worker。"""
        import zipfile as zipfile_module

        with patch("features.cluster.api._require_cluster_enabled") as require_enabled, \
             tempfile.TemporaryDirectory() as suite_dir:
            archive_path = Path(suite_dir) / "android-cts-17_r1.zip"
            with zipfile_module.ZipFile(archive_path, "w") as bundle:
                bundle.writestr("android-cts/tools/cts-tradefed", "probe")
            data = archive_path.read_bytes()
            archive_path.write_bytes(data[: data.index(b"PK\x05\x06")] or data[:1])
            with patch(
                "features.cluster.suite_library_api.controller_suite_archives",
                return_value=[archive_path],
            ):
                response = self.client.post(
                    "/api/cluster/suites/download",
                    headers={"X-Test-User": "admin", "X-Test-Role": "admin"},
                    json={"worker_id": "worker-246",
                          "url": "https://example.com/android-cts-17_r1.zip",
                          "filename": "android-cts-17_r1.zip",
                          "size_bytes": archive_path.stat().st_size},
                )
        require_enabled.assert_called_once()
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn("incomplete or corrupted", response.text)

    def test_suite_action_terminal_ack_notifies_owner(self):
        failed = self.repo.create_command({
            "worker_id": "worker-246",
            "command_type": "suite_action",
            "payload": {"action": "download_url", "filename": "android-cts.zip",
                        "owner_id": "alice-id"},
        })
        completed = self.repo.create_command({
            "worker_id": "worker-246",
            "command_type": "suite_action",
            "payload": {"action": "extract", "target_dir_name": "android-cts",
                        "owner_id": "alice-id"},
        })
        with patch("features.system.queue_notification") as queued:
            for command, status, error in (
                (failed, "failed", "disk full"),
                (completed, "completed", ""),
            ):
                response = self.client.post(
                    f"/api/cluster/workers/worker-246/commands/{command['id']}/ack",
                    headers={"Authorization": "Bearer token"},
                    json={"status": status, "result": {}, "error": error},
                )
                self.assertEqual(response.status_code, 200)

            self.assertEqual(queued.call_count, 2)
            failure_args = queued.call_args_list[0][0]
            success_args = queued.call_args_list[1][0]

        self.assertEqual(failure_args[0], "alice-id")
        self.assertIn("下发失败", failure_args[1])
        self.assertIn("disk full", failure_args[2])
        self.assertEqual(failure_args[3], "error")
        self.assertEqual(failure_args[4], "cluster")

        self.assertEqual(success_args[0], "alice-id")
        self.assertIn("解压完成", success_args[1])
        self.assertEqual(success_args[3], "success")

        with patch("features.system.queue_notification") as queued:
            response = self.client.post(
                f"/api/cluster/workers/worker-246/commands/{completed['id']}/ack",
                headers={"Authorization": "Bearer token"},
                json={"status": "completed", "result": {}, "error": ""},
            )
            self.assertEqual(response.status_code, 200)
            queued.assert_not_called()

        unowned = self.repo.create_command({
            "worker_id": "worker-246",
            "command_type": "suite_action",
            "payload": {"action": "extract", "target_dir_name": "android-cts"},
        })
        with patch("features.system.queue_notification") as queued:
            response = self.client.post(
                f"/api/cluster/workers/worker-246/commands/{unowned['id']}/ack",
                headers={"Authorization": "Bearer token"},
                json={"status": "completed", "result": {}, "error": ""},
            )
            self.assertEqual(response.status_code, 200)
            queued.assert_not_called()


if __name__ == "__main__":
    unittest.main()
