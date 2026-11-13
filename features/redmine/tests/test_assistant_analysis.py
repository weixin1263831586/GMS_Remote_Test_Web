"""Conversation adapters preserve Redmine URL, credential and owner boundaries."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException

from features.auth import CurrentUser
from features.redmine import assistant_analysis as adapter
from features.redmine import daily_brief_api


def request(role="user", owner="owner-a", auth_method="session"):
    return SimpleNamespace(state=SimpleNamespace(
        current_user=CurrentUser(id=owner, username=owner, role=role), auth_method=auth_method,
    ), headers={}, cookies={})


@pytest.fixture
def base_url(monkeypatch):
    value = Mock(return_value="https://redmine.example.test")
    monkeypatch.setattr(adapter, "owner_base_url", value)
    from features.auth import access

    monkeypatch.setattr(access, "require_authenticated_user_when_auth_required", lambda req: req.state.current_user)
    return value


@pytest.mark.asyncio
@pytest.mark.parametrize("reference", ["661332", "https://redmine.example.test/issues/661332"])
async def test_analysis_reuses_typed_single_issue_route(base_url, monkeypatch, reference):
    dispatch = AsyncMock(return_value={"success": True, "data": {"run_id": "run-1"}})
    monkeypatch.setattr(adapter, "submit_single_issue_analysis", dispatch)
    req = request()
    result = await adapter.analyze_issue_for_assistant(req, reference, "full", "检查 kernel_vendor.log")
    assert result["success"]
    base_url.assert_called_once_with("owner-a")
    assert dispatch.await_args.args[0] is req
    payload = dispatch.await_args.args[1]
    assert payload.issue_id == 661332 and payload.analysis_mode == "full"
    assert payload.analysis_hint == "检查 kernel_vendor.log"
    assert payload.device_serials == [] and payload.device_serial == ""
    assert dispatch.await_args.kwargs == {"output_style": "summary"}


@pytest.mark.asyncio
@pytest.mark.parametrize("reference", ["0", "999999999999999999999999", "https://other.test/issues/661332",
                                       "https://[broken/issues/661332", "kernel_vendor.log", None])
async def test_invalid_or_foreign_issue_ref_never_enqueues(base_url, monkeypatch, reference):
    dispatch = AsyncMock()
    monkeypatch.setattr(adapter, "submit_single_issue_analysis", dispatch)
    response = await adapter.analyze_issue_for_assistant(request(), reference)
    assert response.status_code == 400
    dispatch.assert_not_awaited()


@pytest.mark.asyncio
async def test_agent_token_cannot_start_analysis(base_url, monkeypatch):
    dispatch = AsyncMock()
    monkeypatch.setattr(adapter, "submit_single_issue_analysis", dispatch)
    with pytest.raises(HTTPException) as error:
        await adapter.analyze_issue_for_assistant(request("agent_service", auth_method="agent_token"), "661332")
    assert error.value.status_code == 403
    dispatch.assert_not_awaited()
    base_url.assert_not_called()


@pytest.mark.asyncio
async def test_missing_credentials_remains_an_actionable_dependency_error(base_url, monkeypatch):
    monkeypatch.setattr(daily_brief_api, "_has_redmine_credentials", lambda req: False)
    response = await adapter.analyze_issue_for_assistant(request(), "661332")
    assert response.status_code == 503
    assert "Redmine" in response.body.decode()


@pytest.mark.asyncio
async def test_status_does_not_return_another_owners_run(monkeypatch):
    service = SimpleNamespace(owner_id="owner-a", repository=SimpleNamespace(
        get_run=Mock(return_value=SimpleNamespace(owner_id="owner-b"))), run_payload=Mock())
    monkeypatch.setattr(daily_brief_api, "_service_for_request", lambda req: service)
    response = await adapter.get_analysis_for_assistant(request(), "run-of-b")
    assert response.status_code == 404
    service.run_payload.assert_not_called()
