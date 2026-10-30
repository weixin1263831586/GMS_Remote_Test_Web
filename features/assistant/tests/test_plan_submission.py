"""Cancelled confirmations can retry using the original durable operation key."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from features.assistant.plan_submission import submit_pending_plan_once


@pytest.mark.asyncio
async def test_cancelled_submission_is_retryable_without_changing_operation_id():
    plan = {"operation_id": "op-original", "submit_state": "pending"}
    entered = asyncio.Event()
    submitted_ids = []

    async def submit(current):
        submitted_ids.append(current["operation_id"])
        entered.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(submit_pending_plan_once(plan, submit))
    await asyncio.wait_for(entered.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert plan["submit_state"] == "failed"

    async def retry(current):
        submitted_ids.append(current["operation_id"])
        return {"success": True}

    result, hint = await submit_pending_plan_once(plan, retry)
    assert result == {"success": True}
    assert hint == ""
    assert submitted_ids == ["op-original", "op-original"]


@pytest.mark.asyncio
async def test_legacy_plan_gets_one_operation_id_before_first_submission():
    plan = {}
    operation_ids = []

    async def submit(current):
        operation_ids.append(current.get("operation_id"))
        return {"success": len(operation_ids) > 1}

    await submit_pending_plan_once(plan, submit)
    await submit_pending_plan_once(plan, submit)
    assert operation_ids[0]
    assert operation_ids[0] == operation_ids[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["_run_pre_actions", "_start_test_with_plan"])
@pytest.mark.parametrize("cancelled", [False, True])
async def test_start_exception_clears_run_and_anchor(monkeypatch, stage, cancelled):
    from features.assistant import api, session_store

    session = {"session_id": "s-start-error", "owner_id": "alice", "active_run": None}
    plan = {"request": {"devices": ["ABC"], "test_suite": "cts", "worker_id": "w1"}}
    request = SimpleNamespace(headers={}, cookies={}, client=None)
    failure = asyncio.CancelledError() if cancelled else RuntimeError("start failed")
    monkeypatch.setattr(api, "owner_id_from_request", lambda _: "alice")
    monkeypatch.setattr(api, "_latest_report_for_client", lambda _: None)
    monkeypatch.setattr(api, "_run_pre_actions", AsyncMock(return_value={"success": True}))
    monkeypatch.setattr(api, stage, AsyncMock(side_effect=failure))

    with pytest.raises(type(failure)):
        await api._execute_plan(session, request, plan)
    assert session["active_run"] is None
    assert session["status"] == ("cancelled" if cancelled else "error")
    assert "s-start-error" not in session_store.load_anchors()
    assert plan["operation_id"]


@pytest.mark.asyncio
async def test_old_monitor_finishing_analysis_keeps_new_run(monkeypatch):
    from features.assistant import api, session_registry

    session = {
        "session_id": "s-analysis", "client_id": "alice", "owner_id": "alice",
        "active_run": {
            "plan": {"policy": {"analyze_on_failure": True}}, "monitor_token": "old",
        },
        "messages": [], "steps": [],
    }
    replacement = {"monitor_token": "new", "cluster_job_id": "new-job"}

    async def finish_analysis(*_args):
        session["active_run"] = replacement
        session["status"] = "monitoring"

    monkeypatch.setitem(api._agent_sessions, session["session_id"], session)
    monkeypatch.setattr(api, "get_or_create_user_state", lambda _: {"running": False})
    monkeypatch.setattr(api, "_latest_report_for_client", lambda *_a, **_kw: {"timestamp": "r1"})
    monkeypatch.setattr(api, "_report_failed", lambda _: True)
    monkeypatch.setattr(api, "_run_failure_analysis_pipeline", finish_analysis)
    clear_anchor = Mock()
    monkeypatch.setattr(session_registry, "clear_session_anchor", clear_anchor)
    await api._monitor_agent_run(session["session_id"], SimpleNamespace(), "old")
    assert session["active_run"] is replacement
    assert session["status"] == "monitoring"
    clear_anchor.assert_not_called()
