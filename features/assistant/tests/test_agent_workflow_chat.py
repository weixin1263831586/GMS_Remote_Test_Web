"""agent_chat 与 WorkflowDraft 的端到端回归：原始指令 → 计划 → 重新规划 → 确认。

固定回归来自一次真实失败对话（对话 Agent 只生成 CTS 计划且设备为空、
「重新规划」退回能力介绍并清空计划）：
1. 原始整句（设备+固件分享+AOA+CTS）→ 生成完整多阶段计划（此前只生成 CTS 测试计划且设备为空）；
2. 「重新规划」→ 保留实体并升版本（此前退回能力介绍并清空计划）；
3. 阻塞计划确认 → 显式禁用执行并展示原因；
4. 预检就绪计划确认 → 提交到 automation（human principal 真实授权）。
"""

import asyncio
from unittest.mock import AsyncMock, patch

from features.assistant import api
from features.assistant.tests.test_agent_session_safety import (
    _bare_request,
    _seed_confirm_session,
)


ORIGINAL_MESSAGE = (
    "设备66df75a34ebde4a4 烧写固件"
    "https://172.16.14.233:5001/api/firmware-shares/"
    "16ec2353bc314483b5a8c92e582a540d/download，"
    "同时执行AOA配置RK3588_evb1_A17_dpi320_wizard-to-usb-debug，"
    "然后支持CTS测试"
)


def _ready_preflight():
    return {
        "ready": True,
        "checks": [{"stage": "device", "ok": True, "detail": "在线"}],
        "blocks": [],
    }


def _patch_preflight(monkeypatch, payload):
    monkeypatch.setattr(
        "features.assistant.workflow_execution.refresh_workflow_preflight",
        lambda _session, _draft: payload)
    monkeypatch.setattr(api, "_refresh_workflow_preflight", lambda _s, _d: payload, raising=False)


def test_composite_message_creates_workflow_plan(monkeypatch):
    saved = dict(api._agent_sessions)
    try:
        _patch_preflight(monkeypatch, _ready_preflight())
        session = _seed_confirm_session(api, {"type": "test_execution"})
        request = _bare_request()
        body = api.AgentChatRequest(session_id="s-confirm", message=ORIGINAL_MESSAGE)

        asyncio.run(api.agent_chat(request, body))

        plan = session["pending_plan"]
        assert plan["type"] == api._WORKFLOW_PLAN_TYPE
        draft = plan["draft"]
        # 修复 1：序列号不再丢失。
        assert draft["device"]["serial"] == "66df75a34ebde4a4"
        # 修复 2：不再误收 URL 里的 share_id。
        assert draft["firmware"]["share_id"] == "16ec2353bc314483b5a8c92e582a540d"
        assert draft["aoa"]["profile"] == "RK3588_evb1_A17_dpi320_wizard-to-usb-debug"
        assert draft["test"]["type"] == "CTS"
        assert session["status"] == "planning"
        content = session["messages"][-1]["content"]
        assert "66df75a34ebde4a4" in content
    finally:
        api._agent_sessions.clear()
        api._agent_sessions.update(saved)


def test_replan_keeps_entities_and_bumps_version(monkeypatch):
    saved = dict(api._agent_sessions)
    try:
        _patch_preflight(monkeypatch, _ready_preflight())
        session = _seed_confirm_session(api, {"type": "test_execution"})
        request = _bare_request()
        asyncio.run(api.agent_chat(
            request, api.AgentChatRequest(session_id="s-confirm", message=ORIGINAL_MESSAGE)))
        draft_v1 = session["pending_plan"]["draft"]
        assert draft_v1["plan_version"] == 1

        asyncio.run(api.agent_chat(
            request, api.AgentChatRequest(session_id="s-confirm", message="重新规划")))

        plan = session["pending_plan"]
        assert plan["type"] == api._WORKFLOW_PLAN_TYPE
        draft = plan["draft"]
        # 修复 3：重新规划保留已解析实体并递增版本，而不是重开能力介绍。
        assert draft["plan_version"] == 2
        assert draft["device"]["serial"] == "66df75a34ebde4a4"
        assert draft["firmware"]["share_id"] == "16ec2353bc314483b5a8c92e582a540d"
        last = session["messages"][-1]["content"]
        assert "v2" in last
    finally:
        api._agent_sessions.clear()
        api._agent_sessions.update(saved)


def test_replan_applies_modifications(monkeypatch):
    saved = dict(api._agent_sessions)
    try:
        _patch_preflight(monkeypatch, _ready_preflight())
        session = _seed_confirm_session(api, {"type": "test_execution"})
        request = _bare_request()
        asyncio.run(api.agent_chat(
            request, api.AgentChatRequest(session_id="s-confirm", message=ORIGINAL_MESSAGE)))

        asyncio.run(api.agent_chat(
            request,
            api.AgentChatRequest(session_id="s-confirm", message="改成 GTS，先只烧写不测试")))

        draft = session["pending_plan"]["draft"]
        assert draft["test"]["type"] == "GTS"
        assert draft["test"]["scope"] == "skip"
        assert draft["plan_version"] == 2
    finally:
        api._agent_sessions.clear()
        api._agent_sessions.update(saved)


def test_unknown_message_keeps_workflow_plan(monkeypatch):
    saved = dict(api._agent_sessions)
    try:
        _patch_preflight(monkeypatch, _ready_preflight())
        session = _seed_confirm_session(api, {"type": "test_execution"})
        request = _bare_request()
        asyncio.run(api.agent_chat(
            request, api.AgentChatRequest(session_id="s-confirm", message=ORIGINAL_MESSAGE)))

        asyncio.run(api.agent_chat(
            request, api.AgentChatRequest(session_id="s-confirm", message="asdkjhzx")))

        assert session["pending_plan"]["type"] == api._WORKFLOW_PLAN_TYPE
        assert session["status"] == "planning"
    finally:
        api._agent_sessions.clear()
        api._agent_sessions.update(saved)


def test_blocked_plan_confirm_is_rejected_with_reasons(monkeypatch):
    saved = dict(api._agent_sessions)
    try:
        blocked = {
            "ready": False,
            "checks": [{"stage": "device", "ok": False, "reason": "设备被占用"}],
            "blocks": [{"stage": "device", "reason": "设备 66df75a34ebde4a4 被占用，不会自动换设备"}],
        }
        _patch_preflight(monkeypatch, blocked)
        session = _seed_confirm_session(api, {"type": "test_execution"})
        request = _bare_request()
        asyncio.run(api.agent_chat(
            request, api.AgentChatRequest(session_id="s-confirm", message=ORIGINAL_MESSAGE)))

        submitted = []
        monkeypatch.setattr(
            api, "_execute_workflow_plan",
            AsyncMock(side_effect=lambda *_a, **_k: submitted.append(1) or {"success": True}))
        asyncio.run(api.agent_chat(
            request, api.AgentChatRequest(session_id="s-confirm", message="确认执行")))

        # 修复 4：阻塞计划禁用执行，只给原因与下一步，不出现“确认后启动”。
        assert not submitted
        assert session["status"] == "planning"
        assert "暂不能执行" in session["messages"][-1]["content"]
        assert "被占用" in session["messages"][-1]["content"]
    finally:
        api._agent_sessions.clear()
        api._agent_sessions.update(saved)


def test_ready_plan_confirm_submits_to_automation(monkeypatch):
    saved = dict(api._agent_sessions)
    try:
        _patch_preflight(monkeypatch, _ready_preflight())
        session = _seed_confirm_session(api, {"type": "test_execution"})
        request = _bare_request()
        asyncio.run(api.agent_chat(
            request, api.AgentChatRequest(session_id="s-confirm", message=ORIGINAL_MESSAGE)))

        created = {}

        def fake_create_run(body, *, created_by="", principal=None):
            created["body"] = body
            created["created_by"] = created_by
            return {"id": "run-123", "status": "queued"}

        monitor = AsyncMock(return_value=None)
        monkeypatch.setattr(
            "features.assistant.workflow_execution.monitor_workflow_run", monitor)
        monkeypatch.setattr(
            api, "_monitor_workflow_run", monitor, raising=False)
        with (
            patch("features.auth.get_authenticated_user", return_value=None),
            patch("features.automation.get_automation_service") as get_service,
        ):
            service = get_service.return_value
            service.create_run.side_effect = fake_create_run
            asyncio.run(api.agent_chat(
                request, api.AgentChatRequest(session_id="s-confirm", message="确认执行")))

        assert created["created_by"] == session.get("owner_id")
        body = created["body"]
        assert body["devices"] == ["66df75a34ebde4a4"]
        assert body["test_plan"]["flash"]["firmware_share_id"] == "16ec2353bc314483b5a8c92e582a540d"
        assert session["active_run"]["automation_run_id"] == "run-123"
        assert session["pending_plan"]["draft"]["status"] == "submitted"
    finally:
        api._agent_sessions.clear()
        api._agent_sessions.update(saved)


def test_cancel_workflow_plan(monkeypatch):
    saved = dict(api._agent_sessions)
    try:
        _patch_preflight(monkeypatch, _ready_preflight())
        session = _seed_confirm_session(api, {"type": "test_execution"})
        request = _bare_request()
        asyncio.run(api.agent_chat(
            request, api.AgentChatRequest(session_id="s-confirm", message=ORIGINAL_MESSAGE)))
        asyncio.run(api.agent_chat(
            request, api.AgentChatRequest(session_id="s-confirm", message="取消计划")))
        assert session["pending_plan"] is None
        assert session["status"] == "idle"
    finally:
        api._agent_sessions.clear()
        api._agent_sessions.update(saved)
