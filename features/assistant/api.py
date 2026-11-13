"""Conversation Agent router for guided GMS Remote Test workflows."""

import asyncio
import logging
import re
import uuid
from typing import Any

from fastapi import APIRouter, Body, Request
from pydantic import BaseModel, Field

from features.assistant.capabilities import capabilities_payload
from features.assistant.context import record_user_message, update_context
from features.assistant.executor import executor
from features.assistant.payloads import jsonable as _jsonable, session_payload as _session_payload
from features.assistant.plan_submission import submit_pending_plan_once
from features.assistant.response import (
    WEBAPP_PAGES,
    generate_capability_overview,
    generate_clarification,
    generate_page_overview,
    page_quick_actions,
)
from features.assistant.response import (
    generate as gen_response,
)
from features.assistant.session_directory import (
    _restore_session_from_transcript as _restore_session_from_transcript,
)
from features.assistant.session_directory import (
    _session_directory,
    delete_session,
)
from features.assistant.session_directory import (
    _session_directory_entry as _session_directory_entry,
)
from features.assistant.session_log import (
    _append_message,
    _append_step,
    _format_params,
)
from features.assistant.session_registry import (
    AgentRequestShim,
    _agent_monitor_tasks,
    _agent_sessions,
    cleanup_due,
)
from features.assistant.session_registry import (
    cleanup_sessions as _cleanup_sessions,
)
from features.assistant.session_registry import (
    expired_session_payload as _expired_session_payload,
)
from features.assistant.session_registry import (
    finish_run_state as _finish_run_state,
)
from features.assistant.session_registry import (
    get_or_create_session as _get_or_create_session,
)
from features.assistant.session_registry import (
    restore_session as _restore_session,
)
from features.assistant.test_runs import (
    _execute_plan,
    _monitor_agent_run,
)
from features.assistant.tools import registry
from features.assistant.workflow_draft import (
    build_workflow_draft,
    detect_composite_intent,
    draft_blocks,
    extract_workflow_entities,
    summarize_draft,
)
from features.assistant.workflow_execution import (
    WORKFLOW_PLAN_TYPE as _WORKFLOW_PLAN_TYPE,
)
from features.assistant.workflow_execution import (
    execute_workflow_plan as _execute_workflow_plan,
)
from features.assistant.workflow_execution import (
    handle_workflow_plan_message as _handle_workflow_plan_message,
)
from features.assistant.workflow_execution import (
    is_workflow_confirm_message as _is_workflow_confirm_message,
)
from features.assistant.workflow_execution import (
    refresh_workflow_preflight as _refresh_workflow_preflight,
)
from features.assistant.workflow_execution import (
    wants_message_execution as _wants_message_execution,
)
from features.assistant.workspace import (
    _apply_workspace_tool_params,
    _extract_device_count,
    _missing_required_params,
    _normalize_workspace_context,
    _request_missing_params,
)
from features.auth import require_human_principal_when_auth_required
from features.users import get_client_id_from_request, owner_id_from_request
from foundation.responses import error_response, success_response


logger = logging.getLogger(__name__)
router = APIRouter()


class AgentChatRequest(BaseModel):
    """Request body for Agent chat messages."""

    message: str = Field(default="", max_length=4000)
    session_id: str | None = None
    execute: bool = False
    action: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    workspace_context: dict[str, Any] = Field(default_factory=dict)


@router.post("/api/agent/chat")
async def agent_chat(request: Request, req: AgentChatRequest = Body(...)):
    """Chat with the local GMS workflow Agent."""
    if cleanup_due():
        await _cleanup_sessions()
    message = (req.message or "").strip()
    if not message:
        return error_response("Message cannot be empty", 400)

    client_id = get_client_id_from_request(request)
    session = await _get_or_create_session(req.session_id, client_id)
    session["owner_id"] = owner_id_from_request(request)
    if req.workspace_context:
        supplied_workspace = _normalize_workspace_context(req.workspace_context)
        session["workspace_context"] = {
            **(session.get("workspace_context") or {}),
            **supplied_workspace,
        }
    workspace = session.get("workspace_context") or _normalize_workspace_context({})
    _append_message(session, "user", message)
    record_user_message(session, message)

    # --- 0. WorkflowDraft 待确认计划：显式 plan 操作（重新规划/取消/状态）---
    workflow_pending = session.get("pending_plan")
    workflow_action_map = {
        "workflow_replan": "重新规划",
        "workflow_cancel": "取消计划",
        "workflow_status": "计划状态",
    }
    if (
        workflow_pending
        and workflow_pending.get("type") == _WORKFLOW_PLAN_TYPE
        and not _wants_message_execution(req, message)
    ):
        if req.action in workflow_action_map:
            await _handle_workflow_plan_message(
                session, workflow_action_map[req.action], workflow_pending, request
            )
            return success_response({"session": _session_payload(session)}, "Agent updated")
        # 确认执行类消息直接放行到第 1 段确认链；其余先尝试计划修改。
        if not _is_workflow_confirm_message(message):
            handled = await _handle_workflow_plan_message(session, message, workflow_pending, request)
            if handled:
                return success_response({"session": _session_payload(session)}, "Agent updated")

    # --- 1. Check pending plan confirmation ---
    pending_plan = session.get("pending_plan")
    wants_execute = req.execute or _is_workflow_confirm_message(message)
    if wants_execute and (session.get("agent_thinking") or session.get("status") == "thinking"):
        _append_message(session, "assistant", "Agent 正在规划，请等待当前计划生成后再确认执行。")
        return success_response({"session": _session_payload(session)}, "Agent updated")
    if pending_plan and wants_execute:
        if pending_plan.get("type") == "generic_action":
            intent_data = pending_plan.get("intent")

            if isinstance(intent_data, dict):
                tool_name = intent_data.get("tool_name", "")
                params = _apply_workspace_tool_params(
                    tool_name, intent_data.get("params", {}), workspace
                )
                category = intent_data.get("category", "")
            else:
                tool_name = getattr(intent_data, "tool_name", "")
                params = _apply_workspace_tool_params(
                    tool_name, getattr(intent_data, "params", {}), workspace
                )
                category = ""
            tool = registry.get(tool_name)
            missing = _missing_required_params(tool, params)
            if missing:
                _request_missing_params(session, missing)
                return success_response({"session": _session_payload(session)}, "Agent updated")
            from features.assistant.project_agent import execute_confirmed_tool

            submitted, repeat_hint = await submit_pending_plan_once(
                pending_plan, lambda _plan: execute_confirmed_tool(session, request, tool_name, params))
            if submitted is None:
                _append_message(session, "assistant", repeat_hint)
                return success_response({"session": _session_payload(session)}, "Agent updated")
            tool_result = submitted["outcome"]

            update_context(
                session, tool_name=tool_result.tool_name,
                category=category or (tool.category if tool else ""),
                entities=tool_result.entities,
                result_summary=tool_result.formatted_text[:200] if tool_result.formatted_text else "",
            )

            resp = gen_response(tool_result)
            _append_message(session, "assistant", resp.content, kind=resp.kind, data=resp.to_message_data(), private_params=params)
            session["pending_plan"] = None
            session["status"] = "monitoring" if (session.get("active_run") or {}).get("redmine_run_id") else "idle"
        elif pending_plan.get("type") == "test_execution":
            # 确认提交状态机在 plan_submission：同一计划只提交一次。
            result, repeat_hint = await submit_pending_plan_once(
                pending_plan,
                lambda _plan: _execute_plan(session, request, pending_plan))
            if result is None:
                _append_message(session, "assistant", repeat_hint)
            elif not result.get("success"):
                _append_message(session, "assistant", f"执行失败：{result.get('error') or result.get('message')}")
        elif pending_plan.get("type") == _WORKFLOW_PLAN_TYPE:
            # 阻塞未解除时禁用执行，明确告知原因；不再出现“不能执行”
            # 与“确认后启动”并存。确认时重刷新预检以带上创建者权限状态
            # （提权可能发生在计划生成之后）。
            if not pending_plan.get("submit_state"):
                pending_plan["preflight"] = _refresh_workflow_preflight(
                    session, pending_plan.get("draft") or {}, request=request
                )
            preflight = pending_plan.get("preflight") or {}
            if not preflight.get("ready"):
                blocks = preflight.get("blocks") or draft_blocks(pending_plan.get("draft") or {})
                reasons = "\n".join(f"- {item.get('reason') or item.get('stage')}" for item in blocks) or "- 计划预检未通过"
                _append_message(
                    session, "assistant",
                    "当前工作流存在阻塞项，暂不能执行：\n" + reasons +
                    "\n解决后输入“重新规划”刷新预检。",
                    kind="plan",
                )
            else:
                result, repeat_hint = await submit_pending_plan_once(
                    pending_plan,
                    lambda _plan: _execute_workflow_plan(
                        session, request, pending_plan.get("draft") or {}))
                if result is None:
                    _append_message(session, "assistant", repeat_hint)
                elif not result.get("success"):
                    _append_message(session, "assistant", f"执行失败：{result.get('error') or result.get('message')}")
        else:
            session["pending_plan"] = None
            session["status"] = "idle"
            _append_message(session, "assistant", "待执行计划格式已失效，请重新发起操作。")
        return success_response({"session": _session_payload(session)}, "Agent updated")

    # Model-led discovery covers the project tool catalog; rules remain a fallback.
    if not req.action or req.action in {"agent_choices", "agent_retry"}:
        from features.assistant.project_agent import start_project_turn

        if await start_project_turn(session, request, req):
            return success_response({"session": _session_payload(session)}, "Agent updated")

    # --- 2. Resolve intent through multi-stage router ---
    from features.assistant.intent import resolve as resolve_intent

    # --- 1b. 复合工作流：烧写/AOA/测试 ≥2 类意图 → WorkflowDraft ---
    # 一句话生成完整多阶段计划；执行仍走 automation 状态机。
    if detect_composite_intent(message):
        entities = extract_workflow_entities(message)
        test_requested = bool(
            entities.get("test_type")
            or entities.get("test_module")
            or re.search(r"测试|test|跑", message, re.IGNORECASE)
        )
        if not test_requested:
            entities["flash_only"] = True
        # 约束冻结与 kkagent 路径（project_agent._make_workflow）同源同义：
        # 本轮显式给出 > 上一轮冻结值；作用域绑定未提交计划（取消/提交
        # 执行时由 workflow_execution 清空）。两条后端建计划路径必须保持
        # 一致的生命周期，否则回退路径会静默丢失/复活约束。
        from features.assistant.workflow_draft import (
            constraint_conflicts,
            freeze_user_constraints,
        )
        constraints = freeze_user_constraints(
            entities, session.get("workflow_user_constraints")
        )
        session["workflow_user_constraints"] = constraints
        conflicts = constraint_conflicts(entities, constraints)
        if conflicts:
            session["status"] = "idle"
            _append_message(
                session, "assistant",
                "与用户已明确的要求冲突：" + "；".join(conflicts),
            )
            return success_response({"session": _session_payload(session)}, "Agent updated")
        draft = build_workflow_draft(entities, workspace)
        draft["user_constraints"] = constraints
        draft["operation_id"] = str(uuid.uuid4())
        preflight = _refresh_workflow_preflight(session, draft, request=request)
        session["pending_plan"] = {
            "type": _WORKFLOW_PLAN_TYPE,
            "draft": draft,
            "preflight": preflight,
            "intent": {"tool_name": "workflow_draft", "params": entities, "category": "workflow"},
        }
        session["status"] = "planning"
        _append_step(
            session, "生成工作流计划", "done",
            "已解析为 烧写 → AOA → 测试 多阶段工作流",
            {"plan": draft, "preflight": preflight},
        )
        _append_message(
            session, "assistant", summarize_draft(draft, preflight),
            kind="plan",
            data={"plan": {"type": _WORKFLOW_PLAN_TYPE, "draft": draft, "preflight": preflight}},
        )
        return success_response({"session": _session_payload(session)}, "Agent updated")

    if req.action:
        tool = registry.get(req.action)
        if not tool:
            _append_message(session, "assistant", f"未知操作：{req.action}")
            session["status"] = "idle"
            return success_response({"session": _session_payload(session)}, "Agent updated")

        params = _apply_workspace_tool_params(
            tool.name, _jsonable(req.params or {}), workspace
        )
        missing = _missing_required_params(tool, params)
        if missing:
            _request_missing_params(session, missing)
            return success_response({"session": _session_payload(session)}, "Agent updated")
        if tool.requires_confirm and not req.execute:
            session["pending_plan"] = {
                "intent": {"tool_name": tool.name, "params": params, "category": tool.category},
                "type": "generic_action",
            }
            session["status"] = "planning"
            _append_message(
                session,
                "assistant",
                "此操作需要确认：**" + tool.display_name + "**\n\n"
                "参数：" + _format_params(params) + "\n\n"
                "输入\"确认执行\"或点击执行按钮后启动。",
                kind="plan",
                data={"plan": {"tool_name": tool.name, "params": params, "type": "generic_action"}},
            )
            return success_response({"session": _session_payload(session)}, "Agent updated")

        tool_result = await executor.execute(session, request, tool.name, params)
        update_context(
            session,
            tool_name=tool_result.tool_name,
            category=tool.category,
            entities=tool_result.entities,
            result_summary=tool_result.formatted_text[:200] if tool_result.formatted_text else "",
        )
        resp = gen_response(tool_result)
        _append_message(session, "assistant", resp.content, kind=resp.kind, data=resp.to_message_data(), private_params=params)
        session["status"] = "monitoring" if (session.get("active_run") or {}).get("redmine_run_id") else "idle"
        session["pending_plan"] = None
        return success_response({"session": _session_payload(session)}, "Agent updated")

    intent = resolve_intent(message, session)

    # --- 2a. Low confidence: ask for clarification ---
    if not intent.tool_name or intent.confidence < 0.3:
        scored = registry.find(message, top_k=3, min_score=0.5)
        if scored:
            suggestions = [
                {"label": s.tool.display_name, "action": s.tool.name, "description": s.tool.description}
                for s in scored[:3]
            ]
            resp = generate_clarification(suggestions)
            _append_message(session, "assistant", resp.content, kind=resp.kind, data=resp.to_message_data())
        else:
            _append_message(session, "assistant", generate_capability_overview(), data={"page": "agent"})
        # 未识别消息不再清空待确认的工作流计划：草案保留在会话中，
        # 用户仍可“重新规划/取消计划”。
        if session.get("pending_plan", {}).get("type") == _WORKFLOW_PLAN_TYPE:
            session["status"] = "planning"
        else:
            session["status"] = "idle"
            session["pending_plan"] = None
        return success_response({"session": _session_payload(session)}, "Agent updated")

    # --- 2b. Capabilities: special handling ---
    if intent.tool_name == "agent_capabilities":
        text = generate_capability_overview()
        _append_message(session, "assistant", text, data={"page": "agent"})
        session["status"] = "idle"
        session["pending_plan"] = None
        return success_response({"session": _session_payload(session)}, "Agent updated")

    if intent.tool_name == "page_overview":
        _append_message(
            session,
            "assistant",
            generate_page_overview(),
            data={
                "page": "agent",
                "quick_actions": page_quick_actions(),
            },
        )
        session["status"] = "idle"
        session["pending_plan"] = None
        return success_response({"session": _session_payload(session)}, "Agent updated")

    if intent.tool_name == "greeting":
        _append_message(session, "assistant", "你好，我是 GMS 远程测试 Agent。可以查设备/套件/报告/状态，也能生成测试计划、做 retry 和失败分析；问「每个页面功能」可以看完整页面说明。", data={"page": "agent"})
        session["status"] = "idle"
        session["pending_plan"] = None
        return success_response({"session": _session_payload(session)}, "Agent updated")

    # --- 2c. Navigate: special handling ---
    if intent.tool_name == "navigate":
        page = intent.params.get("page", "")
        if page:
            name, _desc = WEBAPP_PAGES.get(page, (page, ""))
            _append_message(session, "assistant", f"已打开「{name}」页面。", data={"page": page, "auto_open": True})
        else:
            _append_message(session, "assistant", "没有识别到要打开的页面。", data={"page": "agent"})
        session["status"] = "idle"
        session["pending_plan"] = None
        return success_response({"session": _session_payload(session)}, "Agent updated")

    # --- 2d. Run test: generate an explicit test-execution plan ---
    if intent.is_run_test:
        # 计划资源选择/构建已拆至 plan_context.py（file-size baseline）；
        # 仅此一处使用，函数内 import 避免顶部兼容别名。
        from features.assistant.plan_context import (
            _select_devices,
            _select_suite,
        )
        from features.assistant.plan_context import (
            build_test_plan as _build_plan,
        )
        from features.assistant.plan_context import (
            summarize_test_plan as _summarize_plan,
        )

        test_intent = dict(intent.params)
        test_intent["device_count"] = (len(test_intent.get("devices") or []) or _extract_device_count(message))
        selected_devices, device_details = _select_devices(test_intent, workspace)
        suite = _select_suite(test_intent, workspace)
        plan = _build_plan(
            test_intent, selected_devices, device_details, suite, workspace
        )
        session["pending_plan"] = plan
        session["status"] = "planning"
        _append_step(session, "生成测试计划", "done", "已根据自然语言匹配设备和测试套件", {"plan": plan})
        _append_message(session, "assistant", _summarize_plan(plan), kind="plan", data={"plan": plan})
        return success_response({"session": _session_payload(session)}, "Agent updated")

    tool_params = _apply_workspace_tool_params(
        intent.tool_name, intent.params, workspace
    )
    missing = _missing_required_params(intent.tool, tool_params)
    if missing:
        _request_missing_params(session, missing)
        return success_response({"session": _session_payload(session)}, "Agent updated")

    # --- 2e. Dangerous/confirm-required: show confirmation plan ---
    if intent.needs_confirm and not req.execute:
        tool = intent.tool
        tool_display = tool.display_name if tool else intent.tool_name
        plan_text = (
            "⚠️ 此操作需要确认：**" + tool_display + "**\n\n"
            "参数：" + _format_params(tool_params) + "\n\n"
            "输入\"确认执行\"或点击执行按钮后启动。"
        )
        session["pending_plan"] = {
            "intent": {
                "tool_name": intent.tool_name,
                "params": _jsonable(tool_params),
                "category": tool.category if tool else "",
            },
            "type": "generic_action",
        }
        session["status"] = "planning"
        _append_message(session, "assistant", plan_text, kind="plan",
                        data={"plan": {"tool_name": intent.tool_name, "params": tool_params, "type": "generic_action"}})
        return success_response({"session": _session_payload(session)}, "Agent updated")

    # --- 3. Execute tool ---
    tool_result = await executor.execute(session, request, intent.tool_name, tool_params)

    # --- 4. Update context ---
    update_context(
        session,
        tool_name=tool_result.tool_name,
        category=intent.tool.category if intent.tool else "",
        entities=tool_result.entities,
        result_count=len(tool_result.entities.get(next(iter(tool_result.entities.keys())), [])) if tool_result.entities else 0,
        result_summary=tool_result.formatted_text[:200] if tool_result.formatted_text else "",
    )

    # --- 5. Generate and send response ---
    resp = gen_response(tool_result)
    _append_message(session, "assistant", resp.content, kind=resp.kind, data=resp.to_message_data(), private_params=tool_params)

    session["status"] = "monitoring" if (session.get("active_run") or {}).get("redmine_run_id") else "idle"
    session["pending_plan"] = None
    return success_response({"session": _session_payload(session)}, "Agent updated")


def kkagent_bridge_availability() -> dict[str, Any]:
    """kkagent 桥接可用性（惰性导入，避免影响基础路由加载）。"""
    from features.assistant.kkagent_bridge import availability

    try:
        return availability()
    except Exception:
        return {"available": False, "binary": "", "mode": "headless_stream_json"}


@router.get("/api/agent/sessions")
async def list_agent_sessions(request: Request):
    """List this client's Agent sessions (live + recoverable anchors)."""
    client_id = get_client_id_from_request(request)
    owner_id = owner_id_from_request(request)
    return success_response({"sessions": _session_directory(client_id, owner_id)})


@router.delete("/api/agent/sessions/{session_id}")
async def delete_agent_session(session_id: str, request: Request):
    """Discard one Agent session: drop live state and its recovery anchor."""
    require_human_principal_when_auth_required(request)
    return delete_session(session_id, get_client_id_from_request(request), owner_id_from_request(request))


@router.get("/api/agent/sessions/{session_id}")
async def get_agent_session(session_id: str, request: Request):
    """Get Agent session status."""
    client_id = get_client_id_from_request(request)
    owner_id = owner_id_from_request(request)
    session = _agent_sessions.get(session_id)
    if not session or session.get("client_id") != client_id:
        # Controller 重启后内存会话丢失；优先从落盘对话记录恢复消息正文，
        # 活跃运行再叠加持久锚点续接监控协程直到任务终态。
        session = _restore_session(session_id, client_id, owner_id)
        if session and session_id not in _agent_monitor_tasks:
            request_shim = AgentRequestShim(request)
            redmine_active = bool((session.get("active_run") or {}).get("redmine_run_id"))
            _append_step(session, "会话已恢复", "done" if redmine_active else "running", "Controller 重启后根据持久任务重建监控状态")
            if redmine_active:
                from features.assistant.redmine_analysis import monitor_redmine_run

                task = asyncio.create_task(monitor_redmine_run(session_id, request_shim, session['active_run'].get('monitor_token')))
            elif (session.get("active_run") or {}).get("automation_run_id"):
                # 工作流 run 只在锚点保留 automation_run_id，监控续接走
                # workflow 监视器（token=None 与恢复锚点 active_run 匹配）。
                from features.assistant.workflow_execution import monitor_workflow_run

                task = asyncio.create_task(monitor_workflow_run(session_id, request_shim, session['active_run'].get('monitor_token')))
            else:
                task = asyncio.create_task(_monitor_agent_run(session_id, request_shim, session['active_run'].get('monitor_token')))
            _agent_monitor_tasks[session_id] = task
        if not session:
            # 无活跃运行的重启恢复：从落盘记录重建只读对话历史。
            session = _restore_session_from_transcript(session_id, client_id, owner_id)
    if not session or session.get("client_id") != client_id:
        return success_response(_expired_session_payload(session_id), "Agent session expired")
    active = session.get("active_run") or {}
    task = _agent_monitor_tasks.get(session_id)
    if active.get("redmine_run_id") and (task is None or task.done()):
        from features.assistant.redmine_analysis import monitor_redmine_run

        _agent_monitor_tasks[session_id] = asyncio.create_task(monitor_redmine_run(
            session_id, AgentRequestShim(request), active.get("monitor_token"),
        ))
    return success_response({"session": _session_payload(session)})


@router.post("/api/agent/sessions/{session_id}/cancel")
async def cancel_agent_session(session_id: str, request: Request):
    """Cancel Agent monitoring state. Running tests should still be stopped from the test page."""
    client_id = get_client_id_from_request(request)
    owner_id = owner_id_from_request(request)
    session = _agent_sessions.get(session_id)
    if not session or session.get("client_id") != client_id:
        session = _restore_session(session_id, client_id, owner_id)
    if not session or session.get("client_id") != client_id:
        return success_response(_expired_session_payload(session_id), "Agent session expired")
    task = _agent_monitor_tasks.pop(session_id, None)
    if task and not task.done():
        task.cancel()
    redmine_active = bool((session.get("active_run") or {}).get("redmine_run_id"))
    _finish_run_state(session, "cancelled")
    from features.assistant.project_agent import cancel_project_turn

    cancel_project_turn(session_id)
    detail = ("已停止跟踪 Redmine 分析；后台任务仍可在 Redmine 单号分析页面查看或停止。"
              if redmine_active else "如果测试仍在运行，请到测试界面停止测试")
    _append_step(session, "Agent 已取消", "warning", detail)
    _append_message(session, "assistant", detail if redmine_active else "Agent 会话已取消。如果测试仍在运行，请到测试界面停止测试。")
    return success_response({"session": _session_payload(session)})


@router.get("/api/agent/capabilities")
async def get_agent_capabilities():
    """List supported Agent capabilities with full tool catalog."""
    return success_response(capabilities_payload(kkagent_bridge_availability()))
