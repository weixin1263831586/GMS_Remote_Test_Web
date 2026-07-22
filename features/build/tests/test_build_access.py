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
            request.state.current_user = CurrentUser(
                id=f"id-{username}",
                username=username,
                role=role,
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


if __name__ == "__main__":
    unittest.main()
