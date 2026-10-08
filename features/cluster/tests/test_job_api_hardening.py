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

    def _commit_job_without_dispatch(self):
        from types import SimpleNamespace

        from features.cluster.job_requests import request_fingerprint
        from features.cluster.jobs_api import _create_job
        from features.cluster.models import ClusterJobCreate

        payload = self._idempotent_job_payload()
        payload["worker_id"] = "worker-246"
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
