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
        lambda _session, _draft, **_kw: payload)
    monkeypatch.setattr(
        api, "_refresh_workflow_preflight",
        lambda _s, _d, **_kw: payload, raising=False)


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

        def fake_create_run(body, *, created_by="", principal=None, elevated=False):
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


def _plain_user_request(monkeypatch, *, elevated: bool | None):
    """构造带人类身份的请求：user 角色、可选提权标记。"""
    from features.auth import CurrentUser

    request = _bare_request()
    request.state.current_user = CurrentUser(id="u1", username="u1", role="user")
    if elevated is not None:
        request.state.is_elevated = elevated
    return request


def test_unprivileged_user_plan_blocked_with_actionable_hint(monkeypatch):
    """user 角色未提权：计划生成即报权限阻塞，确认不再撞英文 403。"""
    saved = dict(api._agent_sessions)
    try:
        _seed_confirm_session(api, {"type": "test_execution"})
        user_request = _plain_user_request(monkeypatch, elevated=False)
        _seed_confirm_session(api, {"type": "test_execution"}, user_request)
        asyncio.run(api.agent_chat(
            user_request,
            api.AgentChatRequest(session_id="s-confirm", message=ORIGINAL_MESSAGE)))
        session = api._agent_sessions["s-confirm"]
        plan = session["pending_plan"]
        assert plan["type"] == api._WORKFLOW_PLAN_TYPE
        reasons = " ".join(
            block.get("reason") or "" for block in plan["preflight"]["blocks"])
        assert "缺少工作流所需权限" in reasons
        assert "管理员提权" in reasons

        created = []
        monkeypatch.setattr(
            "features.automation.get_automation_service",
            lambda: type("S", (), {"create_run": lambda *a, **k: created.append(1) or {}})())
        asyncio.run(api.agent_chat(
            user_request,
            api.AgentChatRequest(session_id="s-confirm", message="确认执行")))
        assert not created, "权限阻塞的计划不得提交 automation"
        assert "暂不能执行" in session["messages"][-1]["content"]
        assert "缺少工作流所需权限" in session["messages"][-1]["content"]
    finally:
        api._agent_sessions.clear()
        api._agent_sessions.update(saved)


def test_unprivileged_plan_generation_never_touches_remote_lookups(monkeypatch):
    """审核 P1：权限阻塞的请求不得访问 SSH/Worker/固件下载等远程资源。

    无权限必须短路返回：资源 lookups（套件 SSH 查询、集群设备探测、
    固件分享快照、AOA 读取）一次都不能被构造或调用，否则网络不可用时
    用户看到的是连接异常而不是权限提示。
    """
    from features.assistant import workflow_execution as we

    saved = dict(api._agent_sessions)
    try:
        _seed_confirm_session(api, {"type": "test_execution"})
        user_request = _plain_user_request(monkeypatch, elevated=False)
        _seed_confirm_session(api, {"type": "test_execution"}, user_request)

        def _forbidden(*_args, **_kwargs):
            raise AssertionError("权限阻塞的请求不得发起任何远程资源查询")

        monkeypatch.setattr(we, "workflow_preflight_lookups", _forbidden)
        monkeypatch.setattr(we, "_list_suites", _forbidden)
        monkeypatch.setattr(
            we, "workflow_execution_readiness_block", _forbidden)

        asyncio.run(api.agent_chat(
            user_request,
            api.AgentChatRequest(session_id="s-confirm", message=ORIGINAL_MESSAGE)))

        plan = api._agent_sessions["s-confirm"]["pending_plan"]
        assert plan["type"] == api._WORKFLOW_PLAN_TYPE
        reasons = " ".join(
            block.get("reason") or "" for block in plan["preflight"]["blocks"])
        assert "缺少工作流所需权限" in reasons
        assert "管理员提权" in reasons
    finally:
        api._agent_sessions.clear()
        api._agent_sessions.update(saved)


def test_suite_lookup_isolates_ssh_failures(monkeypatch):
    """审核 P1：套件 SSH/网络异常转为阻塞项，不得中断对话预检。"""
    from features.assistant.workflow_draft import preflight_draft
    from features.assistant.workflow_execution import workflow_preflight_lookups

    draft = {
        "device": {"serial": "DEV-1", "worker_id": "w1"},
        "firmware": {"share_id": "abc12345"},
        "aoa": {"mode": "off"},
        "test": {"type": "CTS", "scope": "full"},
    }
    monkeypatch.setattr(
        "features.assistant.workflow_execution._list_suites",
        lambda _context: (_ for _ in ()).throw(RuntimeError("SSH connection failed")))
    lookups = workflow_preflight_lookups({"worker_id": "w1"}, "u1")

    # 套件闭包自身：异常转为 {"ok": False, "reason": ...}，不再抛出。
    suite = lookups["suite_lookup"]("CTS", "w1")
    assert suite["ok"] is False
    assert "测试套件查询暂不可用" in (suite.get("reason") or "")

    # 完整预检：套件查询失败只产生 test 阶段阻塞，其余检查照常返回。
    checks = preflight_draft(
        draft,
        share_lookup=lambda _sid: {"ok": True, "detail": "分享有效"},
        device_lookup=lambda _serial, _worker: {"ok": True, "worker_id": "w1", "detail": "在线"},
        aoa_lookup=lambda _profile, _owner: {"ok": True, "detail": "AOA 可用"},
        suite_lookup=lookups["suite_lookup"],
    )
    test_checks = [item for item in checks["checks"] if item.get("stage") == "test"]
    assert test_checks and test_checks[0]["ok"] is False
    assert checks["ready"] is False


def test_elevated_user_confirms_workflow_with_elevated_flag(monkeypatch):
    """提权会话（管理员重认证）：确认提交并携带 elevated 快照同意。"""
    saved = dict(api._agent_sessions)
    try:
        user_request = _plain_user_request(monkeypatch, elevated=True)
        _seed_confirm_session(api, {"type": "test_execution"}, user_request)
        _patch_preflight(monkeypatch, _ready_preflight())
        asyncio.run(api.agent_chat(
            user_request,
            api.AgentChatRequest(session_id="s-confirm", message=ORIGINAL_MESSAGE)))

        created = {}

        def fake_create_run(body, *, created_by="", principal=None, elevated=False):
            created["elevated"] = elevated
            return {"id": "run-ehr", "status": "queued"}

        monitor = AsyncMock(return_value=None)
        monkeypatch.setattr(
            "features.assistant.workflow_execution.monitor_workflow_run", monitor)
        monkeypatch.setattr(api, "_monitor_workflow_run", monitor, raising=False)
        with (
            patch("features.auth.get_authenticated_user", return_value=None),
            patch("features.automation.get_automation_service") as get_service,
        ):
            service = get_service.return_value
            service.create_run.side_effect = fake_create_run
            asyncio.run(api.agent_chat(
                user_request,
                api.AgentChatRequest(session_id="s-confirm", message="确认执行")))
        assert created.get("elevated") is True
        assert api._agent_sessions["s-confirm"]["active_run"]["automation_run_id"] == "run-ehr"
    finally:
        api._agent_sessions.clear()
        api._agent_sessions.update(saved)


def test_machine_principal_gets_human_only_block_before_preflight(monkeypatch):
    """审核 P1：机器主体在预检期直报 human-only，不得显示 Ready 后创建失败。"""
    from features.assistant.workflow_execution import workflow_permission_block
    from features.auth import CurrentUser

    request = _bare_request()
    request.state.current_user = CurrentUser(
        id="agent-1", username="agent:token", role="agent_service")
    block = workflow_permission_block({}, {}, request)
    assert block is not None
    assert block["stage"] == "permission"
    assert "human-only" in block["reason"]


def test_execution_readiness_reuses_ats_authoritative_preflight(monkeypatch):
    """审核 P1：聊天预检复用 automation preflight（create 同一入口校验）。

    资源 lookups 全部放行时，集群准入失败（内存不足/无命令 Agent 等）
    也必须在预检期阻塞，不能拖到创建 Run 才失败。
    """
    from features.assistant.workflow_execution import (
        refresh_workflow_preflight,
        workflow_execution_readiness_block,
    )

    draft = {
        "device": {"serial": "DEV-1", "worker_id": "w1"},
        "firmware": {"share_id": "abc12345"},
        "aoa": {"mode": "off"},
        "test": {"type": "CTS", "scope": "full"},
    }
    session = {"workspace_context": {"worker_id": "w1"}, "owner_id": "u1"}
    request = _bare_request()
    from features.auth import CurrentUser

    request.state.current_user = CurrentUser(
        id="u1", username="u1", role="device_operator",
        extra_permissions=frozenset({
            "firmware.stage", "devices.lease", "devices.read",
            "devices.use_leased", "tests.execute", "tests.cancel"}),
    )
    request.state.is_elevated = False

    monkeypatch.setattr(
        "features.assistant.workflow_execution.preflight_draft",
        lambda draft, **lookups: {"ready": True, "checks": [], "blocks": []})
    monkeypatch.setattr(
        "features.assistant.workflow_execution.workflow_permission_block",
        lambda draft, workspace, request=None: None)

    class FakeService:
        def __init__(self, outcome):
            self.outcome = outcome

        def preflight(self, body, *, principal=None, elevated=False):
            if isinstance(self.outcome, Exception):
                raise self.outcome
            return self.outcome

    # 集群准入失败 → 预检阻塞并携带原始原因
    monkeypatch.setattr(
        "features.automation.get_automation_service",
        lambda: FakeService(ValueError("Worker w1 is not admissible (memory 12.0 GB < 28 GB)")))
    result = refresh_workflow_preflight(session, draft, request=request)
    assert result["ready"] is False
    execution = [b for b in result["blocks"] if b.get("stage") == "execution"]
    assert execution and "not admissible" in execution[0]["reason"]

    # 通过 → 不追加 execution 阻塞
    monkeypatch.setattr(
        "features.automation.get_automation_service",
        lambda: FakeService({"ready": True}))
    ok = refresh_workflow_preflight(session, draft, request=request)
    assert ok["ready"] is True
    assert not [b for b in ok["blocks"] if b.get("stage") == "execution"]

    # 基础设施异常 → 依赖不可用阻塞而非崩溃
    monkeypatch.setattr(
        "features.automation.get_automation_service",
        lambda: FakeService(RuntimeError("cluster db locked")))
    degraded = workflow_execution_readiness_block(draft, {}, request)
    assert degraded is not None
    assert "暂不可用" in degraded["reason"]
