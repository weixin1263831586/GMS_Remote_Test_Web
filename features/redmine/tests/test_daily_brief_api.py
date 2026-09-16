"""Daily Brief API 测试：triage/run/config/latest 端点（不依赖真实 kkagent）。"""

from __future__ import annotations

import asyncio
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


VALID = {
    "problem_summary": "summary",
    "customer_request": "request",
    "recommended_actions": [],
    "suggested_solution": "solution",
    "detailed_report": "## 一、问题概况\n\n| 项目 | 内容 |\n|---|---|\n| Issue | #101 |",
    "evidence": [],
    "confidence": 0.9,
    "root_cause_type": "likely",
}

TRIAGE = {
    "brief_date": "2026-09-13",
    "generated_at": "2026-09-13T00:00:03",
    "snapshot_hash": "h1",
    "counts": {"waiting_my_reply": 1, "no_reply_3_days": 0, "total": 1},
    "issues": [{"issue_id": 1, "subject": "S", "buckets": ["waiting_my_reply"],
                 "priority": "P1", "priority_score": 90, "fingerprint": "f1"}],
}


class DailyBriefApiTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

        # owner 隔离：重定向到临时目录
        repo_patch = patch.object(
            brief_repo, "owner_redmine_root",
            lambda owner: self.root / "owners" / str(owner),
        )
        repo_patch.start()
        self.addCleanup(repo_patch.stop)
        brief_repo._REPO_CACHE.clear()
        self.addCleanup(brief_repo._REPO_CACHE.clear)

        runtime: dict = {}
        fake_manager = SimpleNamespace(
            get_runtime_config=lambda: runtime,
            save_runtime=lambda data: runtime.update(data) or True,
        )
        config_patch = patch.object(
            daily_brief_api, "get_redmine_config_for_request",
            lambda request: fake_manager,
        )
        config_patch.start()
        self.addCleanup(config_patch.stop)
        self.runtime = runtime

        self.has_creds = True
        creds_patch = patch.object(
            daily_brief_api, "_has_redmine_credentials",
            lambda request: self.has_creds,
        )
        creds_patch.start()
        self.addCleanup(creds_patch.stop)

        triage_mock = AsyncMock(return_value=dict(TRIAGE))
        self.triage_mock = triage_mock
        triage_patch = patch.object(
            daily_brief_api.DailyBriefService, "build_triage", triage_mock
        )
        triage_patch.start()
        self.addCleanup(triage_patch.stop)

        async def fake_analyze(entry):
            return KkAgentAnalysisResult(ok=True, result=dict(VALID))

        fake_analyzer = SimpleNamespace(analyze=fake_analyze)
        analyzer_patch = patch.object(
            daily_brief_api.DailyBriefService, "_build_analyzer",
            lambda self, config=None: fake_analyzer,
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
                id=owner,
                username=owner,
                role=role,
                extra_permissions=scopes,
            )
            request.state.auth_method = request.headers.get(
                "x-test-auth-method", "session"
            )
            return await call_next(request)

        app.include_router(daily_brief_api.router)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    # ------------------------------------------------------------------ triage

    def test_single_issue_queues_only_requested_id_without_workload_scan(self):
        response = self.client.post('/api/redmine-agent/daily-brief/analyze-issue', json={'issue_id': 647338})
        self.assertEqual(response.status_code, 200)
        queued = response.json()['data']
        repo = brief_repo.owner_daily_brief_repository('owner-a')
        self.assertEqual([row.issue_id for row in repo.list_issues(queued['run_id'])], [647338])
        job = repo.get_job(queued['job_id'])
        self.assertEqual(job['kind'], 'issue')
        self.assertEqual(job['issue_id'], 647338)
        self.triage_mock.assert_not_awaited()
        self.assertIsNone(repo.latest_run('owner-a'))
        again = self.client.post('/api/redmine-agent/daily-brief/analyze-issue', json={'issue_id': 647338}).json()['data']
        self.assertEqual(again['job_id'], queued['job_id'])
        self.assertTrue(again['already_running'])

    def test_single_issue_validation_and_owner_isolation(self):
        for invalid in (0, -1, True, '647338', 1.5):
            response = self.client.post('/api/redmine-agent/daily-brief/analyze-issue', json={'issue_id': invalid})
            self.assertEqual(response.status_code, 422)
        queued = self.client.post('/api/redmine-agent/daily-brief/analyze-issue', json={'issue_id': 647338}).json()['data']
        url = '/api/redmine-agent/daily-brief/runs/' + queued['run_id']
        self.assertEqual(self.client.get(url).status_code, 200)
        self.assertEqual(self.client.get(url, headers={'x-test-owner': 'owner-b'}).status_code, 404)

    def test_active_single_issue_is_available_after_page_reload(self):
        queued = self.client.post(
            '/api/redmine-agent/daily-brief/analyze-issue',
            json={'issue_id': 647338},
        ).json()['data']

        response = self.client.get('/api/redmine-agent/daily-brief/active-issue')

        self.assertEqual(response.status_code, 200)
        payload = response.json()['data']
        self.assertEqual(payload['run']['run_id'], queued['run_id'])
        self.assertEqual(payload['run']['status'], 'pending')
        self.assertEqual(payload['issues'][0]['issue_id'], 647338)

    def test_single_issue_worker_analyzes_without_snapshot_scan(self):
        from features.redmine.daily_brief_worker import _execute_job

        queued = self.client.post('/api/redmine-agent/daily-brief/analyze-issue', json={'issue_id': 647338}).json()['data']
        repo = brief_repo.owner_daily_brief_repository('owner-a')
        with patch('features.redmine.daily_brief_service.preflight_gms_auth', AsyncMock(return_value=(True, ''))):
            asyncio.run(_execute_job(repo, repo.get_job(queued['job_id']), lambda owner: daily_brief_api.DailyBriefService(owner)))
        self.triage_mock.assert_not_awaited()
        self.assertEqual(repo.get_issue(queued['run_id'], 647338).status, 'completed')
        self.assertEqual(len(repo.list_issues(queued['run_id'])), 1)


    def test_triage_returns_snapshot(self):
        resp = self.client.get("/api/redmine-agent/daily-brief/triage")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        self.assertEqual(data["counts"]["total"], 1)
        self.assertEqual(data["issues"][0]["buckets"], ["waiting_my_reply"])

    def test_triage_without_credentials_returns_guidance(self):
        self.has_creds = False
        resp = self.client.get("/api/redmine-agent/daily-brief/triage")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["success"])
        self.assertFalse(body["data"]["configured"])
        self.assertIn("message", body["data"])

    # ------------------------------------------------------------------ run

    def test_manual_run_is_durably_queued_without_web_background_task(self):
        resp = self.client.post("/api/redmine-agent/daily-brief/run")
        self.assertEqual(resp.status_code, 200)
        queued = resp.json()["data"]
        run_id = queued["run_id"]
        self.assertTrue(run_id.startswith("db_"))
        self.assertTrue(queued["job_id"].startswith("dbj_"))
        self.assertEqual(queued["status"], "pending")

        latest = self.client.get("/api/redmine-agent/daily-brief/latest")
        payload = latest.json()["data"]
        self.assertEqual(payload["run"]["run_id"], run_id)
        self.assertEqual(payload["run"]["status"], "pending")
        self.assertEqual(payload["issues"], [])
        repo = brief_repo.owner_daily_brief_repository("owner-a")
        self.assertEqual(repo.get_job(queued["job_id"])["status"], "queued")

    def test_manual_run_is_idempotent_same_day(self):
        first = self.client.post("/api/redmine-agent/daily-brief/run").json()["data"]["run_id"]
        second = self.client.post("/api/redmine-agent/daily-brief/run").json()["data"]
        self.assertEqual(second["run_id"], first)
        self.assertTrue(second.get("reused") or second.get("already_running"))

        forced = self.client.post(
            "/api/redmine-agent/daily-brief/run", json={"force": True}
        ).json()["data"]
        self.assertEqual(forced["run_id"], first)
        self.assertTrue(forced.get("already_running"))

    def test_manual_run_rejects_non_boolean_force(self):
        resp = self.client.post(
            "/api/redmine-agent/daily-brief/run", json={"force": "yes"}
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("boolean", resp.json()["error"])

    def test_run_rejects_invalid_mode(self):
        resp = self.client.post(
            "/api/redmine-agent/daily-brief/run?mode=weekly"
        )
        self.assertEqual(resp.status_code, 400)

    def test_run_without_credentials_returns_guidance(self):
        self.has_creds = False
        resp = self.client.post("/api/redmine-agent/daily-brief/run")
        body = resp.json()
        self.assertFalse(body["data"]["configured"])

    def test_cancelled_run_allows_single_issue_reanalysis(self):
        """UI 在 cancelled 详情中仍提供单条重试，API 必须可达。"""
        queued = self.client.post(
            "/api/redmine-agent/daily-brief/run"
        ).json()["data"]
        repo = brief_repo.owner_daily_brief_repository("owner-a")
        run = repo.get_run(queued["run_id"])
        repo.upsert_issue(brief_repo.DailyBriefIssue(
            run_id=run.run_id, issue_id=101, buckets=[], status="pending"
        ))
        self.assertTrue(repo.request_cancel(run.run_id))
        run.status = "cancelled"
        repo.update_run(run)

        response = self.client.post(
            f"/api/redmine-agent/daily-brief/{run.brief_date}/issues/101/reanalyze"
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(repo.is_cancel_requested(run.run_id))

    # ------------------------------------------------------------------ date

    def test_reanalysis_of_running_issue_is_conflict(self):
        queued = self.client.post("/api/redmine-agent/daily-brief/run").json()["data"]
        repo = brief_repo.owner_daily_brief_repository("owner-a")
        run = repo.get_run(queued["run_id"])
        repo.upsert_issue(brief_repo.DailyBriefIssue(
            run_id=run.run_id, issue_id=101, buckets=[], status="pending",
        ))
        for path in (
            f"runs/{run.run_id}/issues/101/reanalyze",
            f"{run.brief_date}/issues/101/reanalyze",
        ):
            response = self.client.post(f"/api/redmine-agent/daily-brief/{path}")
            self.assertEqual(response.status_code, 409)
            self.assertEqual(response.json()["code"], "STATE_CONFLICT")

    def test_get_brief_for_date_validates_and_404(self):
        bad = self.client.get("/api/redmine-agent/daily-brief/not-a-date")
        self.assertEqual(bad.status_code, 400)
        missing = self.client.get("/api/redmine-agent/daily-brief/2020-01-01")
        self.assertEqual(missing.status_code, 404)

    # ------------------------------------------------------------------ config

    def test_config_get_put_roundtrip(self):
        resp = self.client.put(
            "/api/redmine-agent/daily-brief/config",
            json={"model": "glm-4", "max_parallel_issues": 3},
        )
        self.assertEqual(resp.status_code, 200)
        got = self.client.get("/api/redmine-agent/daily-brief/config").json()["data"]
        self.assertEqual(got["model"], "glm-4")
        self.assertEqual(got["max_parallel_issues"], 3)

    def test_config_rejects_unknown_keys(self):
        resp = self.client.put(
            "/api/redmine-agent/daily-brief/config",
            json={"typo_key": 1},
        )
        self.assertEqual(resp.status_code, 400)

    def test_model_options_expose_only_safe_model_metadata(self):
        safe_options = {
            "default_model": "glm-5.3-flash",
            "models": [{
                "model": "glm-5.3-flash",
                "display_name": "GLM Local",
                "provider": "glm_local",
            }],
        }
        with patch.object(daily_brief_api, "list_daily_brief_model_options", return_value=safe_options):
            response = self.client.get("/api/redmine-agent/daily-brief/model-options")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"], safe_options)

    def test_agent_profiles_expose_names_only(self):
        profiles = {"profiles": ["kkagent-local"], "default_profile": "kkagent-local"}
        with patch.object(daily_brief_api, "list_daily_brief_agent_profiles", return_value=profiles):
            response = self.client.get("/api/redmine-agent/daily-brief/agent-profiles")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"], profiles)

    # ------------------------------------------------------------------ owner

    def test_runs_are_owner_isolated(self):
        a = self.client.post(
            "/api/redmine-agent/daily-brief/run", headers={"x-test-owner": "owner-a"}
        ).json()["data"]["run_id"]
        b = self.client.post(
            "/api/redmine-agent/daily-brief/run", headers={"x-test-owner": "owner-b"}
        ).json()["data"]["run_id"]
        self.assertNotEqual(a, b)


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
