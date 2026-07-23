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


class ClusterApiHardeningTests(unittest.TestCase):
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
            return await call_next(request)

        app.include_router(cluster_api.router)
        self.client = TestClient(app)
        self.tokens_path = Path(self.temp.name) / "cluster.json"
        self.tokens_path.write_text(
            json.dumps({"worker_tokens": {"worker-246": "token"}}),
            encoding="utf-8",
        )
        self.env = patch.dict(
            "os.environ", {"GMS_CLUSTER_CONFIG": str(self.tokens_path)}
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

    def test_oversized_artifact_is_rejected_without_partial_file(self):
        job = self.repo.create_job_with_leases({
            "worker_id": "worker-246",
            "owner_id": "tester",
            "devices": ["worker-246:ABC"],
            "suite_key": "CTS:17_r1",
        })
        endpoint = (
            f"/api/cluster/jobs/{job['id']}/artifacts/stdout.log"
            f"?attempt_id={job['current_attempt_id']}"
        )
        with patch.dict("os.environ", {"GMS_CLUSTER_ARTIFACT_MAX_BYTES": "4"}):
            response = self.client.put(
                endpoint,
                headers=self.worker_headers(),
                content=b"12345",
            )

        self.assertEqual(response.status_code, 413)
        artifact_path = (
            self.repo.db_path.parent
            / "artifacts"
            / job["id"]
            / job["current_attempt_id"]
            / "stdout.log"
        )
        self.assertFalse(artifact_path.exists())

    def test_oversized_gsi_is_rejected_and_staging_is_removed(self):
        with patch.dict("os.environ", {"GMS_CLUSTER_FIRMWARE_MAX_BYTES": "3"}):
            response = self.client.post(
                "/api/cluster/gsi/stage",
                data={"worker_id": "worker-246", "devices": "ABC"},
                files={"system_file": ("system.img", b"1234")},
            )

        self.assertEqual(response.status_code, 413)
        firmware_root = self.repo.db_path.parent / "firmware"
        self.assertEqual(list(firmware_root.iterdir()), [])

    def test_terminal_flash_ack_removes_controller_staging(self):
        stage_id = "fw-" + "a" * 32
        stage_dir = self.repo.db_path.parent / "firmware" / stage_id
        stage_dir.mkdir(parents=True)
        (stage_dir / "update.img").write_bytes(b"firmware")
        command = self.repo.create_command({
            "worker_id": "worker-246",
            "command_type": "flash_firmware",
            "payload": {"stage_id": stage_id},
        })

        response = self.client.post(
            f"/api/cluster/workers/worker-246/commands/{command['id']}/ack",
            headers={"Authorization": "Bearer token"},
            json={"status": "completed", "result": {}, "error": ""},
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(stage_dir.exists())

    def test_manual_firmware_claim_is_released_by_terminal_ack(self):
        staged = self.client.post(
            "/api/cluster/firmware/stage",
            data={"worker_id": "worker-246", "devices": "ABC"},
            files={"firmware_file": ("update.img", b"firmware")},
        )
        self.assertEqual(staged.status_code, 200)
        command = self.repo.get_command(staged.json()["command_id"])
        claim_source = command["payload"]["claim_source_id"]
        self.assertIsNotNone(self.repo.claims.active_claim("worker-246:ABC"))

        acknowledged = self.client.post(
            f"/api/cluster/workers/worker-246/commands/{command['id']}/ack",
            headers={"Authorization": "Bearer token"},
            json={"status": "completed", "result": {}, "error": ""},
        )

        self.assertEqual(acknowledged.status_code, 200)
        self.assertEqual(self.repo.claims.renew(claim_source, 60), 0)

    def test_device_action_cannot_bypass_active_job_claim(self):
        self.repo.create_job_with_leases({
            "worker_id": "worker-246",
            "owner_id": "tester",
            "devices": ["ABC"],
            "suite_key": "CTS:17_r1",
        })

        response = self.client.post(
            "/api/cluster/devices/actions",
            json={
                "worker_id": "worker-246",
                "devices": ["ABC"],
                "action": "reboot",
            },
        )

        self.assertEqual(response.status_code, 409)
        self.assertIn("already claimed", response.json()["detail"])

    def test_agent_backed_endpoints_reject_controller_local_worker(self):
        self.repo.register_worker({
            "worker_id": "worker-local",
            "name": "controller",
            "hostname": "controller",
            "address": "127.0.0.1",
            "agent_version": "controller-local",
            "max_jobs": 1,
            "capabilities": {},
        })

        job = self.client.post(
            "/api/cluster/jobs",
            json={"worker_id": "worker-local", "suite_key": "CTS:17_r1"},
        )
        export = self.client.post(
            "/api/cluster/suites/export",
            params={
                "worker_id": "worker-local",
                "suite_path": "/tmp/suite",
                "path": "results",
            },
        )

        self.assertEqual(job.status_code, 503)
        self.assertEqual(export.status_code, 409)

        self.repo.register_worker({
            "worker_id": "worker-local",
            "name": "local-agent",
            "hostname": "controller",
            "address": "127.0.0.1",
            "agent_version": "0.2.0",
            "max_jobs": 1,
            "capabilities": {},
        })
        accepted = self.client.post(
            "/api/cluster/suites/export",
            params={
                "worker_id": "worker-local",
                "suite_path": "/tmp/suite",
                "path": "results",
            },
        )
        self.assertEqual(accepted.status_code, 200)

    def test_worker_with_external_test_cannot_be_deleted(self):
        self.repo.heartbeat("worker-246", {
            "agent_version": "1",
            "running_jobs": [{
                "worker_job_id": "external-123",
                "source": "external",
                "status": "running",
                "devices": ["ABC"],
            }],
            "devices": [{"serial": "ABC", "state": "available"}],
            "suites": [],
        })

        response = self.client.delete("/api/cluster/workers/worker-246")

        self.assertEqual(response.status_code, 409)
        self.assertIsNotNone(self.repo.get_worker("worker-246"))

    def test_busy_worker_configuration_cannot_change(self):
        self.repo.heartbeat("worker-246", {
            "agent_version": "1",
            "running_jobs": [{
                "worker_job_id": "external-123",
                "source": "external",
                "status": "running",
                "devices": ["ABC"],
            }],
            "devices": [{"serial": "ABC", "state": "available"}],
            "suites": [],
        })

        response = self.client.post(
            "/api/cluster/workers/worker-246/config",
            json={"max_jobs": 4},
        )

        self.assertEqual(response.status_code, 409)
        self.assertIn("tests are running", response.json()["detail"])

    def test_regular_user_cannot_delete_worker(self):
        with patch("features.auth.access.authentication_required", return_value=True):
            response = self.client.delete(
                "/api/cluster/workers/worker-246",
                headers={"X-Test-User": "alice", "X-Test-Role": "user"},
            )

        self.assertEqual(response.status_code, 403)
        self.assertIsNotNone(self.repo.get_worker("worker-246"))

    def test_job_owner_is_server_principal_and_cross_user_id_is_hidden(self):
        created = self.client.post(
            "/api/cluster/jobs",
            headers={"X-Test-User": "alice", "X-Test-Role": "user"},
            json={
                "worker_id": "worker-246",
                "suite_key": "CTS:17_r1",
                "devices": ["ABC"],
                "argv": ["/bin/true"],
                "owner_id": "bob-id",
            },
        )
        self.assertEqual(created.status_code, 200)
        job = created.json()["job"]
        self.assertEqual(job["owner_id"], "alice-id")

        hidden = self.client.get(
            f"/api/cluster/jobs/{job['id']}",
            headers={"X-Test-User": "bob", "X-Test-Role": "user"},
        )
        self.assertEqual(hidden.status_code, 404)

        own = self.client.get(
            f"/api/cluster/jobs/{job['id']}",
            headers={"X-Test-User": "alice", "X-Test-Role": "user"},
        )
        self.assertEqual(own.status_code, 200)


if __name__ == "__main__":
    unittest.main()
