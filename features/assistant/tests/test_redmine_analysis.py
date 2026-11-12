"""Durable Redmine runs are followed once, using the original owner and run id."""

import asyncio
import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from features.assistant import project_agent, session_registry, session_store
from features.assistant import redmine_analysis as analysis
from features.assistant.kkagent_bridge import BridgeTurnResult
from features.assistant.session_log import _append_message
from features.assistant.tools import registry
from features.auth import CurrentUser
from foundation.error_model import ApiError


def request():
    return SimpleNamespace(state=SimpleNamespace(current_user=CurrentUser(
        id="owner-a", username="qa", role="user"), auth_method="session"), headers={}, cookies={},
        client=SimpleNamespace(host="127.0.0.1", port=80), method="POST", url=None, query_params={})


def session():
    current = {"session_id": str(uuid.uuid4()), "client_id": "browser", "owner_id": "owner-a",
               "status": "idle", "messages": [], "steps": [], "pending_plan": None,
               "active_run": None, "workspace_context": {}, "created_at": session_registry._now_iso()}
    session_registry._agent_sessions[current["session_id"]] = current
    return current


def status(state, *, report="", issue_state=None, run_id="run-1", previous=""):
    return {"success": True, "data": {
        "run": {"run_id": run_id, "status": state},
        "issues": [{"issue_id": 661332, "status": issue_state or state,
                    "result": {"detailed_report": report},
                    "previous_analysis": {"result": {"detailed_report": previous}}}],
    }}


@pytest_asyncio.fixture
async def environment(monkeypatch, tmp_path):
    saved = dict(session_registry._agent_sessions)
    saved_tasks = dict(session_registry._agent_monitor_tasks)
    session_registry._agent_sessions.clear()
    session_registry._agent_monitor_tasks.clear()
    monkeypatch.setattr(session_store, "default_sessions_path", lambda: tmp_path / "anchors.json")
    monkeypatch.setattr(session_store, "default_transcripts_dir", lambda: tmp_path / "transcripts")
    monkeypatch.setattr(analysis, "POLL_SECONDS", 0)
    submit = AsyncMock(return_value={"success": True, "data": {
        "run_id": "run-1", "job_id": "job-1", "issue_id": 661332, "status": "pending", "queued": True,
    }})
    read = AsyncMock(return_value=status("completed", report="本次日志分析结果"))
    monkeypatch.setattr(analysis, "analyze_issue_for_assistant", submit)
    monkeypatch.setattr(analysis, "get_analysis_for_assistant", read)
    yield submit, read
    tasks = list(session_registry._agent_monitor_tasks.values())
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    session_registry._agent_sessions.clear()
    session_registry._agent_sessions.update(saved)
    session_registry._agent_monitor_tasks.clear()
    session_registry._agent_monitor_tasks.update(saved_tasks)


def test_tools_describe_single_issue_analysis_and_required_attachment_url():
    tool = registry.get(analysis.START_TOOL)
    assert tool and not tool.is_readonly and not tool.requires_confirm
    assert {item["name"] for item in tool.params if item.get("required")} == {"issue_ref"}
    assert registry.get(analysis.STATUS_TOOL).is_readonly
    assert analysis.START_TOOL in {item.tool.name for item in registry.find("分析 Redmine 工单", top_k=8)}
    attachment = registry.get("reports_extract_redmine_attachment")
    assert {item["name"] for item in attachment.params if item.get("required")} == {"issue_url"}


@pytest.mark.asyncio
async def test_submit_follows_original_run_until_current_report_is_ready(environment):
    submit, read = environment
    read.side_effect = [status("pending", report="旧结果", previous="历史报告"), status("running"),
                        status("completed", report="kernel_vendor.log：本次证据及结论")]
    current, req = session(), request()
    outcome = await analysis.start_analysis(current, req, {"issue_ref": "https://redmine.example.test/issues/661332"})
    assert outcome.success and current["status"] == "monitoring"
    task = session_registry._agent_monitor_tasks[current["session_id"]]
    await asyncio.wait_for(task, 2)
    assert submit.await_count == 1 and read.await_count == 3
    assert all(call.args[1] == "run-1" for call in read.await_args_list)
    assert all(call.args[0].state.current_user.id == "owner-a" for call in read.await_args_list)
    assert current["status"] == "idle" and current["active_run"] is None
    transcript = json.dumps(current["messages"], ensure_ascii=False)
    assert "本次证据及结论" in transcript and "旧结果" not in transcript and "历史报告" not in transcript
    assert current["steps"][-1]["status"] == "done"


@pytest.mark.asyncio
async def test_running_task_rejects_duplicate_submission(environment):
    submit, read = environment
    gate = asyncio.Event()

    async def wait(*args):
        await gate.wait()
        return status("completed", report="分析结果")

    read.side_effect = wait
    current = session()
    await analysis.start_analysis(current, request(), {"issue_ref": "661332"})
    duplicate = await analysis.start_analysis(current, request(), {"issue_ref": "661332"})
    assert not duplicate.success and duplicate.data["status_code"] == 409
    assert submit.await_count == 1
    gate.set()
    await asyncio.wait_for(session_registry._agent_monitor_tasks[current["session_id"]], 2)


@pytest.mark.asyncio
async def test_failed_submission_does_not_create_running_state(environment):
    submit, _ = environment
    submit.return_value = ApiError.dependency_unavailable("请配置 Redmine 凭据。").to_response()
    current = session()
    outcome = await analysis.start_analysis(current, request(), {"issue_ref": "661332"})
    assert not outcome.success and outcome.data["status_code"] == 503
    assert current["active_run"] is None and current["status"] == "idle"
    assert current["session_id"] not in session_registry._agent_monitor_tasks


@pytest.mark.asyncio
@pytest.mark.parametrize("dispatch", ["confirmed", "action", "rules"])
async def test_chat_dispatch_keeps_polling_the_background_analysis(environment, monkeypatch, dispatch):
    from features.assistant import api, intent

    submit, read = environment
    gate = asyncio.Event()

    async def wait(*args):
        await gate.wait()
        return status("completed", report="本次分析结果")

    read.side_effect = wait
    current = session()
    params = {"issue_ref": "661332"}
    if dispatch == "confirmed":
        current["pending_plan"] = {"type": "generic_action", "intent": {
            "tool_name": analysis.START_TOOL, "params": params, "category": "redmine",
        }}
    elif dispatch == "rules":
        monkeypatch.setenv("GMS_ASSISTANT_KKAGENT_ENABLED", "0")
        monkeypatch.setattr(intent, "resolve", lambda *_: SimpleNamespace(
            tool_name=analysis.START_TOOL, confidence=1, params=params,
            tool=registry.get(analysis.START_TOOL), is_run_test=False, needs_confirm=False,
        ))
    monkeypatch.setattr(api, "cleanup_due", lambda: False)
    monkeypatch.setattr(api, "get_client_id_from_request", lambda req: "browser")
    response = await api.agent_chat(request(), api.AgentChatRequest(
        session_id=current["session_id"], message="分析工单661332", execute=dispatch == "confirmed",
        action=analysis.START_TOOL if dispatch == "action" else None, params=params,
    ))
    assert json.loads(response.body)["data"]["session"]["status"] == "monitoring"
    assert current["pending_plan"] is None and submit.await_count == 1
    gate.set()
    await asyncio.wait_for(session_registry._agent_monitor_tasks[current["session_id"]], 2)


@pytest.mark.asyncio
async def test_concurrent_submissions_are_rejected_before_enqueueing_twice(environment):
    submit, _ = environment
    entered, release = asyncio.Event(), asyncio.Event()
    acknowledgement = submit.return_value

    async def enqueue(*args):
        entered.set()
        await release.wait()
        return acknowledgement

    submit.side_effect = enqueue
    current = session()
    first = asyncio.create_task(analysis.start_analysis(current, request(), {"issue_ref": "661332"}))
    try:
        await entered.wait()
        duplicate = await analysis.start_analysis(current, request(), {"issue_ref": "661332"})
        assert not duplicate.success and duplicate.data["status_code"] == 409
        assert submit.await_count == 1
    finally:
        release.set()
        outcome = await asyncio.wait_for(first, 2)
    assert outcome.success and "_redmine_submitting" not in current
    task = session_registry._agent_monitor_tasks.get(current["session_id"])
    if task:
        await asyncio.wait_for(task, 2)


@pytest.mark.asyncio
async def test_query_excludes_stale_or_foreign_run_results(environment):
    _, read = environment
    read.return_value = status("running", report="旧结果", previous="历史报告")
    outcome = await analysis.query_analysis(session(), request(), {"run_id": "run-1"})
    assert "旧结果" not in json.dumps(outcome.to_dict(), ensure_ascii=False)
    assert "历史报告" not in json.dumps(outcome.to_dict(), ensure_ascii=False)
    read.return_value = status("completed", report="错误任务", run_id="run-2")
    outcome = await analysis.query_analysis(session(), request(), {"run_id": "run-1"})
    assert not outcome.success and outcome.data["status_code"] == 502


@pytest.mark.asyncio
async def test_model_turn_submits_analysis_without_waiting_or_calling_report_parser(environment, monkeypatch):
    submit, read = environment
    gate = asyncio.Event()

    async def wait(*args):
        await gate.wait()
        return status("completed", report="基于日志的分析")

    read.side_effect = wait
    monkeypatch.setenv("GMS_ASSISTANT_KKAGENT_ENABLED", "1")
    monkeypatch.setattr(project_agent.kkagent_bridge, "availability", lambda: {"available": True})
    model = AsyncMock(return_value=BridgeTurnResult(ok=True, text=json.dumps({
        "type": "call", "tool": analysis.START_TOOL,
        "params": {"issue_ref": "https://redmine.example.test/issues/661332"},
    })))
    monkeypatch.setattr(project_agent.kkagent_bridge, "run_turn", model)
    current = session()
    await project_agent.handle_project_turn(current, request(), SimpleNamespace(
        message="分析 https://redmine.example.test/issues/661332", action=None, params={}))
    assert submit.await_count == 1 and model.await_count == 1
    assert current["status"] == "monitoring"
    assert analysis.START_TOOL in model.await_args.kwargs["context"]
    gate.set()
    await asyncio.wait_for(session_registry._agent_monitor_tasks[current["session_id"]], 2)


@pytest.mark.asyncio
async def test_anchor_resumes_run_and_retains_conversation(environment, monkeypatch):
    from features.assistant import api

    _, read = environment
    current = session()
    _append_message(current, "user", "原始工单请求")
    gate = asyncio.Event()

    async def pending(*args):
        await gate.wait()
        return status("pending")

    read.side_effect = pending
    await analysis.start_analysis(current, request(), {"issue_ref": "661332"})
    sid = current["session_id"]
    task = session_registry._agent_monitor_tasks[sid]
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    session_registry._agent_monitor_tasks.pop(sid, None)
    session_registry._agent_sessions.pop(sid)
    assert session_registry.restore_session(sid, "other-browser", "other-owner") is None
    read.side_effect = None
    read.return_value = status("completed", report="恢复后的本次结果")
    monkeypatch.setattr(api, "get_client_id_from_request", lambda req: "browser-new")
    await api.get_agent_session(sid, request())
    await asyncio.wait_for(session_registry._agent_monitor_tasks[sid], 2)
    restored = session_registry._agent_sessions[sid]
    transcript = json.dumps(restored["messages"], ensure_ascii=False)
    assert "原始工单请求" in transcript and transcript.count("恢复后的本次结果") == 1
    assert not any(step["status"] == "running" for step in restored["steps"])
    await api.get_agent_session(sid, request())
    assert json.dumps(restored["messages"], ensure_ascii=False).count("恢复后的本次结果") == 1


@pytest.mark.asyncio
async def test_old_monitor_cannot_finish_replacement_run(environment):
    _, read = environment
    entered, release = asyncio.Event(), asyncio.Event()

    async def wait(*args):
        entered.set()
        await release.wait()
        return status("completed", report="旧监控结果")

    read.side_effect = wait
    current = session()
    await analysis.start_analysis(current, request(), {"issue_ref": "661332"})
    task = session_registry._agent_monitor_tasks[current["session_id"]]
    await entered.wait()
    replacement = {"redmine_run_id": "run-new", "redmine_issue_id": 661333, "monitor_token": "replacement"}
    current["active_run"] = replacement
    release.set()
    await asyncio.wait_for(task, 2)
    assert current["active_run"] is replacement
    assert "旧监控结果" not in json.dumps(current["messages"], ensure_ascii=False)


@pytest.mark.asyncio
async def test_cancel_only_stops_tracking_and_retains_run_for_followup(environment, monkeypatch):
    from features.assistant import api

    submit, read = environment
    gate = asyncio.Event()

    async def pending(*args):
        await gate.wait()
        return status("pending")

    read.side_effect = pending
    current = session()
    await analysis.start_analysis(current, request(), {"issue_ref": "661332"})
    task = session_registry._agent_monitor_tasks[current["session_id"]]
    monkeypatch.setattr(api, "get_client_id_from_request", lambda req: "browser")
    await api.cancel_agent_session(current["session_id"], request())
    await asyncio.gather(task, return_exceptions=True)
    assert current["status"] == "cancelled" and current["active_run"] is None
    assert submit.await_count == 1
    assert "后台任务仍可" in current["messages"][-1]["content"]
    assert "run-1" in project_agent._context(current, [])


@pytest.mark.asyncio
async def test_failed_run_does_not_deliver_previous_report(environment):
    _, read = environment
    read.return_value = status("failed", report="旧结果", issue_state="failed", previous="历史报告")
    current = session()
    await analysis.start_analysis(current, request(), {"issue_ref": "661332"})
    await asyncio.wait_for(session_registry._agent_monitor_tasks[current["session_id"]], 2)
    assert "分析失败" in current["messages"][-1]["content"]
    assert "旧结果" not in current["messages"][-1]["content"]
    assert current["steps"][-1]["status"] == "warning"


@pytest.mark.asyncio
async def test_monitor_timeout_does_not_resubmit_or_cancel_analysis(environment, monkeypatch):
    submit, read = environment
    monkeypatch.setattr(analysis, "MAX_TRACKING_SECONDS", 0)
    current = session()
    await analysis.start_analysis(current, request(), {"issue_ref": "661332"})
    await asyncio.wait_for(session_registry._agent_monitor_tasks[current["session_id"]], 2)
    assert submit.await_count == 1
    read.assert_not_awaited()
    assert current["status"] == "idle"
    assert "等待超时" in current["steps"][-1]["detail"]


@pytest.mark.asyncio
@pytest.mark.parametrize("error, attempts", [(ApiError.not_found("分析任务不存在。"), 1),
                                          (ApiError.dependency_unavailable("依赖暂不可用。"), 3)])
async def test_monitor_read_failure_preserves_run_without_resubmission(environment, error, attempts):
    submit, read = environment
    read.return_value = error.to_response()
    current = session()
    await analysis.start_analysis(current, request(), {"issue_ref": "661332"})
    await asyncio.wait_for(session_registry._agent_monitor_tasks[current["session_id"]], 2)
    assert read.await_count == attempts and submit.await_count == 1
    assert current["status"] == "idle" and current["active_run"] is None
    assert "无法继续跟踪" in current["messages"][-1]["content"]
    assert "run-1" in project_agent._context(current, [])
