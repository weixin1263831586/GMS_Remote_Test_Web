"""Daily Brief 端点的 scope / human-only 边界测试。

从 test_daily_brief_api.py 拆出（文件尺寸预算内聚块）：
agent token 能力边界 + human session 触发路径。
"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

import features.redmine.daily_brief_api as daily_brief_api
import features.redmine.daily_brief_repository as brief_repo
from features.auth import CurrentUser
from features.redmine.kkagent_analyzer import KkAgentAnalysisResult
from features.redmine.tests.test_daily_brief_api import TRIAGE, VALID


AGENT_HEADERS = {
    "x-test-role": "agent_service",
    "x-test-auth-method": "agent_token",
}


class DailyBriefApiAuthzTests(unittest.TestCase):
    """Daily Brief 端点的 scope / human-only 边界。"""

    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        repo_patch = patch.object(
            brief_repo, "owner_redmine_root",
            lambda owner: Path(self.directory.name) / "owners" / str(owner),
        )
        repo_patch.start()
        self.addCleanup(repo_patch.stop)
        brief_repo._REPO_CACHE.clear()
        self.addCleanup(brief_repo._REPO_CACHE.clear)

        triage_patch = patch.object(
            daily_brief_api.DailyBriefService, "build_triage",
            AsyncMock(return_value=dict(TRIAGE)),
        )
        triage_patch.start()
        self.addCleanup(triage_patch.stop)

        fake_manager = SimpleNamespace(
            get_runtime_config=lambda: {},
            save_runtime=lambda data: True,
        )
        cfg_patch = patch.object(
            daily_brief_api, "get_redmine_config_for_request",
            lambda request: fake_manager,
        )
        cfg_patch.start()
        self.addCleanup(cfg_patch.stop)
        creds_patch = patch.object(
            daily_brief_api, "_has_redmine_credentials", lambda request: True,
        )
        creds_patch.start()
        self.addCleanup(creds_patch.stop)

        async def fake_analyze(entry):
            return KkAgentAnalysisResult(ok=True, result=dict(VALID))

        analyzer_patch = patch.object(
            daily_brief_api.DailyBriefService, "_build_analyzer",
            lambda self, config=None: SimpleNamespace(analyze=fake_analyze),
        )
        analyzer_patch.start()
        self.addCleanup(analyzer_patch.stop)

        app = FastAPI()

        @app.middleware("http")
        async def authenticate(request, call_next):
            owner = request.headers.get("x-test-owner", "owner-a")
            role = request.headers.get("x-test-role", "user")
            scopes = frozenset(
                part for part in request.headers.get("x-test-scopes", "").split(",")
                if part
            )
            request.state.current_user = CurrentUser(
                id=owner, username=owner, role=role, extra_permissions=scopes,
            )
            request.state.auth_method = request.headers.get(
                "x-test-auth-method", "session"
            )
            return await call_next(request)

        app.include_router(daily_brief_api.router)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def test_agent_token_without_redmine_scope_cannot_read(self):
        headers = {**AGENT_HEADERS, "x-test-scopes": "devices.read"}
        resp = self.client.get(
            "/api/redmine-agent/daily-brief/latest", headers=headers
        )
        self.assertEqual(resp.status_code, 403)
        resp = self.client.get(
            "/api/redmine-agent/daily-brief/triage", headers=headers
        )
        self.assertEqual(resp.status_code, 403)

    def test_agent_token_with_redmine_scope_can_read(self):
        headers = {**AGENT_HEADERS, "x-test-scopes": "redmine.read"}
        resp = self.client.get(
            "/api/redmine-agent/daily-brief/latest", headers=headers
        )
        self.assertEqual(resp.status_code, 200)

    def test_agent_token_cannot_trigger_run_even_with_read_scope(self):
        headers = {**AGENT_HEADERS, "x-test-scopes": "redmine.read"}
        resp = self.client.post("/api/redmine-agent/daily-brief/run", headers=headers)
        self.assertEqual(resp.status_code, 403)
        resp = self.client.post('/api/redmine-agent/daily-brief/analyze-issue', headers=headers, json={'issue_id': 647338})
        self.assertEqual(resp.status_code, 403)
        resp = self.client.put(
            "/api/redmine-agent/daily-brief/config",
            headers=headers, json={"enabled": False},
        )
        self.assertEqual(resp.status_code, 403)

    def test_human_session_can_trigger_run(self):
        resp = self.client.post("/api/redmine-agent/daily-brief/run")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("run_id", resp.json()["data"])


if __name__ == "__main__":
    unittest.main()
