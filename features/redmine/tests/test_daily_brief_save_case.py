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


# 诊断结果：带 gate（diagnostic 模式）+ 根因结论。
DIAGNOSTIC_RESULT = {
    "problem_summary": "RK3588 CtsCarrierApiTestCases fail",
    "root_cause": "modem 未实现 UICC ARA 逻辑通道",
    "root_cause_type": "likely",
    "suggested_solution": "① 厂商 RIL 支持 ARA",
    "confidence": 0.85,
    "evidence_gate": {"analysis_mode": "diagnostic"},
}

# triage 结果：根因为空/占位、root_cause_type=unknown（无 gate 的旧数据）。


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
    for run_id, mode, owner in (("old", "manual", "owner"), ("new", "delta", "owner"), ("foreign", "manual", "other"), ("native", "issue:101", "owner")):
        run = DailyBriefRun(owner_id=owner, brief_date="2026-09-15", mode=mode, run_id=run_id, status="completed")
        repository.create_run(run)
        result = DIAGNOSTIC_RESULT if run_id == "old" else {"problem_summary": run_id}
        if run_id == "new":
            result = dict(result, evidence_gate={"analysis_mode": "triage"}, root_cause="", root_cause_type="unknown")
        if run_id == "native":
            # native 摘要：diagnostic gate + result_format 标记（ADR 0009）。
            result = {
                "result_format": "kkagent_markdown",
                "detailed_report": "## 结论\n…",
                "evidence_gate": {"analysis_mode": "diagnostic"},
            }
        repository.upsert_issue(DailyBriefIssue(
            run_id=run_id, issue_id=101, buckets=[], status="completed", result=result,
        ))
    with TestClient(app) as client:
        yield client, sink


def test_save_case_uses_explicit_daily_brief_run(case_client):
    client, sink = case_client
    response = client.post("/daily-brief/2026-09-15/issues/101/save-case?run_id=old")
    assert response.status_code == 200
    fact = sink.upsert_case_fact.call_args.args[0]
    assert fact["problem_summary"] == "RK3588 CtsCarrierApiTestCases fail"
    # diagnostic 结果的执行状态（completed）不进入 status_name。
    assert fact["status_name"] == ""
    # merge_missing 落库语义：AI 结论不整行覆盖已有事实。
    assert sink.upsert_case_fact.call_args.kwargs.get("merge_missing") is True


def test_save_case_rejects_triage_result(case_client):
    """triage 待办摘要禁止沉淀为知识库案例。"""
    client, sink = case_client
    response = client.post("/daily-brief/2026-09-15/issues/101/save-case?run_id=new")
    assert response.status_code == 409
    assert response.json()["code"] == "STATE_CONFLICT"
    sink.upsert_case_fact.assert_not_called()


def test_save_case_rejects_native_markdown_summary(case_client):
    """ADR 0009：native 摘要无结构化字段，禁止保存为案例（挡 API 直连）。"""
    client, sink = case_client
    response = client.post("/daily-brief/2026-09-15/issues/101/save-case?run_id=native")
    assert response.status_code == 409
    assert response.json()["code"] == "STATE_CONFLICT"
    assert "native markdown summary" in response.json()["error"]
    sink.upsert_case_fact.assert_not_called()


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
