"""A delayed start must not revive cancelled sessions or overwrite a new run."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from features.assistant import api, session_store


@pytest.fixture
def context(monkeypatch):
    session = {
        "session_id": "start-race", "client_id": "alice", "owner_id": "alice",
        "messages": [], "steps": [], "workspace_context": {}, "active_run": None,
    }
    plan = {"request": {"devices": ["ABC"], "test_suite": "cts", "worker_id": "w1"}}
    request = SimpleNamespace(headers={}, cookies={}, client=None)
    monkeypatch.setattr(api, "owner_id_from_request", lambda _: "alice")
    monkeypatch.setattr(api, "_latest_report_for_client", lambda _: None)
    monitor = AsyncMock()
    monkeypatch.setattr(api, "_monitor_agent_run", monitor)
    monkeypatch.setattr(api, "_run_pre_actions", AsyncMock(return_value={"success": True}))
    start = AsyncMock(return_value={"success": True})
    monkeypatch.setattr(api, "_start_test_with_plan", start)
    return session, plan, request, start, monitor


@pytest.mark.asyncio
@pytest.mark.parametrize("replace", [False, True])
@pytest.mark.parametrize("stage", ["_run_pre_actions", "_start_test_with_plan"])
@pytest.mark.parametrize("succeeded", [False, True])
async def test_delayed_start_cannot_mutate_replaced_or_cancelled_run(context, monkeypatch, replace, stage, succeeded):
    session, plan, request, start, monitor = context
    entered = asyncio.Event()
    release = asyncio.Event()

    async def delayed(*_args):
        entered.set()
        await release.wait()
        return {"success": succeeded}

    monkeypatch.setattr(api, stage, delayed)
    task = asyncio.create_task(api._execute_plan(session, request, plan))
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        replacement = {"monitor_token": "new", "cluster_job_id": "new-job"} if replace else None
        session["active_run"] = replacement
        session["status"] = "monitoring" if replace else "cancelled"
        if replace:
            api._save_session_anchor(session)
        else:
            api._finish_run_state(session, "cancelled")
        release.set()
        result = await asyncio.wait_for(task, timeout=5)
        await asyncio.sleep(0)
        assert result["success"] is False
        assert session["active_run"] is replacement
        assert session["status"] == ("monitoring" if replace else "cancelled")
        monitor.assert_not_awaited()
        if stage == "_run_pre_actions":
            start.assert_not_awaited()
        anchors = session_store.load_anchors()
        if replace:
            assert anchors[session["session_id"]]["active_run"]["cluster_job_id"] == "new-job"
        else:
            assert session["session_id"] not in anchors
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        monitor_task = api._agent_monitor_tasks.pop(session["session_id"], None)
        if monitor_task:
            await asyncio.gather(monitor_task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("replace", [False, True])
async def test_late_job_receipt_does_not_overwrite_run_or_workspace(monkeypatch, replace):
    from features import test_execution

    old_run = {"monitor_token": "old"}
    session = {
        "session_id": "receipt-race", "owner_id": "alice", "active_run": old_run,
        "steps": [], "workspace_context": {"cluster_job_id": "new-job"},
    }
    replacement = {"monitor_token": "new", "cluster_job_id": "new-job"} if replace else None

    async def delayed_start(*_args, **_kwargs):
        session["active_run"] = replacement
        session["status"] = "monitoring" if replace else "cancelled"
        return SimpleNamespace(body=json.dumps({
            "success": True,
            "data": {"cluster_job_id": "old-job", "attempt_id": "a1", "worker_id": "w1"},
        }).encode())

    monkeypatch.setattr(test_execution, "start_test", delayed_start)
    plan = {
        "operation_id": "op-old",
        "request": {"devices": ["ABC"], "test_suite": "cts", "worker_id": "w1"},
    }
    result = await api._start_test_with_plan(session, SimpleNamespace(), plan)
    assert result["success"] is False
    assert result["data"]["cluster_job_id"] == "old-job"
    assert session["active_run"] is replacement
    assert session["workspace_context"] == {"cluster_job_id": "new-job"}
    assert session["steps"] == []
    assert session["session_id"] not in session_store.load_anchors()
