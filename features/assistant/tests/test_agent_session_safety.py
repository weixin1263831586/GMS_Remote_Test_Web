"""Agent 会话安全回归：

- 报告 owner 过滤走请求主体的 resource_owner_id（ADR 0010），agent 主体
  映射回注册账号，而不是会话里的 client_id 回退；
- running/monitoring 会话超过最长存活时间纳入清扫并取消监控任务；
- 测试计划回显中的 pre_actions[].password 不出明文，服务端保留执行。
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from features.auth import CurrentUser


def _agent_request(resource_owner_id: str = "user-42"):
    principal = CurrentUser(
        id="agent:t1",
        username="agent",
        role="agent_service",
        resource_owner_id=resource_owner_id,
    )
    return SimpleNamespace(
        state=SimpleNamespace(current_user=principal),
        headers={},
        cookies={},
        client=SimpleNamespace(host="127.0.0.1", port=80),
        method="POST",
        url=None,
        query_params={},
    )


def test_execute_plan_resolves_report_owner_from_request_principal():
    from features.assistant import api

    session = {
        "session_id": "s1",
        "client_id": "probe@192.0.2.7",
        "status": "idle",
        "messages": [],
        "steps": [],
        "pending_plan": None,
        "active_run": None,
    }
    plan = {"request": {"devices": ["ABC"], "test_suite": "cts", "worker_id": "w1"}}
    request = _agent_request("user-42")

    async def scenario():
        with patch.object(
            api.test_report_db,
            "get_reports",
            return_value=[{"timestamp": "T1"}],
        ) as get_reports, patch.object(
            api, "_run_pre_actions", new=AsyncMock(return_value={"success": True})
        ), patch.object(
            api,
            "_start_test_with_plan",
            new=AsyncMock(return_value={"success": True}),
        ), patch.object(
            api, "_monitor_agent_run", new=AsyncMock()
        ):
            await api._execute_plan(session, request, plan)
        return get_reports

    get_reports = asyncio.run(scenario())
    assert get_reports.call_args.kwargs.get("owner_id") == "user-42"
    assert get_reports.call_args.kwargs.get("limit") == 20


def test_analyze_saved_report_uses_request_owner():
    from features.assistant import api

    session = {"messages": [], "steps": []}
    request = _agent_request("owner-7")

    with patch.object(
        api.test_report_db, "get_report_by_timestamp", return_value=None
    ) as get_by_ts:
        asyncio.run(api._analyze_saved_report(session, "T9", request))

    assert get_by_ts.call_args.kwargs.get("owner_id") == "owner-7"


def test_cleanup_sweeps_over_lifetime_monitoring_sessions():
    from features.assistant import api
    from features.assistant.session_registry import (
        AGENT_SESSION_MAX_LIFETIME_SECONDS,
        AGENT_SESSION_TTL_SECONDS,
    )

    now = datetime.now()
    fresh_running = {
        "session_id": "fresh",
        "client_id": "c1",
        "status": "monitoring",
        "created_at": now.isoformat(),
        "updated_at": now.isoformat(),
    }
    zombie_running = {
        "session_id": "zombie",
        "client_id": "c2",
        "status": "running",
        "created_at": (
            now - timedelta(seconds=AGENT_SESSION_MAX_LIFETIME_SECONDS + 3600)
        ).isoformat(),
        "updated_at": (
            now - timedelta(seconds=AGENT_SESSION_MAX_LIFETIME_SECONDS + 3600)
        ).isoformat(),
    }
    stale_idle = {
        "session_id": "idle-old",
        "client_id": "c3",
        "status": "idle",
        "created_at": (
            now - timedelta(seconds=AGENT_SESSION_TTL_SECONDS + 3600)
        ).isoformat(),
        "updated_at": (
            now - timedelta(seconds=AGENT_SESSION_TTL_SECONDS + 3600)
        ).isoformat(),
    }

    saved_sessions = dict(api._agent_sessions)
    saved_tasks = dict(api._agent_monitor_tasks)
    api._agent_sessions.clear()
    api._agent_monitor_tasks.clear()
    api._agent_sessions.update(
        {s["session_id"]: s for s in (fresh_running, zombie_running, stale_idle)}
    )

    async def scenario():
        async def zombie_loop():
            await asyncio.sleep(600)

        task = asyncio.create_task(zombie_loop())
        api._agent_monitor_tasks["zombie"] = task
        await api._cleanup_sessions()
        await asyncio.sleep(0)
        return task

    try:
        task = asyncio.run(scenario())
        assert set(api._agent_sessions) == {"fresh"}
        assert "zombie" not in api._agent_monitor_tasks
        assert task.done()
    finally:
        api._agent_sessions.clear()
        api._agent_sessions.update(saved_sessions)
        api._agent_monitor_tasks.clear()
        api._agent_monitor_tasks.update(saved_tasks)


def test_plan_echo_hides_pre_actions_password_but_keeps_it_server_side():
    from features.assistant import api

    session = {
        "session_id": "s2",
        "client_id": "c@192.0.2.8",
        "status": "planning",
        "messages": [],
        "steps": [],
        "pending_plan": None,
        "active_run": None,
    }
    plan = {
        "type": "test_execution",
        "steps": [],
        "request": {"devices": ["ABC"], "test_suite": "cts"},
        "pre_actions": [
            {"type": "connect_wifi", "ssid": "lab", "password": "wifi-secret-pw"}
        ],
    }
    session["pending_plan"] = plan
    api._append_message(
        session,
        "assistant",
        "计划已生成",
        kind="plan",
        data={"plan": json.loads(json.dumps(plan))},
    )

    payload = json.dumps(api._session_payload(session), ensure_ascii=False)
    assert "wifi-secret-pw" not in payload
    masked = [
        action
        for action in (
            json.loads(payload).get("pending_plan", {}).get("pre_actions", [])
        )
    ]
    assert masked and masked[0]["password"] != "wifi-secret-pw"
    # 服务端保留明文用于执行（_run_pre_actions 读取）。
    assert session["pending_plan"]["pre_actions"][0]["password"] == "wifi-secret-pw"
