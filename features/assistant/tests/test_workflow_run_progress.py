"""Agent 步骤面板实时进展与设备预检严格性的回归。

真实失败对话：预检[device] 显示「通过」，确认执行后 automation.create_run
以 "Devices are unavailable" 拒绝（预检只查 serial 存在 + 未占用，执行期还要求
state == available）；同时 run 提交后步骤面板只有一个跳转按钮，看不到推进。
"""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from features.assistant import api
from features.assistant import workflow_execution as we


def _seed_session(run_id: str) -> dict:
    session = {
        "session_id": "s-progress", "owner_id": "alice", "status": "running",
        "messages": [], "steps": [], "workspace_context": {},
        "active_run": {
            "automation_run_id": run_id, "monitor_token": "tok", "workflow_draft": {},
        },
    }
    api._append_step(
        session, "创建 ATS 工作流", "running",
        f"Run {run_id}（烧写 → AOA → 测试）",
        {"automation_run_id": run_id, "status": "queued"},
    )
    return session


def test_update_workflow_run_step_writes_progress_and_status():
    session = _seed_session("run-1")
    run = {"id": "run-1", "status": "flashing", "current_stage": "flashing"}

    assert we.update_workflow_run_step(session, "run-1", run) is True
    step = session["steps"][-1]
    assert step["status"] == "running"
    assert step["data"]["progress"]["status"] == "flashing"
    assert "刷机中" in step["detail"]

    # 状态未变时不要求落盘（返回 False），但进展时间戳仍刷新。
    assert we.update_workflow_run_step(session, "run-1", dict(run)) is False
    assert step["data"]["progress"]["status"] == "flashing"

    done = we.update_workflow_run_step(
        session, "run-1", {"id": "run-1", "status": "completed"})
    assert done is True
    assert step["status"] == "done"


def test_update_workflow_run_step_ignores_foreign_runs():
    session = _seed_session("run-1")
    assert we.update_workflow_run_step(
        session, "run-other", {"id": "run-other", "status": "flashing"}) is False
    assert session["steps"][-1]["data"].get("progress") is None


def test_download_counters_refresh_without_extra_anchor_writes():
    session = _seed_session("run-1")
    run = {"status": "artifact_fetching", "result_json": json.dumps({"artifact_download": {
        "total_bytes": 100, "downloaded_bytes": 20, "bytes_per_second": 10,
        "password": "must-not-be-exposed",
    }})}
    assert we.update_workflow_run_step(session, "run-1", run)
    run["result_json"] = run["result_json"].replace('"downloaded_bytes": 20', '"downloaded_bytes": 50')
    assert we.update_workflow_run_step(session, "run-1", run) is False
    progress = session["steps"][-1]["data"]["progress"]
    assert progress["artifact_download"]["percent"] == 50
    assert "must-not-be-exposed" not in json.dumps(progress)


@pytest.mark.asyncio
async def test_monitor_loop_streams_progress_until_terminal(monkeypatch):
    session = _seed_session("run-1")
    api._agent_sessions["s-progress"] = session
    runs = [
        {"id": "run-1", "status": "flashing", "current_stage": "flashing",
         "cluster_job_id": "job-1"},
        {"id": "run-1", "status": "testing", "current_stage": "testing"},
        {"id": "run-1", "status": "completed",
         "report_timestamp": "2026-11-10T12:00:00"},
    ]

    class FakeService:
        def get_run(self, _run_id):
            if len(runs) > 1:
                return runs.pop(0)
            return runs[0]

    monkeypatch.setattr(api, "_save_session_anchor", lambda _session: None)
    monkeypatch.setattr(
        "features.assistant.session_registry.clear_session_anchor",
        lambda _session: None)
    monkeypatch.setattr(asyncio, "sleep", _fake_sleep)
    with (
        patch("features.auth.get_authenticated_user", return_value=None),
        patch("features.automation.get_automation_service",
              return_value=FakeService()),
    ):
        await asyncio.wait_for(
            we.monitor_workflow_run(
                "s-progress", SimpleNamespace(headers={}, cookies={}, client=None),
                "tok"),
            timeout=5,
        )

    step = session["steps"][-2]
    assert step["title"] == "创建 ATS 工作流"
    assert step["status"] == "done"
    assert step["data"]["progress"]["status"] == "completed"
    assert session["steps"][-1]["title"] == "ATS 工作流结束"
    assert session["active_run"] is None
    assert session["status"] == "done"
    # 终态消息把 workspace 关联到报告，便于跳转分析。
    assert session["workspace_context"]["report_timestamp"] == "2026-11-10T12:00:00"


@pytest.mark.asyncio
async def test_monitor_loop_marks_step_error_on_failed_status(monkeypatch):
    session = _seed_session("run-1")
    api._agent_sessions["s-progress"] = session
    runs = [
        {"id": "run-1", "status": "flashing", "cluster_job_id": "job-1"},
        {"id": "run-1", "status": "flash_failed", "error": "No firmware artifact path/url"},
    ]

    class FakeService:
        def get_run(self, _run_id):
            if len(runs) > 1:
                return runs.pop(0)
            return runs[0]

    monkeypatch.setattr(api, "_save_session_anchor", lambda _session: None)
    monkeypatch.setattr(
        "features.assistant.session_registry.clear_session_anchor",
        lambda _session: None)
    monkeypatch.setattr(asyncio, "sleep", _fake_sleep)
    with (
        patch("features.auth.get_authenticated_user", return_value=None),
        patch("features.automation.get_automation_service",
              return_value=FakeService()),
    ):
        await asyncio.wait_for(
            we.monitor_workflow_run(
                "s-progress", SimpleNamespace(headers={}, cookies={}, client=None),
                "tok"),
            timeout=5,
        )

    step = session["steps"][-2]
    assert step["status"] == "error"
    assert step["data"]["progress"]["status"] == "flash_failed"
    assert step["data"]["progress"]["error"] == "No firmware artifact path/url"
    assert "No firmware artifact path/url" in step["detail"]
    terminal = session["steps"][-1]
    assert terminal["data"]["progress"]["status"] == "flash_failed"
    assert terminal["data"]["progress"]["error"] == "No firmware artifact path/url"
    assert "No firmware artifact path/url" in session["messages"][-1]["content"]
    assert session["status"] == "error"


_REAL_SLEEP = asyncio.sleep


async def _fake_sleep(_seconds):
    # monkeypatch 已把 asyncio.sleep 换成此函数，必须用打补丁前保存的
    # 原始引用让出控制权，否则无限递归。
    await _REAL_SLEEP(0)


def _fake_cluster(rows):
    return SimpleNamespace(
        repository=SimpleNamespace(list_devices=lambda worker_id=None: rows))


def test_device_preflight_matches_execution_validation(monkeypatch):
    rows = [
        {"serial": "S-busy", "worker_id": "w1", "state": "busy",
         "claimed": False},
        {"serial": "S-ok", "worker_id": "w1", "state": "available",
         "claimed": False},
        # 回归：此前 worker_id 为空的库存行会绕过 Worker 匹配检查。
        {"serial": "S-orphan", "worker_id": "", "state": "available",
         "claimed": False},
    ]
    monkeypatch.setattr(
        "features.cluster.get_cluster_service", lambda: _fake_cluster(rows))
    lookups = we.workflow_preflight_lookups({"worker_id": "w1"}, "alice")
    device_lookup = lookups["device_lookup"]

    busy = device_lookup("S-busy", "w1")
    assert busy["ok"] is False
    assert "非空闲" in busy["reason"]

    ok = device_lookup("S-ok", "w1")
    assert ok == {"ok": True, "worker_id": "w1",
                  "detail": "Worker w1 / available"}

    orphan = device_lookup("S-orphan", "w1")
    assert orphan["ok"] is False
    assert "与指定 Worker" in orphan["reason"]
