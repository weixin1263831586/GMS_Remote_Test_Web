from __future__ import annotations

import asyncio
import shutil
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from features.firmware import apk, runtime
from features.firmware.apk_store import ApkTaskStore


def _task(root, task_id: str, owner_id: str = "alice"):
    task_root = root / task_id
    task_root.mkdir()
    apk_path = task_root / "app.apk"
    apk_path.write_bytes(b"apk")
    return {
        "status": "analyzing",
        "progress": 10,
        "apk_path": str(apk_path),
        "output_dir": str(task_root / "jadx_output"),
        "filename": "app.apk",
        "timestamp": 1,
        "error": None,
        "owner_id": owner_id,
    }


def test_apk_task_metadata_survives_store_restart(tmp_path):
    path = tmp_path / "tasks.sqlite3"
    task_id = "00000000-0000-0000-0000-000000000001"
    first = ApkTaskStore(path)
    first.upsert(task_id, _task(tmp_path, task_id))

    restarted = ApkTaskStore(path)

    assert restarted.get(task_id)["owner_id"] == "alice"
    assert restarted.list()[task_id]["status"] == "analyzing"


def test_apk_store_recovers_after_runtime_data_directory_deletion(tmp_path):
    data_dir = tmp_path / "apk_uploads"
    store = ApkTaskStore(data_dir / "tasks.sqlite3")

    shutil.rmtree(data_dir)

    assert store.list() == {}


def test_interrupted_analysis_is_rescheduled_only_from_valid_task_paths(tmp_path, monkeypatch):
    valid_id = "00000000-0000-0000-0000-000000000002"
    invalid_id = "00000000-0000-0000-0000-000000000003"
    valid = _task(tmp_path, valid_id)
    invalid = {
        **_task(tmp_path, invalid_id),
        "apk_path": "/etc/passwd",
    }
    state = SimpleNamespace(
        apk_analysis_tasks={valid_id: valid, invalid_id: invalid},
        apk_analysis_tasks_lock=threading.RLock(),
        background_tasks=set(),
    )
    store = ApkTaskStore(tmp_path / "tasks.sqlite3")
    store.upsert(valid_id, valid)
    store.upsert(invalid_id, invalid)
    monkeypatch.setattr(runtime, "global_state", state)
    monkeypatch.setattr(runtime, "apk_task_store", store)
    monkeypatch.setattr(runtime, "apk_upload_dir", str(tmp_path))

    async def exercise():
        runner = AsyncMock(return_value=None)
        with patch("features.firmware.apk._run_jadx_analysis", new=runner):
            recovered = apk.recover_apk_analysis_tasks()
            await asyncio.gather(*recovered)
        return runner, recovered

    runner, recovered = asyncio.run(exercise())

    assert len(recovered) == 1
    runner.assert_awaited_once()
    assert state.apk_analysis_tasks[invalid_id]["status"] == "error"
    assert store.get(invalid_id)["status"] == "error"


def test_resolve_jadx_path_prefers_config_override(monkeypatch):
    class Manager:
        @staticmethod
        def load_config():
            return {"jadx_path": "/opt/jadx-wrapper/bin/jadx"}

    monkeypatch.setattr(apk.runtime, "config_manager", Manager())
    monkeypatch.setattr(apk.runtime, "jadx_path", "/fallback/jadx")
    assert apk._resolve_jadx_path() == "/opt/jadx-wrapper/bin/jadx"


def test_resolve_jadx_path_falls_back_without_config(monkeypatch):
    monkeypatch.setattr(apk.runtime, "config_manager", None)
    monkeypatch.setattr(apk.runtime, "jadx_path", "/fallback/jadx")
    assert apk._resolve_jadx_path() == "/fallback/jadx"


def test_resolve_jadx_path_ignores_broken_config_manager(monkeypatch):
    class BrokenManager:
        @staticmethod
        def load_config():
            raise RuntimeError("config unavailable")

    monkeypatch.setattr(apk.runtime, "config_manager", BrokenManager())
    monkeypatch.setattr(apk.runtime, "jadx_path", "/fallback/jadx")
    assert apk._resolve_jadx_path() == "/fallback/jadx"
