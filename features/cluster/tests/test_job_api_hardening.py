"""Hardening tests for the Cluster Job creation and access endpoints."""

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


class ClusterJobApiHardeningTests(unittest.TestCase):
    def _idempotent_job_payload(self):
        self.repo.heartbeat("worker-246", {
            "agent_version": "1", "running_jobs": [],
            "devices": [{"serial": "ABC", "state": "available"}],
            "suites": [{
                "suite_type": "CTS", "suite_version": "17_r1",
                "suite_key": "CTS:17_r1", "available": True,
                "tools_path": "/srv/GMS-Suite/android-cts/tools",
            }],
        })
        return {"worker_id": "auto", "suite_key": "CTS:17_r1", "devices": ["ABC"]}

    def test_job_retry_replays_original_worker_and_single_command(self):
        payload = self._idempotent_job_payload()
        headers = {"Idempotency-Key": "retry-1"}
        with patch.object(cluster_api.cluster_service, "has_command_agent", return_value=True):
            first = self.client.post("/api/cluster/jobs", json=payload, headers=headers)
        self.assertEqual(first.status_code, 200, first.text)
        self.repo.mark_worker_offline("worker-246")
        replay = self.client.post("/api/cluster/jobs", json=payload, headers=headers)
        self.assertEqual(replay.status_code, 200, replay.text)
        self.assertEqual(replay.json()["job"]["id"], first.json()["job"]["id"])
        self.assertEqual(replay.json()["command"]["id"], first.json()["command"]["id"])
        with self.repo.connect() as conn:
            for table in ("cluster_jobs", "cluster_commands", "device_leases"):
                self.assertEqual(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 1)

    def test_job_key_conflicts_on_changed_input_and_is_account_scoped(self):
        payload = self._idempotent_job_payload()
        payload["worker_id"] = "worker-246"
        headers = {"Idempotency-Key": "shared-key", "X-Test-User": "alice"}
        first = self.client.post("/api/cluster/jobs", json=payload, headers=headers)
        self.assertEqual(first.status_code, 200, first.text)
        changed = self.client.post("/api/cluster/jobs", json={**payload, "device_count": 2}, headers=headers)
        self.assertEqual(changed.status_code, 409)
        self.assertEqual(changed.json()["code"], "STATE_CONFLICT")
        bob = self.client.post("/api/cluster/jobs", json=payload, headers={**headers, "X-Test-User": "bob"})
        self.assertEqual(bob.status_code, 409)
        self.assertNotIn(first.json()["job"]["id"], bob.text)

    def test_deleted_job_receipt_cannot_recreate_work(self):
        payload = self._idempotent_job_payload()
        payload["worker_id"] = "worker-246"
        headers = {"Idempotency-Key": "deleted-job"}
        first = self.client.post("/api/cluster/jobs", json=payload, headers=headers)
        self.assertEqual(first.status_code, 200, first.text)
        job_id = first.json()["job"]["id"]
        self.repo.transition_job(job_id, "failed", source="controller")
        self.repo.delete_job(job_id)
        replay = self.client.post("/api/cluster/jobs", json=payload, headers=headers)
        self.assertEqual(replay.status_code, 409)
        self.assertIn("deleted", replay.json()["error"])

    def test_concurrent_submission_has_one_job_and_one_dispatch(self):
        import threading
        from concurrent.futures import ThreadPoolExecutor

        payload = self._idempotent_job_payload()
        payload["worker_id"] = "worker-246"
        headers = {"Idempotency-Key": "concurrent-job"}
        entered, release = threading.Event(), threading.Event()
        original = self.repo.dispatch_job_start_command

        def delayed_dispatch(*args, **kwargs):
            entered.set()
            self.assertTrue(release.wait(5))
            return original(*args, **kwargs)

        with ThreadPoolExecutor(max_workers=1) as pool, patch.object(
            self.repo, "dispatch_job_start_command", side_effect=delayed_dispatch,
        ) as dispatch:
            first = pool.submit(self.client.post, "/api/cluster/jobs", json=payload, headers=headers)
            try:
                self.assertTrue(entered.wait(5))
                concurrent = self.client.post("/api/cluster/jobs", json=payload, headers=headers)
                self.assertEqual(concurrent.status_code, 409, concurrent.text)
                self.assertEqual(concurrent.json()["code"], "STATE_CONFLICT")
            finally:
                release.set()
            created = first.result(timeout=5)
            self.assertEqual(created.status_code, 200, created.text)
            replay = self.client.post("/api/cluster/jobs", json=payload, headers=headers)
            self.assertEqual(replay.json()["job"]["id"], created.json()["job"]["id"])
            self.assertEqual(dispatch.call_count, 1)

    def _commit_job_without_dispatch(self, *, devices=None):
        from types import SimpleNamespace

        from features.cluster.job_requests import request_fingerprint
        from features.cluster.jobs_api import _create_job
        from features.cluster.models import ClusterJobCreate

        payload = self._idempotent_job_payload()
        payload["worker_id"] = "worker-246"
        if devices is not None:
            payload["devices"] = devices
        body = ClusterJobCreate(**payload)
        request = SimpleNamespace(state=SimpleNamespace(current_user=CurrentUser(
            id="admin-id", username="admin", role="admin",
        )))
        with patch.object(self.repo, "dispatch_job_start_command", side_effect=KeyboardInterrupt), \
                self.assertRaises(KeyboardInterrupt):
            _create_job(body, request, {
                "_idempotency_key": "crash-gap",
                "_request_hash": request_fingerprint(body.model_dump(exclude={"owner_id"})),
            })
        return payload

    def test_committed_start_polled_during_response_failure_keeps_device_claims(self):
        from concurrent.futures import ThreadPoolExecutor

        payload = self._idempotent_job_payload()
        payload["worker_id"] = "worker-246"
        observer = ClusterRepository(self.repo.db_path)
        polled = []

        def lose_query_response(_command_id):
            # create_command has committed before its return-value query.
            # Poll from a separate repository/connection in that exact gap.
            with ThreadPoolExecutor(max_workers=1) as pool:
                polled.extend(pool.submit(observer.poll_commands, "worker-246").result(timeout=5))
            raise RuntimeError("command query response lost after commit")

        headers = {"Idempotency-Key": "polled-response-lost"}
        with patch.object(self.repo, "get_command", side_effect=lose_query_response):
            response = self.client.post("/api/cluster/jobs", json=payload, headers=headers)
        self.assertEqual(response.status_code, 503, response.text)
        self.assertTrue(response.json()["details"]["dispatch_uncertain"])
        self.assertEqual(len(polled), 1)
        job_id = response.json()["details"]["job_id"]
        job = self.repo.get_job(job_id)
        self.assertEqual(job["status"], "dispatching")
        self.assertEqual(job["leases"][0]["status"], "active")
        self.assertIsNotNone(self.repo.claims.active_claim("worker-246:ABC"))
        self.assertEqual(self.repo.get_command(polled[0]["id"])["status"], "delivered")
        replay = self.client.post("/api/cluster/jobs", json=payload, headers=headers)
        self.assertEqual(replay.status_code, 200, replay.text)
        self.assertEqual(replay.json()["command"]["id"], polled[0]["id"])
        with self.repo.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM cluster_commands").fetchone()[0], 1)
        running = observer.ack_command("worker-246", polled[0]["id"], {"status": "running"})
        observer.sync_job_from_command(running)
        self.assertEqual(self.repo.get_job(job_id)["status"], "running")
        self.assertIsNotNone(self.repo.claims.active_claim("worker-246:ABC"))
        completed = observer.ack_command("worker-246", polled[0]["id"], {"status": "completed"})
        observer.sync_job_from_command(completed)
        self.assertIsNone(self.repo.claims.active_claim("worker-246:ABC"))

    def test_requeued_previously_delivered_start_cannot_be_compensated(self):
        self._commit_job_without_dispatch()
        job = self.repo.list_jobs(1)[0]
        command = self.repo.replay_job_command(job)
        self.repo.poll_commands("worker-246")
        # Redelivery preserves delivered_at even while status is queued.
        with self.repo.connect() as conn:
            conn.execute("UPDATE cluster_commands SET status='queued' WHERE id=?", (command["id"],))
        self.assertFalse(self.repo.compensate_failed_dispatch(
            job["id"], RuntimeError("late response failure"), attempt_id=job["current_attempt_id"],
        ))
        self.assertEqual(self.repo.get_job(job["id"])["status"], "dispatching")
        self.assertEqual(self.repo.get_command(command["id"])["status"], "queued")
        self.assertIsNotNone(self.repo.claims.active_claim("worker-246:ABC"))

    def test_replay_resumes_dispatch_after_process_exit_at_commit_gap(self):
        payload = self._commit_job_without_dispatch()
        with self.repo.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM cluster_commands").fetchone()[0], 0)
        replay = self.client.post("/api/cluster/jobs", json=payload, headers={"Idempotency-Key": "crash-gap"})
        self.assertEqual(replay.status_code, 200, replay.text)
        self.assertEqual(replay.json()["command"]["command_type"], "start_test")
        with self.repo.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM cluster_jobs").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM cluster_commands").fetchone()[0], 1)

    def test_replay_repairs_a_legacy_command_without_rewinding_worker_state(self):
        payload = self._commit_job_without_dispatch()
        job = self.repo.list_jobs(1)[0]
        command = self.repo.create_command({
            "worker_id": job["assigned_worker_id"], "job_id": job["id"],
            "attempt_id": job["current_attempt_id"], "command_type": "start_test",
            "operation_id": f"{job['current_attempt_id']}:start_test",
            "payload": {"devices": job["request"]["devices"]},
        })
        self.assertEqual(self.repo.get_job(job["id"])["status"], "assigned")
        replay = self.client.post("/api/cluster/jobs", json=payload, headers={"Idempotency-Key": "crash-gap"})
        self.assertEqual(replay.status_code, 200, replay.text)
        self.assertEqual(replay.json()["command"]["id"], command["id"])
        self.assertEqual(replay.json()["job"]["status"], "dispatching")
        acknowledged = self.repo.ack_command(job["assigned_worker_id"], command["id"], {"status": "running"})
        self.repo.sync_job_from_command(acknowledged)
        self.repo.attach_command_to_job(job["id"], command)
        self.assertEqual(self.repo.get_job(job["id"])["status"], "running")
        with self.repo.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM cluster_commands").fetchone()[0], 1)

    def test_command_and_attachment_roll_back_together_on_process_exit(self):
        payload = self._commit_job_without_dispatch()
        job = self.repo.list_jobs(1)[0]
        original = self.repo._attach_command_to_job_conn

        def exit_after_transition(*args, **kwargs):
            original(*args, **kwargs)
            raise KeyboardInterrupt

        with patch.object(self.repo, "_attach_command_to_job_conn", side_effect=exit_after_transition), \
                self.assertRaises(KeyboardInterrupt):
            self.repo.replay_job_command(job)
        self.assertEqual(self.repo.get_job(job["id"])["status"], "assigned")
        with self.repo.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM cluster_commands").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM cluster_timeline_events WHERE event_type='command.queued'").fetchone()[0], 0)
        retry = self.client.post("/api/cluster/jobs", json=payload, headers={"Idempotency-Key": "crash-gap"})
        self.assertEqual(retry.status_code, 200, retry.text)
        self.assertEqual(retry.json()["job"]["status"], "dispatching")

    def test_command_is_not_visible_until_attachment_commits(self):
        self._commit_job_without_dispatch()
        job = self.repo.list_jobs(1)[0]
        observer = ClusterRepository(self.repo.db_path)
        original = self.repo._attach_command_to_job_conn

        def inspect_uncommitted_transition(*args, **kwargs):
            original(*args, **kwargs)
            with observer.connect() as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM cluster_commands").fetchone()[0], 0)
            self.assertEqual(observer.get_job(job["id"])["status"], "assigned")

        with patch.object(self.repo, "_attach_command_to_job_conn", side_effect=inspect_uncommitted_transition):
            command = self.repo.replay_job_command(job)
        self.assertEqual(observer.get_job(job["id"])["status"], "dispatching")
        self.assertEqual(observer.poll_commands("worker-246")[0]["id"], command["id"])

    def test_legacy_running_ack_is_applied_after_attachment_recovery(self):
        payload = self._commit_job_without_dispatch()
        job = self.repo.list_jobs(1)[0]
        command = self.repo.create_command({
            "worker_id": "worker-246", "job_id": job["id"], "command_type": "start_test",
            "attempt_id": job["current_attempt_id"], "payload": {},
        })
        running = self.repo.ack_command("worker-246", command["id"], {"status": "running"})
        self.repo.sync_job_from_command(running)
        self.assertEqual(self.repo.get_job(job["id"])["status"], "assigned")
        replay = self.client.post("/api/cluster/jobs", json=payload, headers={"Idempotency-Key": "crash-gap"})
        self.assertEqual(replay.status_code, 200, replay.text)
        self.assertEqual(replay.json()["job"]["status"], "running")
        self.assertFalse(self.repo.compensate_failed_dispatch(job["id"], ValueError("stale error")))
        self.assertIsNotNone(self.repo.claims.active_claim("worker-246:ABC"))

    def test_completed_job_retry_returns_original_after_device_release(self):
        payload = self._idempotent_job_payload()
        headers = {"Idempotency-Key": "completed-response-lost"}
        created = self.client.post("/api/cluster/jobs", json=payload, headers=headers)
        self.assertEqual(created.status_code, 200, created.text)
        command = created.json()["command"]
        completed = self.repo.ack_command("worker-246", command["id"], {"status": "completed"})
        self.repo.sync_job_from_command(completed)
        replay = self.client.post("/api/cluster/jobs", json=payload, headers=headers)
        self.assertEqual(replay.status_code, 200, replay.text)
        self.assertEqual(replay.json()["job"]["id"], created.json()["job"]["id"])
        self.assertEqual(replay.json()["job"]["status"], "completed")
        self.assertIsNone(self.repo.claims.active_claim("worker-246:ABC"))
        with self.repo.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM cluster_jobs").fetchone()[0], 1)

    def test_explicit_worker_empty_devices_selects_and_replays_concrete_devices(self):
        payload = self._idempotent_job_payload()
        payload.update(worker_id="worker-246", devices=[])
        headers = {"Idempotency-Key": "explicit-auto-device"}
        first = self.client.post("/api/cluster/jobs", json=payload, headers=headers)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.json()["job"]["request"]["devices"], ["worker-246:ABC"])
        self.assertEqual(first.json()["job"]["leases"][0]["device_id"], "worker-246:ABC")
        replay = self.client.post("/api/cluster/jobs", json=payload, headers=headers)
        self.assertEqual(replay.status_code, 200, replay.text)
        self.assertEqual(first.json()["job"]["id"], replay.json()["job"]["id"])

    def test_empty_devices_dispatch_gap_recovers_with_selected_device_fencing(self):
        payload = self._commit_job_without_dispatch(devices=[])
        replay = self.client.post("/api/cluster/jobs", json=payload, headers={"Idempotency-Key": "crash-gap"})
        self.assertEqual(replay.status_code, 200, replay.text)
        self.assertEqual(replay.json()["job"]["request"]["devices"], ["worker-246:ABC"])
        self.assertEqual(replay.json()["command"]["payload"]["lease_tokens"][0]["device_id"], "worker-246:ABC")

    def test_replay_rejects_a_new_claim_generation_and_cancels_legacy_start(self):
        payload = self._commit_job_without_dispatch()
        job = self.repo.list_jobs(1)[0]
        command = self.repo.create_command({
            "worker_id": "worker-246", "job_id": job["id"], "command_type": "start_test",
            "attempt_id": job["current_attempt_id"], "payload": {},
        })
        self.repo.claims.release(f"job:{job['id']}", status="expired")
        acquired, _ = self.repo.claims.acquire(
            [{"device_key": "worker-246:ABC", "worker_id": "worker-246", "serial": "ABC"}],
            owner_id=job["owner_id"], username="admin", source_type="cluster_job",
            source_id=f"job:{job['id']}", ttl_seconds=90,
        )
        self.assertTrue(acquired)
        self.assertNotEqual(self.repo.claims.active_claim("worker-246:ABC")["generation"], job["leases"][0]["generation"])
        replay = self.client.post("/api/cluster/jobs", json=payload, headers={"Idempotency-Key": "crash-gap"})
        self.assertEqual(replay.status_code, 409, replay.text)
        self.assertEqual(replay.json()["code"], "STATE_CONFLICT")
        self.assertEqual(self.repo.get_command(command["id"])["status"], "cancelled")
        self.assertEqual(self.repo.poll_commands("worker-246"), [])
        self.assertEqual(self.repo.get_job(job["id"])["leases"][0]["status"], "released")

    def _add_worker_inventory(self, worker_id, devices, tools_path="/srv/GMS-Suite/android-cts/tools"):
        self.repo.register_worker({
            "worker_id": worker_id, "name": worker_id, "hostname": worker_id,
            "address": "192.0.2.1", "agent_version": "1", "max_jobs": 1,
        })
        self.repo.heartbeat(worker_id, {
            "agent_version": "1", "running_jobs": [], "devices": devices,
            "suites": [{"suite_type": "CTS", "suite_version": "17_r1",
                        "suite_key": "CTS:17_r1", "available": True, "tools_path": tools_path}],
        })

    def test_auto_scheduler_applies_usb_requirement_before_worker_scoring(self):
        payload = self._idempotent_job_payload()
        self._add_worker_inventory("zz-proxy", [
            {"serial": f"PROXY-{index}", "state": "available", "transport": "adb_proxy"}
            for index in range(4)
        ])
        payload.update(devices=[], execution_spec={
            "test_type": "cts", "suite_path": "/srv/GMS-Suite/android-cts/tools",
            "module": "CtsUsbTestCases",
        })
        response = self.client.post("/api/cluster/jobs", json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["job"]["assigned_worker_id"], "worker-246")
        self.assertEqual(response.json()["command"]["payload"]["execution_spec"]["devices"], ["ABC"])

    def test_auto_scheduler_matches_explicit_devices_and_suite_path(self):
        self._idempotent_job_payload()
        self._add_worker_inventory("zz-other", [
            {"serial": f"OTHER-{index}", "state": "available"} for index in range(4)
        ], tools_path="/different/cts/tools")
        for identity in ("ABC", "worker-246:ABC"):
            with self.subTest(identity=identity):
                worker_id, devices = cluster_api.cluster_service.select_worker(
                    "CTS:17_r1", requested_devices=[identity], require_agent=True,
                )
                self.assertEqual((worker_id, devices), ("worker-246", ["worker-246:ABC"]))
        response = self.client.post("/api/cluster/jobs", json={
            "worker_id": "auto", "suite_key": "CTS:17_r1", "devices": [],
            "execution_spec": {"test_type": "cts", "suite_path": "/srv/GMS-Suite/android-cts/tools"},
        })
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["job"]["assigned_worker_id"], "worker-246")

    def test_unavailable_or_duplicate_devices_do_not_create_a_job(self):
        payload = self._idempotent_job_payload()
        for devices in (["MISSING"], ["ABC", "worker-246:ABC"]):
            with self.subTest(devices=devices):
                response = self.client.post("/api/cluster/jobs", json={**payload, "devices": devices})
                self.assertEqual(response.status_code, 409, response.text)
                self.assertEqual(response.json()["code"], "STATE_CONFLICT")
        self.assertEqual(self.repo.list_jobs(10), [])

    def test_qualified_device_identity_takes_precedence_over_a_colon_serial(self):
        self._idempotent_job_payload()
        self._add_worker_inventory("zz-other", [
            {"serial": serial, "state": "available"}
            for serial in ("worker-246:ABC", "OTHER-1", "OTHER-2", "OTHER-3")
        ])
        response = self.client.post("/api/cluster/jobs", json={
            "worker_id": "auto", "suite_key": "CTS:17_r1", "devices": ["worker-246:ABC"],
        })
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["job"]["assigned_worker_id"], "worker-246")

    def test_spec_devices_are_authoritative_when_job_devices_are_empty(self):
        self._idempotent_job_payload()
        self._add_worker_inventory("zz-other", [
            {"serial": f"OTHER-{index}", "state": "available"} for index in range(4)
        ])
        response = self.client.post("/api/cluster/jobs", json={
            "worker_id": "auto", "suite_key": "CTS:17_r1", "devices": [],
            "execution_spec": {
                "test_type": "cts", "suite_path": "/srv/GMS-Suite/android-cts/tools", "devices": ["ABC"],
            },
        })
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["job"]["assigned_worker_id"], "worker-246")

    def test_empty_selection_requires_the_requested_device_count(self):
        payload = self._idempotent_job_payload()
        response = self.client.post("/api/cluster/jobs", json={
            **payload, "worker_id": "worker-246", "devices": [], "device_count": 2,
        })
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.repo.list_jobs(10), [])

    def test_replay_cannot_dispatch_after_device_claim_changes_owner(self):
        payload = self._commit_job_without_dispatch()
        with self.repo.connect() as conn:
            job_id = conn.execute("SELECT id FROM cluster_jobs").fetchone()[0]
        self.repo.claims.release(f"job:{job_id}", status="expired")
        acquired, _ = self.repo.claims.acquire(
            [{"device_key": "worker-246:ABC", "worker_id": "worker-246", "serial": "ABC"}],
            owner_id="bob-id", username="bob", source_type="firmware",
            source_id="firmware:bob", ttl_seconds=90,
        )
        self.assertTrue(acquired)
        replay = self.client.post("/api/cluster/jobs", json=payload, headers={"Idempotency-Key": "crash-gap"})
        self.assertEqual(replay.status_code, 409, replay.text)
        self.assertEqual(replay.json()["code"], "STATE_CONFLICT")
        self.assertEqual(self.repo.get_job(job_id)["status"], "failed")
        self.assertEqual(self.repo.claims.active_claim("worker-246:ABC")["source_id"], "firmware:bob")
        with self.repo.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM cluster_commands").fetchone()[0], 0)

    def test_agent_replay_rechecks_current_worker_and_device_acl(self):
        payload = self._idempotent_job_payload()
        payload["worker_id"] = "worker-246"
        headers = {"Idempotency-Key": "acl-replay", "X-Test-User": "alice"}
        created = self.client.post("/api/cluster/jobs", json=payload, headers=headers)
        self.assertEqual(created.status_code, 200, created.text)
        for workers, devices in [("another-worker", "*"), ("worker-246", "another-device")]:
            with self.subTest(workers=workers, devices=devices):
                replay = self.client.post("/api/cluster/jobs", json=payload, headers={
                    **headers, "X-Test-Agent-Workers": workers, "X-Test-Agent-Devices": devices,
                })
                self.assertEqual(replay.status_code, 403, replay.text)
                self.assertNotIn(created.json()["job"]["id"], replay.text)
        permitted = self.client.post("/api/cluster/jobs", json=payload, headers={
            **headers, "X-Test-Agent-Workers": "worker-246", "X-Test-Agent-Devices": "ABC",
        })
        self.assertEqual(permitted.status_code, 200, permitted.text)
        self.assertEqual(permitted.json()["job"]["id"], created.json()["job"]["id"])

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
            if "X-Test-Agent-Workers" in request.headers:
                request.state.current_user = CurrentUser(
                    id="agent:test", username=username, role="agent_service",
                    resource_owner_id=f"{username}-id",
                    extra_permissions=frozenset({"tests.execute"}),
                )
                request.state.auth_method = "agent_token"
                request.state.agent_token_record = {
                    "allowed_workers": request.headers["X-Test-Agent-Workers"],
                    "allowed_devices": request.headers.get("X-Test-Agent-Devices", "*"),
                }
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

    def test_job_owner_is_server_principal_and_cross_user_id_is_hidden(self):
        tools_path = "/srv/GMS-Suite/android-cts/tools"
        self.repo.heartbeat("worker-246", {
            "agent_version": "1",
            "running_jobs": [],
            "devices": [{"serial": "ABC", "state": "available"}],
            "suites": [{
                "suite_type": "CTS",
                "suite_version": "17_r1",
                "suite_key": "CTS:17_r1",
                "tools_path": tools_path,
                "available": True,
            }],
        })
        created = self.client.post(
            "/api/cluster/jobs",
            headers={"X-Test-User": "alice", "X-Test-Role": "user"},
            json={
                "worker_id": "worker-246",
                "suite_key": "CTS:17_r1",
                "devices": ["ABC"],
                "execution_spec": {
                    "test_type": "cts",
                    "suite_path": tools_path,
                    "module": "CtsSecurityTestCases",
                    "devices": ["ABC"],
                },
                "owner_id": "bob-id",
            },
        )
        self.assertEqual(created.status_code, 200, created.text)
        job = created.json()["job"]
        self.assertEqual(job["owner_id"], "alice-id")

        hidden = self.client.get(
            f"/api/cluster/jobs/{job['id']}",
            headers={"X-Test-User": "bob", "X-Test-Role": "user"},
        )
        self.assertEqual(hidden.status_code, 404)

        hidden_list = self.client.get(
            "/api/cluster/jobs",
            headers={"X-Test-User": "bob", "X-Test-Role": "user"},
        )
        self.assertEqual(hidden_list.status_code, 200)
        self.assertEqual(hidden_list.json()["jobs"], [])

        monitored = self.client.get(
            "/api/cluster/jobs?include_active=true",
            headers={"X-Test-User": "bob", "X-Test-Role": "user"},
        )
        self.assertEqual(monitored.status_code, 200)
        self.assertEqual(monitored.json()["jobs"], [])

        operator_view = self.client.get(
            "/api/cluster/jobs?include_active=true",
            headers={
                "X-Test-User": "operator",
                "X-Test-Role": "device_operator",
            },
        )
        self.assertEqual(operator_view.status_code, 200)
        monitor_job = operator_view.json()["jobs"][0]
        self.assertEqual(monitor_job["id"], job["id"])
        self.assertTrue(monitor_job["monitor_only"])
        self.assertNotIn("owner_id", monitor_job)
        self.assertNotIn("request", monitor_job)
        self.assertNotIn("current_attempt_id", monitor_job)
        # 跨用户视图只保留脱敏 serial（原值 "ABC" → "AB****"）
        for lease in monitor_job["leases"]:
            self.assertNotEqual(lease["serial"], "ABC")
            self.assertIn("****", lease["serial"])

        admin_view = self.client.get("/api/cluster/jobs?include_active=true")
        self.assertEqual(admin_view.status_code, 200)
        admin_job = admin_view.json()["jobs"][0]
        self.assertFalse(admin_job.get("monitor_only", False))
        self.assertEqual(admin_job["owner_id"], "alice-id")
        self.assertEqual(admin_job["leases"][0]["serial"], "ABC")

        own = self.client.get(
            f"/api/cluster/jobs/{job['id']}",
            headers={"X-Test-User": "alice", "X-Test-Role": "user"},
        )
        self.assertEqual(own.status_code, 200)

    def test_browser_supplied_raw_argv_is_rejected(self):
        """浏览器提交的 raw argv 必须被拒绝，防止绕过 ExecutionSpec 校验。"""
        response = self.client.post(
            "/api/cluster/jobs",
            json={
                "worker_id": "worker-246",
                "suite_key": "CTS:17_r1",
                "devices": ["ABC"],
                "argv": ["/bin/true"],
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("raw argv is not accepted", response.json()["detail"])
        self.assertIsNone(self.repo.list_jobs(1)[0]["id"] if self.repo.list_jobs(1) else None)

    def test_structured_job_uses_inventory_suite_and_leased_devices(self):
        tools_path = "/srv/GMS-Suite/android-cts/tools"
        self.repo.heartbeat("worker-246", {
            "agent_version": "1",
            "running_jobs": [],
            "devices": [{"serial": "ABC", "state": "available"}],
            "suites": [{
                "suite_type": "CTS",
                "suite_version": "17_r1",
                "suite_key": "CTS:17_r1",
                "tools_path": tools_path,
                "available": True,
            }],
        })

        response = self.client.post(
            "/api/cluster/jobs",
            json={
                "worker_id": "worker-246",
                "suite_key": "CTS:17_r1",
                "devices": ["worker-246:ABC"],
                "execution_spec": {
                    "test_type": "cts",
                    "suite_path": tools_path,
                    "module": "CtsSecurityTestCases",
                    "devices": ["ABC"],
                },
            },
        )

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()["command"]["payload"]
        self.assertEqual(payload["execution_spec"]["devices"], ["ABC"])
        self.assertEqual(payload["execution_spec"]["suite_path"], tools_path)
        self.assertIn("CtsSecurityTestCases", payload["argv"])
        self.assertIn("-s ABC", payload["argv"])

    def test_structured_job_rejects_devices_outside_lease_request(self):
        tools_path = "/srv/GMS-Suite/android-cts/tools"
        self.repo.heartbeat("worker-246", {
            "agent_version": "1",
            "running_jobs": [],
            "devices": [{"serial": "ABC", "state": "available"}],
            "suites": [{
                "suite_type": "CTS",
                "suite_version": "17_r1",
                "suite_key": "CTS:17_r1",
                "tools_path": tools_path,
                "available": True,
            }],
        })

        response = self.client.post(
            "/api/cluster/jobs",
            json={
                "worker_id": "worker-246",
                "suite_key": "CTS:17_r1",
                "devices": ["ABC"],
                "execution_spec": {
                    "test_type": "cts",
                    "suite_path": tools_path,
                    "devices": ["OTHER"],
                },
            },
        )

        self.assertEqual(response.status_code, 409, response.text)

    def test_job_response_exposes_resolved_client_display_id(self):
        job = self.repo.create_job_with_leases({
            "worker_id": "worker-246",
            "owner_id": "N387pLbIBhpMw5JsWUL9hg",
            "devices": ["worker-246:ABC"],
            "suite_key": "CTS:17_r1",
        })

        with patch(
            "features.users.resolve_client_display_id",
            return_value="hcq@172.16.14.66",
        ):
            response = self.client.get(f"/api/cluster/jobs/{job['id']}")

        self.assertEqual(response.status_code, 200)
        payload = response.json()["job"]
        self.assertEqual(payload["owner_id"], "N387pLbIBhpMw5JsWUL9hg")
        self.assertEqual(payload["client_display_id"], "hcq@172.16.14.66")


if __name__ == "__main__":
    unittest.main()
