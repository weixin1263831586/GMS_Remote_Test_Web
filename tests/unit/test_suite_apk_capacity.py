"""Suite APK imports report pool pressure without discarding existing evidence."""

from __future__ import annotations

import json
import threading
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from starlette.requests import Request

from features.auth import CurrentUser
from features.firmware import apk
from features.firmware.apk_store import ApkTaskStore
from features.test_execution import suites_api
from features.test_execution.models import SuiteApkAnalyzeRequest


@pytest.fixture
def suite_import(tmp_path, monkeypatch):
    root = tmp_path / "GMS-Suite"
    tools = root / "android-cts" / "android-cts" / "tools"
    source = tools.parent / "testcases" / "sample.apk"
    source.parent.mkdir(parents=True)
    tools.mkdir()
    source.write_bytes(b"apk-content")
    uploads = tmp_path / "uploads"
    store = ApkTaskStore(uploads / "tasks.sqlite3")
    state = SimpleNamespace(apk_analysis_tasks={}, apk_analysis_tasks_lock=threading.RLock())
    for name, value in {
        "apk_upload_dir": str(uploads), "apk_max_tasks": 1, "global_state": state, "apk_task_store": store,
    }.items():
        monkeypatch.setattr(apk.runtime, name, value)
    for name, value in {
        "config_manager": SimpleNamespace(load_config=lambda: {
            "ubuntu_host": "127.0.0.1", "ubuntu_user": "tester", "suites_path": str(root),
        }),
        "apk_upload_dir": str(uploads), "apk_max_file_size": 1024,
        "normalize_apk_filename": apk.normalize_apk_filename, "safe_join": apk.safe_join,
        "create_apk_task": apk.create_apk_task, "cleanup_files": apk.cleanup_files,
    }.items():
        monkeypatch.setattr(suites_api.runtime, name, value)
    request = Request({"type": "http", "method": "POST", "path": "/api/test/suites/apk/analyze"})
    request.state.current_user = CurrentUser(id="alice", username="alice", role="user")
    body = SuiteApkAnalyzeRequest(suite_path=str(tools), path="testcases/sample.apk")
    return uploads, store, state, source, body, request


def configure_transport(monkeypatch, local, source):
    monkeypatch.setattr(suites_api, "is_config_host_local", lambda _config: local)
    manager = MagicMock()
    monkeypatch.setattr(suites_api.runtime, "ssh_manager", manager)
    if not local:
        monkeypatch.setattr(suites_api, "_run_suite_file_script", lambda *_args: {
            "success": True, "is_apk": True, "is_jar": False, "size": source.stat().st_size,
            "name": source.name, "real_path": str(source),
        })
        sftp = manager.get_connection.return_value.open_sftp.return_value
        sftp.get.side_effect = lambda _remote, target: Path(target).write_bytes(source.read_bytes())
    return manager


@pytest.mark.asyncio
@pytest.mark.parametrize("local", [True, False], ids=["controller", "ssh"])
async def test_full_account_returns_503_and_removes_only_rejected_copy(suite_import, monkeypatch, local):
    uploads, store, state, source, body, request = suite_import
    task_id = str(uuid.uuid4())
    existing = uploads / task_id
    existing.mkdir()
    (existing / "kept.apk").write_bytes(b"retained-evidence")
    task = {"owner_id": "alice", "status": "analyzing", "timestamp": 1}
    state.apk_analysis_tasks[task_id] = task.copy()
    store.upsert(task_id, task)
    manager = configure_transport(monkeypatch, local, source)

    response = await suites_api.create_suite_apk_analysis_task(body, request)

    assert response.status_code == 503
    payload = json.loads(response.body)
    assert payload["code"] == "DEPENDENCY_UNAVAILABLE" and payload["next_actions"]
    assert "容量已满" in payload["error"]
    assert state.apk_analysis_tasks == {task_id: task}
    assert store.list() == {task_id: task}
    assert (existing / "kept.apk").read_bytes() == b"retained-evidence"
    assert sorted(path.name for path in uploads.iterdir() if path.is_dir()) == [task_id]
    assert source.read_bytes() == b"apk-content"
    if local:
        manager.get_connection.assert_not_called()
    else:
        manager.get_connection.return_value.open_sftp.return_value.close.assert_called_once()
        manager.return_connection.assert_called_once_with(manager.get_connection.return_value)


@pytest.mark.asyncio
@pytest.mark.parametrize("local", [True, False], ids=["controller", "ssh"])
async def test_available_pool_still_creates_owner_scoped_task(suite_import, monkeypatch, local):
    uploads, store, state, source, body, request = suite_import
    configure_transport(monkeypatch, local, source)
    response = await suites_api.create_suite_apk_analysis_task(body, request)
    assert response.status_code == 200
    task_id = json.loads(response.body)["data"]["task_id"]
    assert state.apk_analysis_tasks[task_id]["owner_id"] == "alice"
    assert store.get(task_id)["status"] == "uploaded"
    assert (uploads / task_id / "sample.apk").read_bytes() == source.read_bytes()


@pytest.mark.asyncio
@pytest.mark.parametrize("local", [True, False], ids=["controller", "ssh"])
@pytest.mark.parametrize("role", ["admin", "user"])
async def test_other_accounts_do_not_block_admin_or_user_import(suite_import, monkeypatch, local, role):
    uploads, store, state, source, body, request = suite_import
    request.state.current_user = CurrentUser(id="alice", username="alice", role=role)
    other_tasks = {}
    for owner, status in [("bob", "completed"), ("carol", "analyzing")]:
        task_id = str(uuid.uuid4())
        directory = uploads / task_id
        directory.mkdir()
        (directory / "kept.apk").write_bytes(b"retained-evidence")
        task = {"owner_id": owner, "status": status, "timestamp": 1}
        state.apk_analysis_tasks[task_id] = task.copy()
        store.upsert(task_id, task)
        other_tasks[task_id] = task
    configure_transport(monkeypatch, local, source)

    response = await suites_api.create_suite_apk_analysis_task(body, request)

    assert response.status_code == 200, response.body
    new_id = json.loads(response.body)["data"]["task_id"]
    assert state.apk_analysis_tasks[new_id]["owner_id"] == "alice"
    assert store.get(new_id)["owner_id"] == "alice"
    for task_id, task in other_tasks.items():
        assert state.apk_analysis_tasks[task_id] == task
        assert store.get(task_id) == task
        assert (uploads / task_id / "kept.apk").read_bytes() == b"retained-evidence"


@pytest.mark.asyncio
async def test_quota_rotates_only_current_accounts_oldest_task(suite_import, monkeypatch):
    uploads, store, state, source, body, request = suite_import
    tasks = {}
    for owner, timestamp in [("bob", 0), ("alice", 1)]:
        task_id = str(uuid.uuid4())
        directory = uploads / task_id
        directory.mkdir()
        (directory / "kept.apk").write_bytes(b"retained-evidence")
        task = {"owner_id": owner, "status": "completed", "timestamp": timestamp}
        tasks[owner] = task_id
        state.apk_analysis_tasks[task_id] = task.copy()
        store.upsert(task_id, task)
    configure_transport(monkeypatch, True, source)

    response = await suites_api.create_suite_apk_analysis_task(body, request)

    assert response.status_code == 200, response.body
    assert tasks["alice"] not in state.apk_analysis_tasks
    assert store.get(tasks["alice"]) is None
    assert not (uploads / tasks["alice"]).exists()
    assert tasks["bob"] in state.apk_analysis_tasks
    assert store.get(tasks["bob"])["owner_id"] == "bob"
    assert (uploads / tasks["bob"] / "kept.apk").read_bytes() == b"retained-evidence"
