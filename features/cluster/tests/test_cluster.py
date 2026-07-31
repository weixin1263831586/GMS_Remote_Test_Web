from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from features.auth import CurrentUser
from features.cluster import api as cluster_api
from features.cluster import commands_api
from features.cluster.config import ClusterConfig
from features.cluster.repository import ClusterRepository
from features.cluster.service import ClusterService


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

    def test_recreates_schema_after_runtime_data_directory_is_deleted(self):
        self.register()
        shutil.rmtree(self.temp.name)
        self.assertEqual(self.repo.list_workers(), [])
        with sqlite3.connect(self.repo.db_path) as conn:
            tables = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()}
        self.assertTrue(self.repo._REQUIRED_TABLES.issubset(tables))
        acquired, claims = self.repo.claims.acquire(
            [{"device_key": "worker-local:ABC", "worker_id": "worker-local", "serial": "ABC"}],
            owner_id="alice",
            username="Alice",
            source_type="test",
            source_id="test:runtime-recovery",
            ttl_seconds=90,
        )
        self.assertTrue(acquired)
        self.assertEqual(claims[0]["device_key"], "worker-local:ABC")

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

    def test_heartbeat_updates_transport_for_same_device_id(self):
        self.register()
        base = {
            "agent_version": "1",
            "running_jobs": [],
            "suites": [],
        }
        self.repo.heartbeat("worker-246", {
            **base,
            "devices": [{
                "serial": "ABC",
                "transport": "adb_proxy",
                "state": "available",
                "properties": {"adb_proxy_source_worker_id": "worker-local"},
            }],
        })

        worker = self.repo.heartbeat("worker-246", {
            **base,
            "devices": [{
                "serial": "ABC",
                "transport": "local_usb",
                "state": "available",
                "properties": {"usb": "2-1"},
            }],
        })

        self.assertEqual(worker["status"], "online")
        devices = self.repo.list_devices("worker-246")
        self.assertEqual(len(devices), 1)
        self.assertEqual(devices[0]["id"], "worker-246:ABC")
        self.assertEqual(devices[0]["transport"], "local_usb")
        self.assertEqual(devices[0]["properties"], {"usb": "2-1"})

    def test_device_api_resolves_claim_owner_without_exposing_internal_id(self):
        self.register()
        self.repo.heartbeat("worker-246", {
            "agent_version": "1",
            "running_jobs": [],
            "suites": [],
            "devices": [{
                "serial": "ABC",
                "transport": "local_usb",
                "state": "available",
            }],
        })
        acquired, _claims = self.repo.claims.acquire(
            [{
                "device_key": "worker-246:ABC",
                "worker_id": "worker-246",
                "serial": "ABC",
            }],
            owner_id="N387pLbIBhpMw5JsWUL9hg",
            username="N387pLbIBhpMw5JsWUL9hg",
            source_type="cluster-job",
            source_id="job:1",
            ttl_seconds=60,
        )
        self.assertTrue(acquired)
        previous = cluster_api.cluster_service
        cluster_api.cluster_service = ClusterService(self.repo)
        try:
            app = FastAPI()
            app.include_router(cluster_api.router)
            with patch(
                "features.users.clients.resolve_client_display_id",
                return_value="hcq@172.16.14.66",
            ), TestClient(app) as client:
                response = client.get(
                    "/api/cluster/devices?worker_id=worker-246"
                )
        finally:
            cluster_api.cluster_service = previous

        self.assertEqual(response.status_code, 200, response.text)
        device = response.json()["devices"][0]
        self.assertEqual(device["claimed_by"], "hcq@172.16.14.66")
        self.assertNotIn("claim_owner_id", device)
        self.assertNotIn("claim_username", device)

    def test_suite_api_includes_cluster_inventory_display_fields(self):
        self.register()
        self.repo.heartbeat("worker-246", {
            "agent_version": "1",
            "running_jobs": [],
            "devices": [],
            "suites": [{
                "suite_type": "CTS",
                "suite_version": "17_r1",
                "suite_key": "CTS:17_r1",
                "tools_path": "/suite/tools",
                "available": True,
            }],
        })
        previous = cluster_api.cluster_service
        cluster_api.cluster_service = ClusterService(self.repo)
        try:
            app = FastAPI()
            app.include_router(cluster_api.router)
            with TestClient(app) as client:
                response = client.get("/api/cluster/suites?worker_id=worker-246")
        finally:
            cluster_api.cluster_service = previous

        self.assertEqual(response.status_code, 200, response.text)
        suite = response.json()["suites"][0]
        self.assertEqual(suite["worker_id"], "worker-246")
        self.assertEqual(suite["suite_type"], "CTS")
        self.assertEqual(suite["suite_version"], "17_r1")
        self.assertEqual(suite["test_type"], "cts")
        self.assertEqual(suite["version"], "android-cts-17_r1")

    def test_refresh_command_result_restores_suite_inventory(self):
        self.register()
        command = self.repo.create_command({
            "worker_id": "worker-246",
            "command_type": "refresh_suites",
            "payload": {},
        })
        command = self.repo.ack_command("worker-246", command["id"], {
            "status": "completed",
            "result": {"suites": [{
                "suite_type": "CTS",
                "suite_version": "17_r1",
                "suite_key": "CTS:17_r1",
                "tools_path": "/suite/tools",
                "available": True,
            }]},
            "error": "",
        })
        previous = cluster_api.cluster_service
        cluster_api.cluster_service = ClusterService(self.repo)
        try:
            commands_api.synchronize_command(command)
        finally:
            cluster_api.cluster_service = previous
        self.assertEqual(
            self.repo.list_suites("worker-246")[0]["suite_key"], "CTS:17_r1"
        )

    def test_registration_without_inventory_queues_suite_refresh(self):
        previous = cluster_api.cluster_service
        cluster_api.cluster_service = ClusterService(self.repo)
        try:
            app = FastAPI()
            app.include_router(cluster_api.router)
            tokens_path = Path(self.temp.name) / "cluster.json"
            tokens_path.write_text(
                json.dumps({"worker_tokens": {"worker-246": "token"}}),
                encoding="utf-8",
            )
            with patch.dict(
                "os.environ", {"GMS_WORKER_TOKENS_FILE": str(tokens_path)}
            ), TestClient(app) as client:
                response = client.post(
                    "/api/cluster/workers/register",
                    headers={"Authorization": "Bearer token"},
                    json={
                        "worker_id": "worker-246",
                        "hostname": "ats-246",
                        "address": "172.16.14.246",
                    },
                )
        finally:
            cluster_api.cluster_service = previous
        self.assertEqual(response.status_code, 200, response.text)
        queued = self.repo.poll_commands("worker-246")
        self.assertEqual([item["command_type"] for item in queued], ["refresh_suites"])

    def test_suite_results_allows_anonymous_development_mode(self):
        previous = cluster_api.cluster_service
        cluster_api.cluster_service = ClusterService(self.repo)
        try:
            app = FastAPI()
            app.include_router(cluster_api.router)
            with patch.dict(
                "os.environ",
                {"GMS_ENV": "development", "GMS_AUTH_REQUIRED": "false"},
            ), patch(
                "features.cluster.api._run_worker_command",
                new=AsyncMock(return_value={"raw_output": "", "launcher": "vts-tradefed"}),
            ), TestClient(app) as client:
                response = client.post(
                    "/api/cluster/suites/results",
                    params={"worker_id": "worker-246", "suite_path": "/suite/tools"},
                )
        finally:
            cluster_api.cluster_service = previous

        self.assertEqual(response.status_code, 200, response.text)

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

    def test_operation_id_deduplicates_command_and_records_timeline(self):
        self.register()
        self.repo.heartbeat("worker-246", {
            "agent_version": "1", "running_jobs": [], "suites": [],
            "devices": [{"serial": "ABC", "state": "available"}],
        })
        job = self.repo.create_job_with_leases({
            "worker_id": "worker-246", "owner_id": "tester",
            "devices": ["ABC"], "suite_key": "CTS:17_r1",
            "automation_run_id": "ats-trace-1",
        })
        request = {
            "worker_id": "worker-246", "command_type": "start_test",
            "job_id": job["id"], "attempt_id": job["current_attempt_id"],
            "operation_id": f"{job['current_attempt_id']}:start_test",
            "payload": {},
        }
        command = self.repo.create_command(request)
        duplicate = self.repo.create_command(request)
        self.assertEqual(duplicate["id"], command["id"])
        self.repo.attach_command_to_job(job["id"], command)
        running = self.repo.ack_command("worker-246", command["id"], {
            "status": "running", "result": {"worker_job_id": "wj-1"}, "error": "",
        })
        self.repo.sync_job_from_command(running)

        current = self.repo.get_job(job["id"])
        self.assertEqual(current["trace_id"], "ats-trace-1")
        self.assertEqual(current["state_version"], 3)
        timeline = self.repo.list_timeline(job_id=job["id"])
        self.assertEqual(
            [item["event_type"] for item in timeline],
            ["job.created", "command.queued", "job.transition",
             "command.acknowledged", "job.transition"],
        )

    def test_worker_session_generation_rejects_stale_agent(self):
        first = self.repo.register_worker({
            "worker_id": "worker-246", "name": "remote", "hostname": "ats-246",
            "address": "172.16.14.246", "agent_version": "1", "max_jobs": 1,
            "session_id": "session-a", "capabilities": {},
        })
        self.assertEqual(first["connection_generation"], 1)
        same = self.repo.register_worker({
            "worker_id": "worker-246", "name": "remote", "hostname": "ats-246",
            "address": "172.16.14.246", "agent_version": "1", "max_jobs": 1,
            "session_id": "session-a", "capabilities": {},
        })
        self.assertEqual(same["connection_generation"], 1)
        replacement = self.repo.register_worker({
            "worker_id": "worker-246", "name": "remote", "hostname": "ats-246",
            "address": "172.16.14.246", "agent_version": "1", "max_jobs": 1,
            "session_id": "session-b", "capabilities": {},
        })
        self.assertEqual(replacement["connection_generation"], 2)
        self.assertFalse(self.repo.validate_worker_session("worker-246", "session-a", 1))
        self.assertTrue(self.repo.validate_worker_session("worker-246", "session-b", 2))
        with self.assertRaisesRegex(ValueError, "stale worker session"):
            self.repo.heartbeat("worker-246", {
                "session_id": "session-a", "connection_generation": 1,
                "running_jobs": [], "devices": [], "suites": None,
            })

    def test_unacknowledged_command_is_redelivered_after_delivery_lease(self):
        self.register()
        command = self.repo.create_command({
            "worker_id": "worker-246", "command_type": "refresh_devices",
            "payload": {},
        })
        self.assertEqual(len(self.repo.poll_commands("worker-246")), 1)

        with self.repo.connect() as conn:
            conn.execute(
                "UPDATE cluster_commands SET delivered_at='2000-01-01T00:00:00Z' WHERE id=?",
                (command["id"],),
            )

        redelivered = self.repo.poll_commands("worker-246")
        self.assertEqual([item["id"] for item in redelivered], [command["id"]])

    def test_late_ack_cannot_regress_terminal_command(self):
        self.register()
        command = self.repo.create_command({
            "worker_id": "worker-246", "command_type": "refresh_devices",
            "payload": {},
        })
        completed = self.repo.ack_command("worker-246", command["id"], {
            "status": "completed", "result": {"count": 1}, "error": "",
        })
        late = self.repo.ack_command("worker-246", command["id"], {
            "status": "running", "result": {}, "error": "",
        })

        self.assertEqual(completed["status"], "completed")
        self.assertEqual(late["status"], "completed")
        self.assertEqual(late["result"], {"count": 1})

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

            @app.middleware("http")
            async def tester_identity(request, call_next):
                request.state.current_user = CurrentUser(
                    id="tester", username="tester", role="user"
                )
                return await call_next(request)

            app.include_router(cluster_api.router)
            with TestClient(app) as client:
                host = client.get("/api/cluster/hosts").json()["hosts"][0]
        finally:
            cluster_api.cluster_service = previous
        self.assertEqual(host["ssh_connection"], "wlq@172.16.14.246")

    def test_registration_uses_source_ip_when_worker_reports_hostname(self):
        previous = cluster_api.cluster_service
        cluster_api.cluster_service = ClusterService(self.repo)
        try:
            app = FastAPI()
            app.include_router(cluster_api.router)
            tokens_path = Path(self.temp.name) / "cluster.json"
            tokens_path.write_text(
                json.dumps({"worker_tokens": {"worker-246": "token"}}),
                encoding="utf-8",
            )
            with patch.dict("os.environ", {"GMS_WORKER_TOKENS_FILE": str(tokens_path)}), TestClient(
                app,
                client=("172.16.14.246", 50000),
            ) as client:
                response = client.post(
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
        svc = ClusterService(self.repo, config=ClusterConfig(enabled=False))
        cluster_api.cluster_service = svc
        try:
            app = FastAPI()
            app.include_router(cluster_api.router)
            with TestClient(app) as client:
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

    def test_scheduler_can_exclude_adb_proxy_devices(self):
        self.register()
        self.repo.heartbeat("worker-246", {
            "agent_version": "1", "running_jobs": [],
            "devices": [
                {"serial": "PROXY", "state": "available", "transport": "adb_proxy"},
                {"serial": "USB", "state": "available", "transport": "usbip"},
            ],
            "suites": [{
                "suite_type": "CTS", "suite_version": "17_r1",
                "suite_key": "CTS:17_r1", "tools_path": "/suite/tools",
                "available": True,
            }],
        })

        worker_id, devices = ClusterService(self.repo).select_worker(
            "CTS:17_r1", 1, excluded_transports={"adb_proxy"}
        )

        self.assertEqual(worker_id, "worker-246")
        self.assertEqual(devices, ["worker-246:USB"])

    def test_scheduler_can_exclude_controller_local_worker(self):
        for worker_id in ("worker-local", "worker-246"):
            self.repo.register_worker({
                "worker_id": worker_id,
                "name": worker_id,
                "hostname": worker_id,
                "address": "127.0.0.1",
                "agent_version": "1",
                "max_jobs": 1,
                "capabilities": {},
            })
            self.repo.heartbeat(worker_id, {
                "agent_version": "1",
                "running_jobs": [],
                "devices": [{"serial": worker_id, "state": "available"}],
                "suites": [{
                    "suite_type": "CTS",
                    "suite_version": "17_r1",
                    "suite_key": "CTS:17_r1",
                    "tools_path": f"/{worker_id}/cts/tools",
                    "available": True,
                }],
            })

        worker_id, devices = ClusterService(self.repo).select_worker(
            "CTS:17_r1",
            include_local=False,
        )

        self.assertEqual(worker_id, "worker-246")
        self.assertEqual(devices, ["worker-246:worker-246"])

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
