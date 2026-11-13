"""Different project commands share model discovery, resource questions and confirmation."""

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from features.assistant import project_agent as agent
from features.assistant.execution_result import ToolResult
from features.assistant.kkagent_bridge import BridgeTurnResult


@pytest.fixture
def model(monkeypatch):
    agent.recovery._checkpoints.clear()
    monkeypatch.setenv("GMS_ASSISTANT_KKAGENT_ENABLED", "1")
    monkeypatch.setattr(agent.kkagent_bridge, "availability", lambda: {"available": True})
    turn = AsyncMock()
    monkeypatch.setattr(agent.kkagent_bridge, "run_turn", turn)
    execute = AsyncMock(return_value=ToolResult(success=True, tool_name="devices_list", data={"devices": ["DEVICE-1"]}))
    monkeypatch.setattr(agent.executor, "execute", execute)
    return turn, execute


def decision(kind, **fields):
    return BridgeTurnResult(ok=True, text=json.dumps({"type": kind, **fields}), session_id="model-session")


def session():
    return {"session_id": "project-session", "status": "idle", "messages": [], "steps": [],
            "workspace_context": {"worker_id": "worker-1", "scope_mode": "cluster"}}


def request(message="帮我处理项目任务", **kwargs):
    return SimpleNamespace(message=message, action=kwargs.get("action"), params=kwargs.get("params") or {})


@pytest.mark.asyncio
async def test_latest_gts_goal_sets_real_query_constraints_when_model_omits_them(model):
    turn, execute = model
    turn.side_effect = [decision("call", tool="reports_list", params={}),
                        decision("answer", text="已查询当前 Worker 的 GTS 结果。")]
    current = session()
    assert await agent.handle_project_turn(current, object(), request("最新GTS测试报告"))
    assert execute.await_args.args[2:] == ("reports_list", {
        "worker_id": "worker-1", "test_type": "GTS", "latest": True, "limit": 1,
    })


@pytest.mark.asyncio
async def test_final_answer_preserves_controller_bound_native_report_navigation(model):
    turn, execute = model
    actions = [{"label": "查看原生报告目录", "page": "test-suites", "params": {
        "worker_id": "worker-1", "suite_path": "/suites/android-gts/tools", "path": "results/run-1",
    }}]
    execute.return_value = ToolResult(success=True, tool_name="reports_list", quick_actions=actions,
                                     data={"reports": [{"registered": False, "access": {"report_management": False}}]})
    turn.side_effect = [decision("call", tool="reports_list", params={}),
                        decision("answer", text="已找到 GTS 原生结果，可通过下方按钮查看目录。")]
    current = session()
    assert await agent.handle_project_turn(current, object(), request("最新GTS测试报告"))
    assert current["messages"][-1]["data"]["quick_actions"] == actions
    assert "report_timestamp" not in current["messages"][-1]["data"]
    assert "access.open_action" in turn.await_args_list[0].kwargs["context"]


@pytest.mark.asyncio
async def test_arbitrary_command_discovers_queries_and_answers_with_evidence(model):
    turn, execute = model
    turn.side_effect = [decision("discover", query="设备"), decision("describe", tools=["devices_list"]),
                        decision("call", tool="devices_list", params={}), decision("answer", text="设备 DEVICE-1 可用")]
    current = session()
    assert await agent.handle_project_turn(current, object(), request("找一下今天能用的板子"))
    assert execute.await_count == 1
    assert current["agent_backend"] == {"mode": "kkagent", "state": "ready"}
    assert current["messages"][-1]["content"] == "设备 DEVICE-1 可用"
    assert "DEVICE-1" in turn.call_args.kwargs["context"]
    assert turn.call_args.kwargs["session_id"] == "model-session"


@pytest.mark.asyncio
async def test_chat_endpoint_uses_the_model_before_missing_parameter_rules(model, monkeypatch):
    from features.assistant import api
    from features.assistant.tests.test_agent_session_safety import _bare_request, _seed_confirm_session

    turn, execute = model
    turn.return_value = decision("question", text="请选择设备", fields=[{
        "name": "device", "label": "设备", "options": [{"value": "DEV-1", "label": "设备一"}],
    }])
    saved = dict(api._agent_sessions)
    try:
        current = _seed_confirm_session(api, {})
        current["pending_plan"] = None
        monkeypatch.setattr(api, "cleanup_due", lambda: False)
        await api.agent_chat(_bare_request(), api.AgentChatRequest(session_id="s-confirm", message="烧写固件"))
        await asyncio.wait_for(agent._project_tasks[current["session_id"]], 5)
        assert current["messages"][-1]["kind"] == "question"
        assert "还缺少必要参数" not in current["messages"][-1]["content"]
        assert current["agent_backend"]["mode"] == "kkagent"
        execute.assert_not_awaited()
    finally:
        api._agent_sessions.clear()
        api._agent_sessions.update(saved)


@pytest.mark.asyncio
async def test_confirmed_model_tool_plan_submits_only_once(model, monkeypatch):
    from features.assistant import api
    from features.assistant.tests.test_agent_session_safety import _bare_request, _seed_confirm_session

    _, execute = model
    entered, release = asyncio.Event(), asyncio.Event()

    async def perform(*args, **kwargs):
        entered.set()
        await release.wait()
        return ToolResult(success=True, tool_name="devices_reboot", formatted_text="已提交")

    execute.side_effect = perform
    saved = dict(api._agent_sessions)
    try:
        current = _seed_confirm_session(api, {"type": "generic_action", "intent": {
            "tool_name": "devices_reboot", "params": {"devices": ["DEV-1"]},
        }})
        monkeypatch.setattr(api, "cleanup_due", lambda: False)
        body = api.AgentChatRequest(session_id="s-confirm", message="确认执行", execute=True)
        first = asyncio.create_task(api.agent_chat(_bare_request(), body))
        await entered.wait()
        await api.agent_chat(_bare_request(), body)
        release.set()
        await first
        assert execute.await_count == 1
        assert current["pending_plan"] is None
    finally:
        release.set()
        api._agent_sessions.clear()
        api._agent_sessions.update(saved)


@pytest.mark.asyncio
async def test_background_model_failure_reports_error_without_rule_replay(model, monkeypatch):
    from features.assistant import api
    from features.assistant.tests.test_agent_session_safety import _bare_request

    turn, _ = model
    turn.return_value = BridgeTurnResult(ok=False, error="模型不可用")
    saved = dict(api._agent_sessions)
    monkeypatch.setattr(api, "cleanup_due", lambda: False)
    try:
        response = await api.agent_chat(_bare_request(), api.AgentChatRequest(message="你好"))
        payload = json.loads(response.body)["data"]["session"]
        sid = payload["session_id"]
        assert payload["status"] == "thinking"
        await asyncio.wait_for(agent._project_tasks[sid], 5)
        current = api._agent_sessions[sid]
        assert len(api._agent_sessions) == len(saved) + 1
        assert len([item for item in current["messages"] if item["role"] == "user"]) == 1
        assert current["agent_backend"]["mode"] == "kkagent"
        assert current["agent_backend"]["state"] == "failed"
        assert current["messages"][-1]["data"]["quick_actions"][0]["action"] == "agent_retry"
        assert "还缺少必要参数" not in current["messages"][-1]["content"]
        assert current["status"] != "thinking"
        assert turn.await_count == 1
    finally:
        api._agent_sessions.clear()
        api._agent_sessions.update(saved)


@pytest.mark.asyncio
async def test_cancel_stops_a_background_model_turn(model):
    from features.assistant import api
    from features.assistant.tests.test_agent_session_safety import _bare_request

    turn, execute = model
    entered = asyncio.Event()

    async def wait(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    turn.side_effect = wait
    current = session()
    assert await agent.start_project_turn(current, _bare_request(), api.AgentChatRequest(message="查询设备"))
    task = agent._project_tasks[current["session_id"]]
    await entered.wait()
    current["status"] = "cancelled"
    agent.cancel_project_turn(current["session_id"])
    with pytest.raises(asyncio.CancelledError):
        await task
    assert current["agent_thinking"] is False
    assert current["session_id"] not in agent._project_tasks
    execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_followup_question_preserves_the_pending_confirmation(model):
    from features.assistant import api
    from features.assistant.tests.test_agent_session_safety import _bare_request

    turn, _ = model
    turn.return_value = decision("answer", text="当前计划会重启指定设备")
    current = session()
    current["pending_plan"] = {"type": "generic_action", "intent": {"tool_name": "devices_reboot"}}
    current["status"] = "planning"
    assert await agent.start_project_turn(current, _bare_request(), api.AgentChatRequest(message="解释一下当前计划"))
    await agent._project_tasks[current["session_id"]]
    assert current["status"] == "planning"
    assert current["pending_plan"]["type"] == "generic_action"


@pytest.mark.asyncio
async def test_flash_resources_are_model_queries_followed_by_a_generic_question(model, monkeypatch):
    turn, execute = model
    fields = [{"name": name, "label": label, "options": [{"value": value, "label": value}]}
              for name, label, value in [("device", "设备", "DEV-1"), ("firmware", "固件", "share-1"), ("aoa", "AOA", "profile-1")]]
    turn.side_effect = [decision("call", tool=name, params={}) for name in ("devices_list", "firmware_shares", "aoa_profiles")]
    turn.side_effect = [*turn.side_effect, decision("question", text="请选择烧写资源", fields=fields)]
    current = session()
    assert await agent.handle_project_turn(current, object(), request("烧写固件"))
    assert [call.args[2] for call in execute.await_args_list] == ["devices_list", "firmware_shares", "aoa_profiles"]
    assert [field["label"] for field in current["agent_question"]["fields"]] == ["设备", "固件", "AOA"]
    assert current.get("pending_plan") is None
    question_id = current["agent_question"]["id"]
    turn.side_effect = [decision("workflow", entities={"device_serials": ["DEV-1"], "firmware_share_id": "share-1",
                                                      "aoa_profile": "profile-1", "flash_only": True})]
    monkeypatch.setattr("features.assistant.workflow_execution.refresh_workflow_preflight",
                        lambda *args, **kwargs: {"ready": True, "checks": [], "blocks": []})
    assert await agent.handle_project_turn(current, object(), request("提交资源选择", action="agent_choices", params={
        "question_id": question_id, "values": {"device": "DEV-1", "firmware": "share-1", "aoa": "profile-1"},
    }))
    assert current["pending_plan"]["type"] == "workflow_draft"
    assert current["pending_plan"]["draft"]["device"]["serial"] == "DEV-1"
    assert execute.await_count == 3, "planning must never flash or run AOA"


@pytest.mark.asyncio
async def test_non_flash_command_uses_the_same_resource_question(model):
    turn, execute = model
    turn.side_effect = [decision("call", tool="reports_list", params={}), decision("question", text="选择报告", fields=[{
        "name": "report", "label": "报告", "options": [{"value": "report-1", "label": "昨天的报告"}],
    }])]
    current = session()
    assert await agent.handle_project_turn(current, object(), request("分析昨天跑失败的报告"))
    assert current["agent_question"]["fields"][0]["name"] == "report"
    assert execute.await_args.args[2] == "reports_list"


@pytest.mark.asyncio
async def test_model_can_navigate_to_a_registered_project_page(model):
    turn, execute = model
    turn.return_value = decision("navigate", page="reports", text="打开报告管理")
    current = session()
    assert await agent.handle_project_turn(current, object(), request("打开报告管理"))
    assert current["messages"][-1]["data"] == {"page": "reports", "auto_open": True}
    execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_model_cannot_execute_a_mutating_tool_without_confirmation(model):
    turn, execute = model
    turn.side_effect = [decision("call", tool="devices_reboot", params={"devices": ["DEV-1"]})]
    current = session()
    assert await agent.handle_project_turn(current, object(), request("重启 DEV-1"))
    execute.assert_not_awaited()
    assert current["pending_plan"]["type"] == "generic_action"
    assert current["status"] == "planning"


@pytest.mark.asyncio
async def test_routine_tool_keeps_its_existing_no_confirmation_policy(model, monkeypatch):
    turn, execute = model
    tool = replace(agent.registry.get("devices_list"), name="routine_update", is_readonly=False,
                   requires_confirm=False, is_dangerous=False, description="更新常规设置")
    monkeypatch.setitem(agent.registry._tools, tool.name, tool)
    turn.side_effect = [decision("call", tool=tool.name, params={}), decision("answer", text="操作完成")]
    current = session()
    assert await agent.handle_project_turn(current, object(), request("更新设置"))
    execute.assert_awaited_once()
    assert current.get("pending_plan") is None


def test_file_and_interactive_tools_are_described_as_unsupported_for_direct_calls():
    assert agent._schema(agent.registry.get("burn_firmware"))["supported"] is False


@pytest.mark.asyncio
async def test_model_cannot_replace_an_explicit_workflow_device(model, monkeypatch):
    turn, execute = model
    turn.side_effect = [decision("workflow", entities={"device_serials": [serial], "firmware_share_id": "share-1", "flash_only": True})
                        for serial in ("OTHER-DEVICE", "66df75a34ebde4a4")]
    monkeypatch.setattr("features.assistant.workflow_execution.refresh_workflow_preflight",
                        lambda *args, **kwargs: {"ready": True, "checks": [], "blocks": []})
    current = session()
    assert await agent.handle_project_turn(current, object(), request("设备66df75a34ebde4a4 烧写固件"))
    assert current["pending_plan"]["draft"]["device"]["serial"] == "66df75a34ebde4a4"
    assert "不得自动替换" in turn.call_args.kwargs["context"]
    execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_invalid_choice_is_rejected_before_another_model_call(model):
    turn, execute = model
    current = session()
    current["agent_question"] = {"id": "question-1", "fields": [{"name": "device", "label": "设备", "required": True,
                                                            "options": [{"value": "allowed"}]}]}
    assert await agent.handle_project_turn(current, object(), request(action="agent_choices", params={
        "question_id": "question-1", "values": {"device": "different"},
    }))
    turn.assert_not_awaited()
    execute.assert_not_awaited()
    assert "不在当前选项" in current["messages"][-1]["content"]


@pytest.mark.asyncio
async def test_model_connection_failure_stays_in_the_model_backend(model):
    turn, execute = model
    turn.return_value = BridgeTurnResult(ok=False, error="模型连接失败")
    current = session()
    assert await agent.handle_project_turn(current, object(), request()) is True
    assert current["agent_backend"]["mode"] == "kkagent"
    assert current["agent_backend"]["state"] == "failed"
    assert current["agent_thinking"] is False
    execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_disabled_model_reports_the_actual_rule_backend(model, monkeypatch):
    from features.assistant import api
    from features.assistant.tests.test_agent_session_safety import _bare_request

    turn, execute = model
    monkeypatch.setenv("GMS_ASSISTANT_KKAGENT_ENABLED", "0")
    current = session()
    assert await agent.start_project_turn(current, _bare_request(), api.AgentChatRequest(message="设备列表")) is False
    assert current["agent_backend"]["mode"] == "rules"
    assert current["agent_backend"]["state"] == "disabled"
    turn.assert_not_awaited()
    execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancel_during_model_call_stops_tool_execution(model):
    turn, execute = model
    current = session()

    async def cancel(*args, **kwargs):
        current["status"] = "cancelled"
        return decision("call", tool="devices_list", params={})

    turn.side_effect = cancel
    assert await agent.handle_project_turn(current, object(), request())
    execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_deadline_cancels_a_stuck_call():
    with pytest.raises(asyncio.TimeoutError):
        async with agent._TurnDeadline(0.01):
            await asyncio.sleep(1)


@pytest.mark.parametrize("field", ["password", "api_token", "private_key"])
def test_resource_questions_cannot_request_credentials(field):
    with pytest.raises(ValueError):
        agent._question_fields([{"name": field}])


@pytest.mark.asyncio
async def test_first_inference_timeout_retries_with_the_new_native_session(model):
    turn, execute = model
    turn.side_effect = [BridgeTurnResult(ok=False, session_id="first-session", error="kkagent 回合超时", error_code="timeout"),
                        decision("question", text="请选择设备", fields=[{"name": "device", "label": "设备"}])]
    current = session()
    assert await agent.handle_project_turn(current, object(), request("烧写固件"))
    assert turn.await_count == 2
    assert turn.await_args_list[1].kwargs["session_id"] == "first-session"
    assert all(call.kwargs["timeout_seconds"] == 120 for call in turn.await_args_list)
    assert current["messages"][-1]["kind"] == "question"
    assert current["agent_backend"]["state"] == "ready"
    assert not current.get("agent_retry")
    execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_repeated_timeout_offers_explicit_retry_instead_of_missing_parameters(model):
    turn, execute = model
    turn.return_value = BridgeTurnResult(ok=False, error="kkagent 回合超时", error_code="timeout")
    current = session()
    assert await agent.handle_project_turn(current, object(), request("烧写固件"))
    assert turn.await_count == 2
    assert current["agent_backend"]["state"] == "timeout"
    assert current["steps"][-1]["status"] == "warning"
    assert "缺少" not in current["messages"][-1]["content"]
    assert current["messages"][-1]["data"]["quick_actions"][0]["params"]["retry_id"] == current["agent_retry"]["id"]
    execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_manual_retry_keeps_observations_without_repeating_a_completed_write(model, monkeypatch):
    turn, execute = model
    tool = replace(agent.registry.get("devices_list"), name="routine_update", is_readonly=False,
                   requires_confirm=False, is_dangerous=False)
    monkeypatch.setitem(agent.registry._tools, tool.name, tool)
    turn.side_effect = [decision("call", tool=tool.name, params={}), decision("call", tool="devices_list", params={}),
                        BridgeTurnResult(ok=False, error="模型连接失败", error_code="process_error"),
                        decision("call", tool=tool.name, params={}), decision("answer", text="继续完成")]
    current = session()
    await agent.handle_project_turn(current, object(), request("更新设置并查询设备"))
    assert execute.await_count == 2
    retry_id = current["agent_retry"]["id"]
    assert "completed" not in json.dumps(current)
    await agent.handle_project_turn(current, object(), request(action="agent_retry", params={"retry_id": retry_id}))
    assert execute.await_count == 2
    assert turn.await_args_list[3].args[0] == "更新设置并查询设备"
    assert "DEVICE-1" in turn.await_args_list[3].kwargs["context"]
    assert current["messages"][-1]["content"] == "继续完成"
    assert not current.get("agent_retry")


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatch", ["retry_id", "owner", "workspace", "expired", "cancelled"])
async def test_retry_checkpoint_is_session_bound_and_expires(model, monkeypatch, mismatch):
    turn, execute = model
    turn.return_value = BridgeTurnResult(ok=False, error="模型连接失败")
    current = session()
    await agent.handle_project_turn(current, object(), request())
    retry_id = current["agent_retry"]["id"]
    if mismatch == "retry_id":
        retry_id = "stale"
    elif mismatch == "owner":
        current["owner_id"] = "another-user"
    elif mismatch == "workspace":
        current["workspace_context"]["worker_id"] = "other-worker"
    elif mismatch == "expired":
        now = agent.recovery.time.monotonic()
        monkeypatch.setattr(agent.recovery.time, "monotonic", lambda: now + 1000)
    else:
        agent.cancel_project_turn(current["session_id"])
    await agent.handle_project_turn(current, object(), request(action="agent_retry", params={"retry_id": retry_id}))
    assert turn.await_count == 1
    assert "重试已过期" in current["messages"][-1]["content"]
    execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_planning_deadline_reports_timeout_and_retains_progress(model, monkeypatch):
    turn, execute = model
    monkeypatch.setattr(agent.recovery, "TURN_TIMEOUT_SECONDS", 0.01)

    async def wait(*args, **kwargs):
        await asyncio.Event().wait()

    turn.side_effect = wait
    current = session()
    assert await agent.handle_project_turn(current, object(), request("烧写固件"))
    assert current["agent_backend"]["state"] == "timeout"
    assert current["agent_thinking"] is False
    assert current["agent_retry"]
    execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_guard_pauses_for_human_elevation_then_retries_the_exact_query(model, monkeypatch):
    from fastapi import HTTPException

    from features.assistant.executor import ActionExecutor
    from features.assistant.tests.test_agent_session_safety import _bare_request
    from features.firmware import shares_api

    turn, _ = model
    # Exercise the real executor dependency graph, rather than model-supplied permission flags.
    execute = ActionExecutor()
    monkeypatch.setattr(agent, "executor", execute)
    monkeypatch.setattr("features.assistant.executor.require_authenticated_user", lambda req: SimpleNamespace(role="user"))
    monkeypatch.setattr("features.auth.access.authentication_required", lambda: True)

    def gate(req):
        if not getattr(req.state, "is_elevated", False):
            raise HTTPException(403, {"message": "Elevation required", "elevation_required": True})

    monkeypatch.setattr("features.auth.access.require_elevated_admin", gate)
    read = Mock(return_value=[{"id": "share-1", "name": "Firmware", "filename": "update.img"}])
    monkeypatch.setattr(shares_api, "_load_records", read)
    turn.side_effect = [decision("call", tool="firmware_shares", params={}), decision("question", text="请选择固件", fields=[{
        "name": "firmware", "label": "固件", "options": [{"value": "share-1", "label": "Firmware"}],
    }])]
    current = session()
    req = _bare_request()
    await agent.handle_project_turn(current, req, request("烧写固件"))
    assert current["status"] == "awaiting_authorization"
    assert current["agent_backend"]["state"] == "awaiting_authorization"
    assert current["messages"][-1]["data"]["quick_actions"][0]["action"] == "agent_elevate_retry"
    assert turn.await_count == 1
    read.assert_not_called()
    retry_id = current["agent_retry"]["id"]
    # Sending retry without real elevation cannot bypass the same guard.
    await agent.handle_project_turn(current, req, request(action="agent_retry", params={"retry_id": retry_id}))
    assert turn.await_count == 1
    read.assert_not_called()
    retry_id = current["agent_retry"]["id"]
    req.state.is_elevated = True
    await agent.handle_project_turn(current, req, request(action="agent_retry", params={"retry_id": retry_id}))
    read.assert_called_once()
    assert turn.await_count == 2
    assert "share-1" in turn.await_args.kwargs["context"]
    assert current["agent_question"]["fields"][0]["options"][0]["value"] == "share-1"
    assert not current.get("agent_retry")
