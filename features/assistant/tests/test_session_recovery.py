"""外部深度评审报告项回归：Agent 会话持久关联与 Controller 重启恢复。

- 执行计划拿到 cluster_job_id 后落盘恢复锚点（不含 pre_actions 密码）；
- 重启后同 owner 的 GET /sessions/{id} 能重建监控状态并续接监控协程，
  异 owner 不能；
- 恢复会话（无持久化 plan）降级为纯跟踪直到任务终态并清锚点；
- 终态（done/cancelled/error）清锚点；损坏的存储文件按空处理。
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

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


def _tmp_store(tmp_path):
    return tmp_path / "agent_sessions.json"


def test_execute_plan_persists_anchor_with_job_link(tmp_path):
    from features.assistant import api, session_registry, session_store

    store = _tmp_store(tmp_path)
    session = {
        "session_id": "s-anchor",
        "client_id": "c1",
        "owner_id": "user-42",
        "status": "idle",
        "messages": [],
        "steps": [],
        "pending_plan": None,
        "active_run": None,
        "workspace_context": {},
    }
    plan = {"request": {"devices": ["ABC"], "test_suite": "cts", "worker_id": "w1"}}
    request = _agent_request("user-42")

    async def scenario():
        with patch.object(
            api.test_report_db, "get_reports", return_value=[]
        ), patch.object(
            api, "_run_pre_actions", new=AsyncMock(return_value={"success": True})
        ), patch.object(
            api, "_start_test_with_plan", new=AsyncMock(return_value={"success": True})
        ), patch.object(
            api, "_monitor_agent_run", new=AsyncMock()
        ):
            await api._execute_plan(session, request, plan)

    asyncio.run(scenario())
    # _start_test_with_plan 被 mock 不会写 correlation，手工模拟其锚点写入
    session.setdefault("active_run", {}).update({
        "cluster_job_id": "job-9", "attempt_id": "a1", "worker_id": "w1",
    })
    with patch.object(session_registry.session_store, "default_sessions_path", return_value=store):
        api._save_session_anchor(session)

    anchors = session_store.load_anchors(store)
    assert anchors["s-anchor"]["active_run"]["cluster_job_id"] == "job-9"
    assert anchors["s-anchor"]["client_id"] == "c1"


def test_anchor_never_persists_pre_actions_password(tmp_path):
    from features.assistant import session_store

    session = {
        "session_id": "s-secret",
        "client_id": "c1",
        "owner_id": "user-42",
        "status": "running",
        "active_run": {
            "plan": {"pre_actions": [{"type": "connect_wifi", "password": "wifi-secret-pw"}]},
            "cluster_job_id": "job-1",
            "attempt_id": "a",
        },
    }
    store = _tmp_store(tmp_path)
    with patch.object(session_store, "default_sessions_path", return_value=store):
        session_store.save_anchor(session)
    raw = store.read_text(encoding="utf-8")
    assert "wifi-secret-pw" not in raw
    assert "pre_actions" not in raw
    anchors = session_store.load_anchors(store)
    assert anchors["s-secret"]["active_run"]["cluster_job_id"] == "job-1"


def test_restored_session_rebuilds_monitoring_state_for_owner(tmp_path):
    from features.assistant import api, session_registry, session_store

    store = _tmp_store(tmp_path)
    session_store.save_anchor({
        "session_id": "s-res",
        "client_id": "c1",
        "owner_id": "user-42",
        "status": "monitoring",
        "active_run": {"cluster_job_id": "job-7", "attempt_id": "a2", "worker_id": "w2"},
        "workspace_context": {"scope_mode": "cluster", "worker_id": "w2"},
        "created_at": "2026-10-16T00:00:00",
        "updated_at": "2026-10-16T00:00:00",
    }, path=store)

    with patch.object(session_registry.session_store, "default_sessions_path", return_value=store):
        rebuilt = api._restore_session("s-res", "c1")
        assert rebuilt is not None
        assert rebuilt["active_run"]["cluster_job_id"] == "job-7"
        assert rebuilt["status"] == "monitoring"
        assert rebuilt.get("recovered") is True
        # 其他 client 无法恢复他人会话
        assert api._restore_session("s-res", "someone-else") is None
        # 无 cluster_job_id 的锚点不构成可恢复会话
        session_store.save_anchor({"session_id": "s-empty", "client_id": "c1"}, path=store)
        assert api._restore_session("s-empty", "c1") is None


def test_recovered_session_without_plan_still_tracked_to_terminal(tmp_path):
    """恢复会话无持久化 plan：降级为纯跟踪，等任务终态并清锚点。"""
    from features.assistant import api

    session = {
        "session_id": "s-track",
        "client_id": "c1",
        "owner_id": "user-42",
        "status": "monitoring",
        "recovered": True,
        "active_run": {"cluster_job_id": "job-8", "attempt_id": "a1"},
        "workspace_context": {},
        "messages": [],
        "steps": [],
    }
    api._agent_sessions["s-track"] = session
    jobs = iter([
        {"id": "job-8", "status": "running", "assigned_worker_id": "w1"},
        {"id": "job-8", "status": "completed", "assigned_worker_id": "w1"},
    ])
    cluster = SimpleNamespace(repository=SimpleNamespace(
        get_job=MagicMock(side_effect=lambda _id: next(jobs))
    ))
    report = {
        "timestamp": "cluster-job-8",
        "report_id": "cluster:job-8:attempt-1",
        "status": "completed",
        "fail": 0,
        "total": 1,
        "cluster_job_id": "job-8",
    }
    request = _agent_request("user-42")

    async def no_wait(_seconds):
        return None

    try:
        with (
            patch("features.cluster.get_cluster_service", return_value=cluster),
            patch.object(api, "_report_for_cluster_job", return_value=report),
            patch.object(api.asyncio, "sleep", side_effect=no_wait),
        ):
            asyncio.run(api._monitor_agent_run("s-track", request))
    finally:
        api._agent_sessions.pop("s-track", None)
        api._agent_monitor_tasks.pop("s-track", None)

    assert session["status"] == "done"
    assert session["active_run"] is None
    assert session["workspace_context"]["cluster_job_id"] == "job-8"
    assert session["workspace_context"]["report_timestamp"] == "cluster-job-8"


def test_get_agent_session_restores_and_resumes_monitoring(tmp_path):
    from features.assistant import api, session_registry, session_store

    store = _tmp_store(tmp_path)
    session_store.save_anchor({
        "session_id": "s-api",
        "client_id": "c1",
        "owner_id": "user-42",
        "status": "monitoring",
        "active_run": {"cluster_job_id": "job-9", "attempt_id": "a1", "worker_id": "w1"},
        "created_at": "2026-10-16T00:00:00",
        "updated_at": "2026-10-16T00:00:00",
    }, path=store)

    request = _agent_request("user-42")
    with (
        patch.object(session_registry.session_store, "default_sessions_path", return_value=store),
        patch.object(api, "_monitor_agent_run", new=AsyncMock()),
    ):
        try:
            response = asyncio.run(api.get_agent_session("s-api", request))
            body = json.loads(response.body.decode("utf-8"))
            assert body["data"]["session"]["active_run"]["cluster_job_id"] == "job-9"
            task = api._agent_monitor_tasks.get("s-api")
            assert task is not None
            task.cancel()
            # 不同 agent token 是不同 actor id（ADR 0010）：client_id 与
            # owner 均不匹配，拿不到他人会话
            intruder = _agent_request("intruder")
            intruder.state.current_user = CurrentUser(
                id="agent:other-token",
                username="agent",
                role="agent_service",
                resource_owner_id="someone-else",
            )
            response = asyncio.run(api.get_agent_session("s-api", intruder))
            body = json.loads(response.body.decode("utf-8"))
            assert body["data"]["expired"] is True
        finally:
            api._agent_sessions.pop("s-api", None)
            api._agent_monitor_tasks.pop("s-api", None)


def test_terminal_states_clear_anchor(tmp_path):
    from features.assistant import api, session_registry

    store = _tmp_store(tmp_path)
    session = {
        "session_id": "s-term",
        "client_id": "c1",
        "owner_id": "user-42",
        "status": "monitoring",
        "messages": [],
        "steps": [],
        "active_run": {"cluster_job_id": "job-3", "attempt_id": "a"},
    }
    with patch.object(session_registry.session_store, "default_sessions_path", return_value=store):
        api._save_session_anchor(session)
        assert json.loads(store.read_text(encoding="utf-8"))["sessions"].get("s-term")
        session["active_run"] = None
        api._clear_session_anchor(session)
        assert not json.loads(store.read_text(encoding="utf-8"))["sessions"].get("s-term")


def test_corrupt_store_loads_empty(tmp_path):
    from features.assistant import session_store

    store = _tmp_store(tmp_path)
    store.write_text('{"sessions": {"x": broken', encoding="utf-8")
    assert session_store.load_anchors(store) == {}
    store.write_text("not json at all", encoding="utf-8")
    assert session_store.load_anchors(store) == {}


def test_anchor_store_file_and_tmp_are_owner_only(tmp_path):
    """锚点文件 0600：umask 022 下不得出现 0644（外部评审报告项）。"""
    import os
    import stat

    from features.assistant import session_store

    store = _tmp_store(tmp_path)
    old_umask = os.umask(0o022)
    try:
        session_store.save_anchor(
            {"session_id": "s-mode", "client_id": "c1", "owner_id": "u1"},
            path=store,
        )
    finally:
        os.umask(old_umask)
    assert stat.S_IMODE(store.stat().st_mode) == 0o600
    # 原子替换后不残留临时文件。
    assert not list(tmp_path.glob("*.tmp"))


def test_save_anchor_failure_marks_session_unrecoverable(caplog):
    """持久化失败不再静默：日志提升为 warning，并在会话上标记不可恢复。"""
    import logging

    from features.assistant import api, session_registry

    session = {"session_id": "s-fail", "client_id": "c1"}
    with caplog.at_level(logging.WARNING, logger=api.logger.name), patch.object(
        session_registry.session_store, "save_anchor", side_effect=OSError("disk full")
    ):
        api._save_session_anchor(session)
    assert session.get("recovery_anchor_failed") is True
    assert any("不可恢复" in record.message for record in caplog.records)


def test_clear_anchor_success_resets_failure_flag():
    from features.assistant import api

    session = {
        "session_id": "s-reset",
        "client_id": "c1",
        "recovery_anchor_failed": True,
    }
    api._clear_session_anchor(session)
    assert "recovery_anchor_failed" not in session


def test_restore_reuses_live_session_for_same_owner_instead_of_rebuild(tmp_path):
    """同属主第二客户端恢复时必须复用内存会话对象。

    监控任务正持有并更新内存对象；若从锚点重建覆盖，监控会继续写旧
    对象、新查询拿到停滞副本（同属主并发恢复竞态场景）。
    """
    from features.assistant import api, session_registry, session_store

    store = _tmp_store(tmp_path)
    session_store.save_anchor({
        "session_id": "s-live",
        "client_id": "c1",
        "owner_id": "user-42",
        "status": "monitoring",
        "active_run": {"cluster_job_id": "job-x", "attempt_id": "a1", "worker_id": "w1"},
        "created_at": "2026-10-26T00:00:00",
        "updated_at": "2026-10-26T00:00:00",
    }, path=store)
    live_session = {
        "session_id": "s-live",
        "client_id": "c1",
        "owner_id": "user-42",
        "status": "monitoring",
        "messages": [{"role": "assistant", "content": "运行中"}],
        "steps": [{"title": "监控中"}],
        "active_run": {"cluster_job_id": "job-x", "monitor_token": "tok-live"},
        "workspace_context": {"cluster_job_id": "job-x"},
    }
    api._agent_sessions["s-live"] = live_session
    try:
        with patch.object(
            session_registry.session_store, "default_sessions_path", return_value=store,
        ):
            restored = api._restore_session("s-live", "client-b", "user-42")
        assert restored is live_session  # 同一对象，不是锚点重建
        assert restored["client_id"] == "client-b"
        assert restored["messages"] == [{"role": "assistant", "content": "运行中"}]
        assert restored["active_run"]["monitor_token"] == "tok-live"
        assert restored.get("recovered") is not True
    finally:
        api._agent_sessions.pop("s-live", None)


def test_failed_plan_start_clears_active_run_and_anchor(tmp_path):
    """启动失败终态：状态、active_run 与恢复锚点一起清理，UI 不残留。"""
    from features.assistant import api, session_registry, session_store

    store = _tmp_store(tmp_path)
    session = {
        "session_id": "s-failstart",
        "client_id": "c1",
        "owner_id": "user-42",
        "status": "running",
        "messages": [],
        "steps": [],
        "active_run": None,
        "workspace_context": {},
    }
    plan = {"request": {"devices": ["ABC"], "test_suite": "cts", "worker_id": "w1"}}
    request = _agent_request("user-42")

    async def scenario():
        with patch.object(
            api.test_report_db, "get_reports", return_value=[]
        ), patch.object(
            api, "_run_pre_actions",
            new=AsyncMock(return_value={"success": False, "error": "烧写失败"}),
        ), patch.object(
            session_registry.session_store, "default_sessions_path", return_value=store,
        ):
            return await api._execute_plan(session, request, plan)

    payload = asyncio.run(scenario())
    assert payload["success"] is False
    assert session["status"] == "error"
    assert session["active_run"] is None
    anchors = session_store.load_anchors(store)
    assert not anchors.get("s-failstart")
