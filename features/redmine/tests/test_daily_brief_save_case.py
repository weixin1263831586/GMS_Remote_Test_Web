"""Save-case HTTP routing uses the owner's daily-brief repository."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from features.auth import CurrentUser
from features.redmine import knowledge_api
from features.redmine.daily_brief_models import DailyBriefIssue, DailyBriefRun
from features.redmine.daily_brief_repository import DailyBriefRepository


@pytest.fixture
def case_client(tmp_path, monkeypatch):
    repository = DailyBriefRepository(tmp_path)
    sink = Mock()
    monkeypatch.setattr(knowledge_api, "owner_daily_brief_repository", lambda _owner: repository)
    monkeypatch.setattr(knowledge_api, "_knowledge", lambda _request: SimpleNamespace(knowledge_db=sink))
    app = FastAPI()

    @app.middleware("http")
    async def identity(request: Request, call_next):
        request.state.current_user = CurrentUser(id="owner", username="owner", role=request.headers.get("role", "admin"))
        request.state.auth_method = "agent_token" if request.headers.get("role") == "agent_service" else "session"
        return await call_next(request)

    app.include_router(knowledge_api.router)
    for run_id, mode, owner in (("old", "manual", "owner"), ("new", "delta", "owner"), ("foreign", "manual", "other")):
        run = DailyBriefRun(owner_id=owner, brief_date="2026-09-15", mode=mode, run_id=run_id, status="completed")
        repository.create_run(run)
        repository.upsert_issue(DailyBriefIssue(
            run_id=run_id, issue_id=101, buckets=[], status="completed", result={"problem_summary": run_id},
        ))
    with TestClient(app) as client:
        yield client, sink


def test_save_case_uses_explicit_daily_brief_run(case_client):
    client, sink = case_client
    response = client.post("/daily-brief/2026-09-15/issues/101/save-case?run_id=old")
    assert response.status_code == 200
    assert sink.upsert_case_fact.call_args.args[0]["problem_summary"] == "old"


def test_save_case_does_not_read_another_owner_run(case_client):
    client, sink = case_client
    response = client.post("/daily-brief/2026-09-15/issues/101/save-case?run_id=foreign")
    assert response.status_code == 404
    assert response.json()["code"] == "NOT_FOUND"
    sink.upsert_case_fact.assert_not_called()


def test_save_case_rejects_agent_write(case_client, monkeypatch):
    monkeypatch.setenv("GMS_AUTH_REQUIRED", "true")
    client, sink = case_client
    response = client.post("/daily-brief/2026-09-15/issues/101/save-case?run_id=old", headers={"role": "agent_service"})
    assert response.status_code == 403
    sink.upsert_case_fact.assert_not_called()
