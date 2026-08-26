"""Cluster Job lifecycle tests: leases, reservations, recovery, cancellation."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from features.auth import CurrentUser
from features.cluster import api as cluster_api
from features.cluster.repository import ClusterRepository
from features.cluster.service import ClusterService


class ClusterJobLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.repo = ClusterRepository(Path(self.temp.name) / "cluster.sqlite3")

    def tearDown(self):
        self.temp.cleanup()

    def register(self):
        return self.repo.register_worker({
            "worker_id": "worker-246", "name": "remote", "hostname": "ats-246",
            "address": "172.16.14.246", "agent_version": "1", "max_jobs": 1,
            "capabilities": {"adb": True},
        })

    def test_job_leases_device_and_releases_after_command_completion(self):
        self.register()
        self.repo.heartbeat("worker-246", {
            "agent_version": "1", "running_jobs": [], "suites": [],
            "devices": [{"serial": "ABC", "state": "available"}],
        })
        job = self.repo.create_job_with_leases({
            "worker_id": "worker-246", "owner_id": "tester",
            "devices": ["worker-246:ABC"], "suite_key": "CTS:17_r1",
        })
        self.assertEqual(job["status"], "assigned")
        self.assertEqual(self.repo.list_devices()[0]["state"], "allocated")
        command = self.repo.create_command({
            "worker_id": "worker-246", "command_type": "start_test",
            "job_id": job["id"], "attempt_id": job["current_attempt_id"],
            "payload": {},
        })
        command = self.repo.ack_command("worker-246", command["id"], {
            "status": "completed", "result": {"worker_job_id": "wj-1"}, "error": "",
        })
        self.repo.sync_job_from_command(command)
        finished = self.repo.get_job(job["id"])
        self.assertEqual(finished["status"], "completed")
        self.assertEqual(finished["leases"][0]["status"], "released")
        self.assertEqual(self.repo.list_devices()[0]["state"], "available")

        # Released leases are historical records and must not block reuse.
        second = self.repo.create_job_with_leases({
            "worker_id": "worker-246", "owner_id": "tester-2",
            "devices": ["worker-246:ABC"], "suite_key": "CTS:17_r1",
        })
        self.assertEqual(second["leases"][0]["status"], "active")
        self.assertEqual(
            second["leases"][0]["generation"],
            finished["leases"][0]["generation"] + 1,
        )

    def test_automation_reservation_survives_heartbeat_and_converts_to_job_lease(self):
        self.register()
        heartbeat = {
            "agent_version": "1", "running_jobs": [], "suites": [],
            "devices": [{"serial": "ABC", "state": "available"}],
        }
        self.repo.heartbeat("worker-246", heartbeat)
        reservation = self.repo.reserve_devices(
            "worker-246", ["worker-246:ABC"],
            owner_id="alice", source_id="ats-run-1",
        )
        reserved_device = self.repo.list_devices("worker-246")[0]
        self.assertEqual(reserved_device["state"], "reserved")
        self.assertEqual(reserved_device["claim_owner_id"], "alice")
        self.assertEqual(reserved_device["claim_username"], "alice")

        self.repo.heartbeat("worker-246", heartbeat)
        self.assertEqual(self.repo.list_devices("worker-246")[0]["state"], "reserved")
        with self.assertRaisesRegex(ValueError, "not available"):
            self.repo.reserve_devices(
                "worker-246", ["worker-246:ABC"],
                owner_id="bob", source_id="ats-run-2",
            )

        job = self.repo.create_job_with_leases({
            "worker_id": "worker-246", "owner_id": "alice",
            "devices": ["worker-246:ABC"], "suite_key": "CTS:17_r1",
            "automation_run_id": "ats-run-1",
            "device_reservation_id": reservation["id"],
        })
        self.assertEqual(job["leases"][0]["status"], "active")
        self.assertEqual(self.repo.get_reservation(reservation["id"])["status"], "converted")
        self.assertEqual(self.repo.list_devices("worker-246")[0]["state"], "allocated")

    def test_job_rejects_reservation_from_another_owner(self):
        self.register()
        self.repo.heartbeat("worker-246", {
            "agent_version": "1", "running_jobs": [], "suites": [],
            "devices": [{"serial": "ABC", "state": "available"}],
        })
        reservation = self.repo.reserve_devices(
            "worker-246", ["ABC"], owner_id="alice", source_id="ats-run-1"
        )
        with self.assertRaisesRegex(ValueError, "another owner"):
            self.repo.create_job_with_leases({
                "worker_id": "worker-246", "owner_id": "bob",
                "devices": ["ABC"], "suite_key": "CTS:17_r1",
                "automation_run_id": "ats-run-1",
                "device_reservation_id": reservation["id"],
            })

    def test_early_cancel_queues_deterministic_worker_job_id(self):
        self.register()
        self.repo.heartbeat("worker-246", {"agent_version": "1", "running_jobs": [],
            "suites": [], "devices": [{"serial": "ABC", "state": "available"}]})
        job = self.repo.create_job_with_leases({"worker_id": "worker-246", "owner_id": "tester",
            "devices": ["worker-246:ABC"], "suite_key": "CTS:17_r1"})
        previous = cluster_api.cluster_service
        cluster_api.cluster_service = ClusterService(self.repo)
        try:
            app = FastAPI()
            @app.middleware("http")
            async def identify_test_owner(request, call_next):
                request.state.current_user = CurrentUser(
                    id="tester", username="tester", role="user"
                )
                return await call_next(request)
            app.include_router(cluster_api.router)
            with TestClient(app) as client:
                response = client.post(f"/api/cluster/jobs/{job['id']}/cancel")
        finally:
            cluster_api.cluster_service = previous
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["command"]["payload"]["worker_job_id"], f"wj-{job['id']}")

    def test_running_attempt_reconciles_orphaned_lease_after_worker_returns(self):
        self.register()
        self.repo.heartbeat("worker-246", {
            "agent_version": "1", "running_jobs": [], "suites": [],
            "devices": [{"serial": "ABC", "state": "available"}],
        })
        job = self.repo.create_job_with_leases({
            "worker_id": "worker-246", "owner_id": "tester",
            "devices": ["worker-246:ABC"], "suite_key": "CTS:17_r1",
        })
        attempt_id = job["current_attempt_id"]
        self.repo.mark_worker_offline("worker-246")
        assert self.repo.get_job(job["id"])["leases"][0]["status"] == "orphaned"

        self.repo.register_worker({
            "worker_id": "worker-246", "name": "remote", "hostname": "ats-246",
            "address": "172.16.14.246", "agent_version": "1", "max_jobs": 1,
            "capabilities": {"adb": True},
        })
        self.repo.heartbeat("worker-246", {
            "agent_version": "1", "devices": [{"serial": "ABC", "state": "available"}],
            "suites": None, "running_jobs": [{"worker_job_id": "wj-1", "job_id": job["id"],
                "attempt_id": attempt_id, "status": "running", "devices": ["ABC"]}],
        })
        recovered = self.repo.get_job(job["id"])
        assert recovered["status"] == "running"
        assert recovered["error"] == ""
        assert recovered["state_version"] == 3
        assert recovered["recovery_count"] == 1
        assert recovered["leases"][0]["status"] == "active"
        assert self.repo.list_devices("worker-246")[0]["state"] == "allocated"
        transitions = [
            (item["from_state"], item["to_state"])
            for item in self.repo.list_timeline(job_id=job["id"])
            if item["event_type"] == "job.transition"
        ]
        assert transitions == [("assigned", "worker_lost"), ("worker_lost", "running")]

    def test_stale_worker_lost_job_is_failed_and_leases_released(self):
        self.register()
        self.repo.heartbeat("worker-246", {
            "agent_version": "1", "running_jobs": [], "suites": [],
            "devices": [{"serial": "ABC", "state": "available"}],
        })
        job = self.repo.create_job_with_leases({
            "worker_id": "worker-246", "owner_id": "tester",
            "devices": ["worker-246:ABC"], "suite_key": "CTS:17_r1",
        })
        self.repo.mark_worker_offline("worker-246")
        assert self.repo.get_job(job["id"])["status"] == "worker_lost"

        # 刚失联的任务在宽限期内保持 worker_lost，等待 Worker 接回。
        self.assertEqual(self.repo.fail_abandoned_worker_lost_jobs(3600), [])

        failed = self.repo.fail_abandoned_worker_lost_jobs(0)
        self.assertEqual(failed, [job["id"]])
        reloaded = self.repo.get_job(job["id"])
        self.assertEqual(reloaded["status"], "failed")
        self.assertIn("did not reconnect", reloaded["error"])
        self.assertEqual(reloaded["attempt"]["status"], "failed")
        self.assertEqual(reloaded["leases"][0]["status"], "released")
        self.assertEqual(self.repo.list_devices("worker-246")[0]["state"], "available")
        # 回收是幂等的。
        self.assertEqual(self.repo.fail_abandoned_worker_lost_jobs(0), [])
        transitions = [
            (item["from_state"], item["to_state"])
            for item in self.repo.list_timeline(job_id=job["id"])
            if item["event_type"] == "job.transition"
        ]
        assert transitions == [("assigned", "worker_lost"), ("worker_lost", "failed")]

    def test_running_attempt_is_revoked_when_unified_claim_was_lost(self):
        self.register()
        self.repo.heartbeat("worker-246", {
            "agent_version": "1", "running_jobs": [], "suites": [],
            "devices": [{"serial": "ABC", "state": "available"}],
        })
        job = self.repo.create_job_with_leases({
            "worker_id": "worker-246", "owner_id": "tester",
            "devices": ["ABC"], "suite_key": "CTS:17_r1",
        })
        attempt_id = job["current_attempt_id"]
        self.repo.mark_worker_offline("worker-246")
        self.repo.claims.release(f"job:{job['id']}", status="expired")

        response = self.repo.heartbeat("worker-246", {
            "agent_version": "1", "devices": [{"serial": "ABC", "state": "available"}],
            "suites": None, "running_jobs": [{
                "worker_job_id": "wj-old", "job_id": job["id"],
                "attempt_id": attempt_id, "status": "running",
                "devices": ["ABC"],
            }],
        })

        revoked = self.repo.get_job(job["id"])
        assert response["revoked_attempt_ids"] == [attempt_id]
        assert revoked["status"] == "failed"
        assert revoked["leases"][0]["status"] == "revoked"


if __name__ == "__main__":
    unittest.main()
