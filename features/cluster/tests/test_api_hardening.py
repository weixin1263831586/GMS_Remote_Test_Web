from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

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
        app.include_router(cluster_api.router)
        self.client = TestClient(app)
        self.env = patch.dict(
            "os.environ", {"GMS_CLUSTER_WORKER_TOKENS": "worker-246:token"}
        )
        self.env.start()

    def tearDown(self):
        self.env.stop()
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

        self.assertEqual(job.status_code, 409)
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


if __name__ == "__main__":
    unittest.main()
