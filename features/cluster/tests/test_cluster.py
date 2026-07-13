from __future__ import annotations

import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from features.cluster.repository import ClusterRepository
from features.cluster.service import ClusterService
from fastapi import FastAPI
from fastapi.testclient import TestClient
from features.cluster import api as cluster_api


class ClusterRepositoryTests(unittest.TestCase):
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

    def test_heartbeat_persists_namespaced_devices_and_suites(self):
        self.register()
        worker = self.repo.heartbeat("worker-246", {
            "agent_version": "1", "cpu_percent": 12, "memory_percent": 20,
            "disk_free_gb": 100, "running_jobs": [],
            "devices": [{"serial": "ABC", "transport": "local_usb",
                         "state": "available", "properties": {"product": "rk"}}],
            "suites": [{"suite_type": "CTS", "suite_version": "17_r1",
                        "suite_key": "CTS:17_r1", "tools_path": "/suite/tools",
                        "available": True}],
        })
        self.assertEqual(worker["status"], "online")
        device = self.repo.list_devices("worker-246")[0]
        self.assertEqual(device["id"], "worker-246:ABC")
        self.assertEqual(device["properties"]["product"], "rk")
        self.assertEqual(self.repo.list_suites("worker-246")[0]["suite_key"], "CTS:17_r1")

    def test_commands_are_delivered_once_and_acknowledged(self):
        self.register()
        command = self.repo.create_command({
            "worker_id": "worker-246", "command_type": "refresh_devices",
            "payload": {},
        })
        delivered = self.repo.poll_commands("worker-246")
        self.assertEqual([item["id"] for item in delivered], [command["id"]])
        self.assertEqual(self.repo.poll_commands("worker-246"), [])
        ack = self.repo.ack_command("worker-246", command["id"], {
            "status": "completed", "result": {"count": 3}, "error": "",
        })
        self.assertEqual(ack["status"], "completed")
        self.assertEqual(ack["result"]["count"], 3)

    def test_transfer_state_is_persisted(self):
        self.register()
        transfer = self.repo.create_transfer("worker-246")
        updated = self.repo.update_transfer(transfer["id"], status="uploading", size_bytes=123)
        self.assertEqual(updated["status"], "uploading")
        self.assertEqual(updated["size_bytes"], 123)

    def test_delete_worker_removes_inventory(self):
        self.register()
        self.repo.create_command({
            "worker_id": "worker-246", "command_type": "refresh_devices", "payload": {},
        })
        self.repo.heartbeat("worker-246", {
            "agent_version": "1", "running_jobs": [],
            "devices": [{"serial": "ABC"}],
            "suites": [{"suite_key": "CTS:17", "tools_path": "/suite/tools"}],
        })
        self.assertTrue(self.repo.delete_worker("worker-246"))
        self.assertIsNone(self.repo.get_worker("worker-246"))
        self.assertEqual(self.repo.list_devices("worker-246"), [])
        self.assertEqual(self.repo.list_suites("worker-246"), [])
        self.assertEqual(self.repo.poll_commands("worker-246"), [])

    def test_missing_devices_are_marked_offline(self):
        self.register()
        base = {"agent_version": "1", "running_jobs": [], "suites": None}
        self.repo.heartbeat("worker-246", {**base, "devices": [{"serial": "ABC"}]})
        self.repo.heartbeat("worker-246", {**base, "devices": []})
        self.assertEqual(self.repo.list_devices()[0]["state"], "offline")

    def test_service_preserves_recent_worker_status(self):
        self.register()
        workers = ClusterService(self.repo, offline_seconds=45).list_workers()
        self.assertEqual(workers[0]["status"], "online")

    def test_hosts_exposes_worker_connection_metadata(self):
        self.repo.register_worker({
            "worker_id": "worker-246", "name": "remote", "hostname": "ats-246",
            "address": "172.16.14.246", "agent_version": "1", "max_jobs": 1,
            "capabilities": {"ssh_user": "wlq"},
        })
        previous = cluster_api.cluster_service
        cluster_api.cluster_service = ClusterService(self.repo)
        try:
            app = FastAPI()
            app.include_router(cluster_api.router)
            host = TestClient(app).get("/api/cluster/hosts").json()["hosts"][0]
        finally:
            cluster_api.cluster_service = previous
        self.assertEqual(host["ssh_connection"], "wlq@172.16.14.246")

    def test_registration_uses_source_ip_when_worker_reports_hostname(self):
        previous = cluster_api.cluster_service
        cluster_api.cluster_service = ClusterService(self.repo)
        try:
            app = FastAPI()
            app.include_router(cluster_api.router)
            with patch.dict("os.environ", {"GMS_CLUSTER_WORKER_TOKENS": "worker-246:token"}):
                response = TestClient(app, client=("172.16.14.246", 50000)).post(
                    "/api/cluster/workers/register",
                    headers={"Authorization": "Bearer token"},
                    json={"worker_id": "worker-246", "hostname": "ats-043056-64g",
                          "address": "ats-043056-64g"},
                )
        finally:
            cluster_api.cluster_service = previous
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["worker"]["address"], "172.16.14.246")

    def test_runtime_single_host_mode_hides_remote_resources(self):
        self.repo.register_worker({
            "worker_id": "worker-local", "name": "local", "hostname": "controller",
            "address": "127.0.0.1", "agent_version": "1", "max_jobs": 1,
            "capabilities": {},
        })
        self.register()
        self.repo.heartbeat("worker-local", {
            "agent_version": "1", "running_jobs": [],
            "devices": [{"serial": "LOCAL", "state": "available"}], "suites": [],
        })
        self.repo.heartbeat("worker-246", {
            "agent_version": "1", "running_jobs": [],
            "devices": [{"serial": "REMOTE", "state": "available"}], "suites": [],
        })
        previous = cluster_api.cluster_service
        svc = ClusterService(self.repo)
        svc.set_runtime_enabled(False)
        cluster_api.cluster_service = svc
        try:
            app = FastAPI()
            app.include_router(cluster_api.router)
            client = TestClient(app)
            self.assertFalse(client.get("/api/cluster/status").json()["enabled"])
            self.assertEqual(
                [item["worker_id"] for item in client.get("/api/cluster/hosts").json()["hosts"]],
                ["worker-local"],
            )
            self.assertEqual(
                [item["id"] for item in client.get("/api/cluster/devices").json()["devices"]],
                ["worker-local:LOCAL"],
            )
            self.assertEqual(client.get("/api/cluster/devices?worker_id=worker-246").status_code, 409)
        finally:
            cluster_api.cluster_service = previous

    def test_scheduler_selects_worker_with_suite_and_available_device(self):
        self.register()
        self.repo.heartbeat("worker-246", {
            "agent_version": "1", "running_jobs": [],
            "devices": [{"serial": "ABC", "state": "available"}],
            "suites": [{"suite_type": "CTS", "suite_version": "17_r1",
                        "suite_key": "CTS:17_r1", "tools_path": "/suite/tools",
                        "available": True}],
        })
        worker_id, devices = ClusterService(self.repo).select_worker("CTS:17_r1", 1)
        self.assertEqual(worker_id, "worker-246")
        self.assertEqual(devices, ["worker-246:ABC"])

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
            app.include_router(cluster_api.router)
            response = TestClient(app).post(f"/api/cluster/jobs/{job['id']}/cancel")
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
        assert recovered["leases"][0]["status"] == "active"
        assert self.repo.list_devices("worker-246")[0]["state"] == "allocated"


if __name__ == "__main__":
    unittest.main()
