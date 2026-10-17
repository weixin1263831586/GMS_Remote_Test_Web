"""Agent Service Token scope gates on cluster read endpoints (ADR 0006).

GET /api/cluster/workers|hosts 与 GET /api/cluster/suites/files|search|download
对 Agent 主体要求显式 scope（devices.read / suites.read）；人类会话与
dev 匿名调用保持原有行为。门以 auth_method == "agent_token" 判定，与
/api/cluster/devices 相同（人类 "user" 角色不携带 devices.read/suites.read，
不能因收紧 Agent 而被拒）。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from features.auth import CurrentUser
from features.cluster import api as cluster_api
from features.cluster.repository import ClusterRepository
from features.cluster.service import ClusterService


class ClusterAgentScopeGateTests(unittest.TestCase):
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
        async def identify_principal(request: Request, call_next):
            scopes = request.headers.get("X-Test-Agent-Scopes")
            if scopes is not None:
                request.state.current_user = CurrentUser(
                    id="agent:token-1",
                    username="agent:token-1",
                    role="agent_service",
                    extra_permissions=frozenset(
                        item for item in scopes.split(",") if item
                    ),
                    resource_owner_id="alice-id",
                )
                request.state.auth_method = "agent_token"
                request.state.agent_token_record = {
                    "allowed_workers": request.headers.get(
                        "X-Test-Agent-Workers", "worker-246"
                    ),
                    "allowed_devices": request.headers.get(
                        "X-Test-Agent-Devices", "*"
                    ),
                }
            elif request.headers.get("X-Test-Anonymous"):
                pass  # dev 匿名：不设置 current_user
            else:
                username = request.headers.get("X-Test-User", "alice")
                request.state.current_user = CurrentUser(
                    id=f"{username}-id",
                    username=username,
                    role=request.headers.get("X-Test-Role", "user"),
                )
            return await call_next(request)

        app.include_router(cluster_api.router)
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        cluster_api.cluster_service = self.previous_service
        self.temp.cleanup()

    def test_agent_without_devices_read_cannot_list_workers_or_hosts(self):
        for path in ("/api/cluster/workers", "/api/cluster/hosts"):
            with self.subTest(path=path):
                response = self.client.get(
                    path, headers={"X-Test-Agent-Scopes": "tests.execute"}
                )
                self.assertEqual(response.status_code, 403, response.text)
                self.assertEqual(
                    response.json()["detail"]["scope_required"], "devices.read"
                )

    def test_agent_with_devices_read_lists_workers_and_hosts(self):
        headers = {"X-Test-Agent-Scopes": "devices.read"}
        workers = self.client.get("/api/cluster/workers", headers=headers)
        hosts = self.client.get("/api/cluster/hosts", headers=headers)
        self.assertEqual(workers.status_code, 200, workers.text)
        self.assertEqual(hosts.status_code, 200, hosts.text)
        self.assertEqual(
            [item["id"] for item in workers.json()["workers"]], ["worker-246"]
        )

    def test_agent_without_suites_read_cannot_read_suite_files(self):
        endpoints = (
            "/api/cluster/suites/files?worker_id=worker-246&suite_path=/s",
            "/api/cluster/suites/search?worker_id=worker-246&suite_path=/s&query=x",
            "/api/cluster/suites/download?worker_id=worker-246&suite_path=/s&path=a",
        )
        for path in endpoints:
            with self.subTest(path=path):
                response = self.client.get(
                    path, headers={"X-Test-Agent-Scopes": "devices.read"}
                )
                self.assertEqual(response.status_code, 403, response.text)
                self.assertEqual(
                    response.json()["detail"]["scope_required"], "suites.read"
                )

    def test_human_sessions_and_anonymous_dev_calls_stay_usable(self):
        # 人类 "user" 会话（无 devices.read/suites.read 权限）保持可用。
        self.assertEqual(
            self.client.get(
                "/api/cluster/workers",
                headers={"X-Test-User": "alice", "X-Test-Role": "user"},
            ).status_code,
            200,
        )
        self.assertEqual(
            self.client.get(
                "/api/cluster/hosts",
                headers={"X-Test-User": "alice", "X-Test-Role": "user"},
            ).status_code,
            200,
        )
        # dev 匿名调用（无凭据）保持可用。
        self.assertEqual(
            self.client.get(
                "/api/cluster/workers", headers={"X-Test-Anonymous": "1"}
            ).status_code,
            200,
        )


if __name__ == "__main__":
    unittest.main()
