from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from features.auth import CurrentUser
from features.build import api as build_api
from features.build.service import BuildNotFoundError


class FakeBuildService:
    def __init__(self):
        self.jobs = {
            "alice-job": {"id": "alice-job", "owner": "id-alice", "status": "completed"},
            "bob-job": {"id": "bob-job", "owner": "id-bob", "status": "completed"},
        }
        self.created_request = None

    def list_jobs(self, **_kwargs):
        return list(self.jobs.values())

    def get_job(self, job_id):
        if job_id not in self.jobs:
            raise BuildNotFoundError("Build job not found")
        return self.jobs[job_id]

    def create_job(self, request, *, start=True):
        self.created_request = request
        return {"id": "new-job", "owner": request.get("owner"), "status": "running"}

    def list_servers(self):
        return [{"id": "server"}]

    def list_templates(self, *, enabled_only=False):
        return [{"id": "template"}]


class BuildAccessTests(unittest.TestCase):
    def setUp(self):
        self.service = FakeBuildService()
        self.service_patch = patch.object(build_api, "build_service", self.service)
        self.service_patch.start()
        app = FastAPI()

        @app.middleware("http")
        async def test_identity(request: Request, call_next):
            username = request.headers.get("X-Test-User", "alice")
            role = request.headers.get("X-Test-Role", "user")
            scopes = [
                item
                for item in request.headers.get("X-Test-Scopes", "").split(",")
                if item.strip()
            ]
            request.state.current_user = CurrentUser(
                id=f"id-{username}",
                username=username,
                role=role,
                extra_permissions=frozenset(scopes),
            )
            return await call_next(request)

        app.include_router(build_api.router)
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        self.service_patch.stop()

    def test_user_lists_only_owned_jobs_and_cannot_read_another_job(self):
        listed = self.client.get("/api/build/jobs")
        hidden = self.client.get("/api/build/jobs/bob-job")

        self.assertEqual(
            [job["id"] for job in listed.json()["data"]["items"]],
            ["alice-job"],
        )
        self.assertEqual(hidden.status_code, 404)

    def test_admin_can_read_all_jobs(self):
        response = self.client.get(
            "/api/build/jobs",
            headers={"X-Test-User": "admin", "X-Test-Role": "admin"},
        )

        self.assertEqual(
            {job["id"] for job in response.json()["data"]["items"]},
            {"alice-job", "bob-job"},
        )

    def test_create_job_ignores_spoofed_owner(self):
        response = self.client.post(
            "/api/build/jobs",
            json={"server_id": "server", "template_id": "template", "owner": "bob"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["owner"], "id-alice")
        self.assertEqual(self.service.created_request["owner"], "id-alice")

    def test_router_rejects_anonymous_access_without_global_middleware(self):
        app = FastAPI()
        app.include_router(build_api.router)
        with patch.dict(os.environ, {"GMS_ENV": "production", "GMS_AUTH_REQUIRED": "true"}), TestClient(app) as anonymous:
            listed = anonymous.get("/api/build/jobs")
            fetched = anonymous.get("/api/build/jobs/alice-job")

        self.assertEqual(listed.status_code, 401)
        self.assertEqual(fetched.status_code, 401)

    def test_zero_scope_agent_token_cannot_create_build_job(self):
        """零 scope Agent token:身份合法但没有 build.execute,必须 403。"""

        response = self.client.post(
            "/api/build/jobs",
            headers={
                "X-Test-User": "kkagent",
                "X-Test-Role": "agent_service",
            },
            json={"server_id": "server", "template_id": "template"},
        )

        self.assertEqual(response.status_code, 403)
        self.assertIsNone(self.service.created_request)

    def test_agent_token_with_build_execute_scope_creates_build_job(self):
        response = self.client.post(
            "/api/build/jobs",
            headers={
                "X-Test-User": "kkagent",
                "X-Test-Role": "agent_service",
                "X-Test-Scopes": "build.execute",
            },
            json={"server_id": "server", "template_id": "template"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.service.created_request["owner"], "id-kkagent")

    def test_zero_scope_agent_token_cannot_discover_or_cancel(self):
        # cancel 类路由挂无条件 require_permission:任何模式下零 scope 都被拒。
        cancel = self.client.post(
            "/api/build/jobs/alice-job/cancel",
            headers={"X-Test-User": "kkagent", "X-Test-Role": "agent_service"},
        )
        self.assertEqual(cancel.status_code, 403)

        # discover 类路由用 require_permission_when_auth_required:
        # 仅在强制认证部署(生产)下校验权限,dev 模式保持开放语义。
        app = FastAPI()
        app.include_router(build_api.router)

        @app.middleware("http")
        async def test_identity(request: Request, call_next):
            username = request.headers.get("X-Test-User", "kkagent")
            role = request.headers.get("X-Test-Role", "agent_service")
            request.state.current_user = CurrentUser(
                id=f"id-{username}", username=username, role=role
            )
            return await call_next(request)

        env = {"GMS_ENV": "production", "GMS_AUTH_REQUIRED": "true"}
        with patch.dict(os.environ, env), TestClient(app) as client:
            discover = client.post(
                "/api/build/discover/workspaces",
                headers={"X-Test-User": "kkagent", "X-Test-Role": "agent_service"},
                json={"server_id": "server"},
            )
        self.assertEqual(discover.status_code, 403)

    def test_zero_scope_agent_token_cannot_enumerate_servers_or_templates(self):
        """零 scope Agent token 不得枚举构建服务器/模板元数据（ADR 0006
        零隐式权限）；带 build.read 的 token 与人类用户保持可用。"""
        app = FastAPI()
        app.include_router(build_api.router)

        @app.middleware("http")
        async def test_identity(request: Request, call_next):
            username = request.headers.get("X-Test-User", "kkagent")
            role = request.headers.get("X-Test-Role", "agent_service")
            scopes = frozenset(
                item
                for item in request.headers.get("X-Test-Scopes", "").split(",")
                if item
            )
            request.state.current_user = CurrentUser(
                id=f"id-{username}", username=username, role=role,
                extra_permissions=scopes,
            )
            return await call_next(request)

        env = {"GMS_ENV": "production", "GMS_AUTH_REQUIRED": "true"}
        with patch.dict(os.environ, env), TestClient(app) as client:
            zero_servers = client.get(
                "/api/build/servers",
                headers={"X-Test-User": "kkagent", "X-Test-Role": "agent_service"},
            )
            zero_templates = client.get(
                "/api/build/templates",
                headers={"X-Test-User": "kkagent", "X-Test-Role": "agent_service"},
            )
            scoped_servers = client.get(
                "/api/build/servers",
                headers={
                    "X-Test-User": "kkagent",
                    "X-Test-Role": "agent_service",
                    "X-Test-Scopes": "build.read",
                },
            )
            human_servers = client.get(
                "/api/build/servers",
                headers={"X-Test-User": "alice", "X-Test-Role": "user"},
            )
        self.assertEqual(zero_servers.status_code, 403)
        self.assertEqual(zero_templates.status_code, 403)
        self.assertEqual(scoped_servers.status_code, 200)
        self.assertEqual(human_servers.status_code, 200)

    def test_human_roles_keep_build_access(self):
        response = self.client.post(
            "/api/build/jobs",
            headers={"X-Test-User": "alice", "X-Test-Role": "user"},
            json={"server_id": "server", "template_id": "template"},
        )

        self.assertEqual(response.status_code, 200)


if __name__ == "__main__":
    unittest.main()
