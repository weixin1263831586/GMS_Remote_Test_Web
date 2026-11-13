"""会话目录（多会话管理）回归：

- GET /api/agent/sessions 只返回本客户端的会话（内存 + 可恢复锚点合并）；
- 首条用户消息派生会话标题，持久锚点携带标题；
- DELETE /api/agent/sessions/{id} 清理内存会话、监控任务与锚点；
- 他人会话不可见也不可删除（404）。
"""

from __future__ import annotations

import asyncio
import json
import uuid
from contextlib import contextmanager

import pytest

from features.assistant import api, session_store
from features.assistant.tests.test_agent_session_safety import _bare_request


def _json(response) -> dict:
    return json.loads(response.body)


@contextmanager
def _client(request, client_id: str):
    saved = api.get_client_id_from_request
    api.get_client_id_from_request = lambda _request: client_id
    try:
        yield
    finally:
        api.get_client_id_from_request = saved


def _seed(session_id: str, client_id: str, *, status="idle", title=""):
    session = {
        "session_id": session_id,
        "client_id": client_id,
        "owner_id": "",
        "title": title,
        "status": status,
        "messages": [],
        "steps": [],
        "pending_plan": None,
        "active_run": None,
        "created_at": "2026-11-12T02:00:00+00:00",
        "updated_at": "2026-11-12T02:00:00+00:00",
    }
    api._agent_sessions[session_id] = session
    return session


def test_directory_lists_only_own_sessions():
    saved = dict(api._agent_sessions)
    api._agent_sessions.clear()
    try:
        _seed("s-mine", "client-A")
        _seed("s-other", "client-B")
        request = _bare_request()
        with _client(request, "client-A"):
            result = _json(asyncio.run(api.list_agent_sessions(request)))
        sessions = result["data"]["sessions"]
        assert [item["session_id"] for item in sessions] == ["s-mine"]
        entry = sessions[0]
        assert entry["title"] == "未命名会话"
        assert entry["active"] is False
    finally:
        api._agent_sessions.clear()
        api._agent_sessions.update(saved)


def test_title_derived_from_first_user_message():
    from features.assistant.session_log import _append_message

    session = {"title": "", "messages": [], "steps": [], "updated_at": ""}
    _append_message(session, "user", "  \n跑 CtsWifiTestCases 并 retry\n第二行")
    assert session["title"] == "跑 CtsWifiTestCases 并 retry"
    _append_message(session, "user", "第二条消息不应覆盖标题")
    assert session["title"] == "跑 CtsWifiTestCases 并 retry"


def test_delete_drops_live_session_and_anchor(monkeypatch):
    saved = dict(api._agent_sessions)
    dropped = []
    monkeypatch.setattr(session_store, "drop_anchor", lambda sid: dropped.append(sid))
    try:
        _seed("s-del", "client-A", status="monitoring")
        request = _bare_request()
        with _client(request, "client-A"):
            result = _json(asyncio.run(api.delete_agent_session("s-del", request)))
        assert result["success"] is True
        assert "s-del" not in api._agent_sessions
        assert dropped == ["s-del"]
    finally:
        api._agent_sessions.clear()
        api._agent_sessions.update(saved)


def test_delete_rejects_foreign_session():
    saved = dict(api._agent_sessions)
    try:
        _seed("s-foreign", "client-B")
        request = _bare_request()
        with _client(request, "client-A"):
            result = _json(asyncio.run(api.delete_agent_session("s-foreign", request)))
        assert result["success"] is False
        assert "s-foreign" in api._agent_sessions
    finally:
        api._agent_sessions.clear()
        api._agent_sessions.update(saved)


@pytest.mark.parametrize("client_id,owner_id,expected_status", [
    ("client-old", "owner-a", 200),
    ("client-new", "owner-a", 200),
    ("client-new", "owner-b", 404),
])
def test_delete_transcript_only_session_after_restart(
    tmp_path, monkeypatch, client_id, owner_id, expected_status,
):
    transcripts = tmp_path / "transcripts"
    monkeypatch.setattr(session_store, "default_transcripts_dir", lambda: transcripts)
    monkeypatch.setattr(api, "owner_id_from_request", lambda _request: owner_id)
    saved = dict(api._agent_sessions)
    api._agent_sessions.clear()
    session_id = str(uuid.uuid4())
    session_store.save_transcript({
        "session_id": session_id, "client_id": "client-old", "owner_id": "owner-a",
        "status": "idle", "title": "重启前的历史会话",
        "messages": [{"role": "user", "content": "保留的对话"}], "steps": [],
    })
    try:
        # The directory includes transcripts even with no live state or anchor.
        own_entries = api._session_directory("client-old", "owner-a")
        assert [item["session_id"] for item in own_entries] == [session_id]
        assert not session_store.load_anchors().get(session_id)
        assert session_id not in api._agent_sessions
        request = _bare_request()
        with _client(request, client_id):
            response = asyncio.run(api.delete_agent_session(session_id, request))
        assert response.status_code == expected_status
        assert _json(response)["success"] is (expected_status == 200)
        if expected_status == 200:
            assert session_store.load_transcript(session_id) is None
            assert api._session_directory(client_id, owner_id) == []
        else:
            assert session_store.load_transcript(session_id) is not None
        assert session_id not in api._agent_sessions
    finally:
        api._agent_sessions.clear()
        api._agent_sessions.update(saved)


def test_anchor_roundtrip_keeps_title(tmp_path):
    session = {
        "session_id": "anchor-1",
        "client_id": "client-A",
        "owner_id": "",
        "title": "烧写固件并跑 CTS",
        "status": "monitoring",
        "created_at": "2026-11-12T02:00:00+00:00",
        "updated_at": "2026-11-12T02:01:00+00:00",
        "active_run": {
            "automation_run_id": "run-1",
            "started_at": "2026-11-12T02:01:00+00:00",
        },
        "workspace_context": {"device_ids": ["D1"]},
        "messages": [{"role": "user", "content": "不应落盘"}],
    }
    store = tmp_path / "sessions.json"
    session_store.save_anchor(session, store)
    anchor = session_store.load_anchors(store)["anchor-1"]
    assert anchor["title"] == "烧写固件并跑 CTS"
    assert "messages" not in anchor
    entry = api._session_directory_entry("anchor-1", anchor)
    assert entry["title"] == "烧写固件并跑 CTS"
    assert entry["status"] == "monitoring"


def test_transcript_persist_and_restore_after_restart(tmp_path, monkeypatch):
    """强刷/重启恢复回归：消息落盘 → 内存清空 → 恢复正文。"""
    import uuid

    from features.assistant.session_log import _append_message

    transcripts = tmp_path / "transcripts"
    monkeypatch.setattr(session_store, "default_transcripts_dir", lambda: transcripts)
    saved_sessions = dict(api._agent_sessions)
    session = {
        "session_id": str(uuid.uuid4()),
        "client_id": "client-A",
        "owner_id": "",
        "title": "",
        "status": "idle",
        "messages": [],
        "steps": [],
        "pending_plan": None,
        "active_run": None,
        "workspace_context": {"device_ids": ["D1"]},
        "created_at": "2026-11-12T02:00:00+00:00",
        "updated_at": "2026-11-12T02:00:00+00:00",
    }
    transcripts = tmp_path / "transcripts"
    session_store.save_transcript(session, transcripts)  # 无消息，不落盘
    assert not list(transcripts.glob("*.json"))

    _append_message(session, "user", "跑 CtsWifiTestCases 并 retry")
    _append_message(session, "assistant", "已解析为多阶段工作流（计划 v1）")
    files = list(transcripts.glob("*.json"))
    assert len(files) == 1
    assert files[0].stat().st_mode & 0o777 == 0o600

    # 重启后内存清空，从记录恢复；无锚点（active_run 为空）走纯记录恢复。
    record = session_store.load_transcript(session["session_id"], transcripts)
    assert [m["content"] for m in record["messages"]] == [
        "跑 CtsWifiTestCases 并 retry", "已解析为多阶段工作流（计划 v1）"]
    assert record["title"] == "跑 CtsWifiTestCases 并 retry"

    restored = api._restore_session_from_transcript(session["session_id"], "client-A", "")
    assert restored is not None
    assert [m["content"] for m in restored["messages"]] == [
        "跑 CtsWifiTestCases 并 retry", "已解析为多阶段工作流（计划 v1）"]
    assert restored["status"] == "idle"
    # 立即清内存，避免污染同进程的其它目录断言。
    api._agent_sessions.pop(session["session_id"], None)

    # 他人不可恢复
    assert api._restore_session_from_transcript(session["session_id"], "client-B", "") is None
    api._agent_sessions.clear()
    api._agent_sessions.update(saved_sessions)

    # DELETE 清记录
    session_store.drop_transcript(session["session_id"], transcripts)
    assert session_store.load_transcript(session["session_id"], transcripts) is None


def test_transcript_running_status_downgrades_on_restore(tmp_path, monkeypatch):
    """记录里的 running 状态在重启后无人续接，恢复为 idle。"""
    import uuid

    session_id = str(uuid.uuid4())
    transcripts = tmp_path / "transcripts"
    transcripts.mkdir(parents=True)
    monkeypatch.setattr(session_store, "default_transcripts_dir", lambda: transcripts)
    (transcripts / f"{session_id}.json").write_text(json.dumps({
        "version": 1, "session_id": session_id, "client_id": "client-A",
        "owner_id": "", "title": "运行中会话", "status": "running",
        "workspace_context": {}, "created_at": "2026-11-12T02:00:00+00:00",
        "updated_at": "2026-11-12T02:01:00+00:00",
        "messages": [], "steps": [],
    }), encoding="utf-8")
    restored = api._restore_session_from_transcript(session_id, "client-A", "")
    assert restored is not None
    assert restored["status"] == "idle"
    assert restored["recovered"] is True
    api._agent_sessions.pop(session_id, None)


def test_directory_includes_transcript_only_sessions(tmp_path, monkeypatch):
    """重启后目录仍能看到会话（记录文件来源）。"""
    import uuid

    saved_sessions = dict(api._agent_sessions)
    session_id = str(uuid.uuid4())
    record = {
        "session_id": session_id, "client_id": "client-A", "owner_id": "",
        "title": "重启前会话", "status": "done", "workspace_context": {},
        "created_at": "2026-11-12T02:00:00+00:00",
        "updated_at": "2026-11-12T02:01:00+00:00", "messages": [], "steps": [],
    }
    monkeypatch.setattr(session_store, "load_transcripts", lambda: {session_id: record})
    try:
        entries = api._session_directory("client-A", "")
        assert [e["session_id"] for e in entries] == [session_id]
        assert entries[0]["title"] == "重启前会话"
        assert entries[0]["active"] is False
    finally:
        api._agent_sessions.clear()
        api._agent_sessions.update(saved_sessions)


def test_transcript_rejects_non_uuid_session_id(tmp_path):
    """路径注入防护：非 uuid 会话 ID 不落盘也不读取。"""
    session = {"session_id": "../../etc/passwd", "client_id": "c",
               "messages": [{"role": "user", "content": "x"}], "steps": []}
    session_store.save_transcript(session, tmp_path)
    assert list(tmp_path.glob("*.json")) == []
    assert session_store.load_transcript("../../etc/passwd", tmp_path) is None
    assert session_store.drop_transcript("../../etc/passwd", tmp_path) is None
