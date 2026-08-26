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
