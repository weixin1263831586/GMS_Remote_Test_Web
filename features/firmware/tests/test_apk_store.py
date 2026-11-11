from __future__ import annotations

import asyncio
import os
import shutil
import threading
import time
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from features.firmware import apk, runtime
from features.firmware.apk_store import ORPHAN_UPLOAD_COOLDOWN_SECONDS, ApkTaskStore


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
        with patch("features.firmware.apk.run_jadx_analysis", new=runner):
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

    monkeypatch.setattr(apk.runtime.get_runtime(), "config_manager", Manager())
    monkeypatch.setattr(apk.runtime.get_runtime(), "jadx_path", "/fallback/jadx")
    assert apk._resolve_jadx_path() == "/opt/jadx-wrapper/bin/jadx"


def test_resolve_jadx_path_falls_back_without_config(monkeypatch):
    monkeypatch.setattr(apk.runtime.get_runtime(), "config_manager", None)
    monkeypatch.setattr(apk.runtime.get_runtime(), "jadx_path", "/fallback/jadx")
    assert apk._resolve_jadx_path() == "/fallback/jadx"


def test_resolve_jadx_path_ignores_broken_config_manager(monkeypatch):
    class BrokenManager:
        @staticmethod
        def load_config():
            raise RuntimeError("config unavailable")

    monkeypatch.setattr(apk.runtime.get_runtime(), "config_manager", BrokenManager())
    monkeypatch.setattr(apk.runtime.get_runtime(), "jadx_path", "/fallback/jadx")
    assert apk._resolve_jadx_path() == "/fallback/jadx"


def _old_dir(path, *, age_seconds: float) -> None:
    path.mkdir(parents=True, exist_ok=True)
    old = time.time() - age_seconds
    os.utime(path, (old, old))


def test_reconcile_removes_orphan_dirs_without_task_row(tmp_path):
    """无任务行、无上传会话、超冷却期的目录被清理。"""
    store = ApkTaskStore(tmp_path / "tasks.sqlite3")
    orphan = uuid.uuid4().hex
    kept_task = "00000000-0000-0000-0000-000000000010"
    task_row = _task(tmp_path, kept_task)
    _old_dir(tmp_path / kept_task, age_seconds=ORPHAN_UPLOAD_COOLDOWN_SECONDS * 2)
    store.upsert(kept_task, task_row)
    _old_dir(tmp_path / orphan, age_seconds=ORPHAN_UPLOAD_COOLDOWN_SECONDS * 2)

    cleaned = store.reconcile_orphan_uploads()

    assert cleaned == [orphan]
    assert not (tmp_path / orphan).exists()
    assert (tmp_path / kept_task).exists()


def test_reconcile_keeps_active_upload_sessions_and_fresh_dirs(tmp_path):
    """含分块会话（metadata/.part）的目录按续传 TTL 判定，未过期保留。"""
    store = ApkTaskStore(tmp_path / "tasks.sqlite3")
    resumable = uuid.uuid4().hex
    (tmp_path / resumable).mkdir()
    (tmp_path / resumable / "upload_metadata.json").write_text("{}")
    fresh_orphan = uuid.uuid4().hex
    _old_dir(tmp_path / fresh_orphan, age_seconds=60)  # 冷却期内
    chunks = uuid.uuid4().hex
    (tmp_path / chunks).mkdir()
    (tmp_path / chunks / "app.apk.part0").write_bytes(b"x")

    cleaned = store.reconcile_orphan_uploads()

    assert cleaned == []
    assert (tmp_path / resumable).exists()
    assert (tmp_path / fresh_orphan).exists()
    assert (tmp_path / chunks).exists()


def test_reconcile_removes_expired_upload_session(tmp_path):
    """放弃的分块会话（超续传 TTL）必须能回收，而不是永久排除。"""
    from features.firmware.apk_store import UPLOAD_SESSION_TTL_SECONDS

    store = ApkTaskStore(tmp_path / "tasks.sqlite3")
    abandoned = uuid.uuid4().hex
    (tmp_path / abandoned).mkdir()
    (tmp_path / abandoned / "upload_metadata.json").write_text("{}")
    (tmp_path / abandoned / "app.apk.part0").write_bytes(b"x" * 10)
    # 先写内容再回拨时间：写文件会刷新目录 mtime；会话内全部文件
    # 都计入"最后活动时间"，需一并回拨。
    old = time.time() - UPLOAD_SESSION_TTL_SECONDS - 3600
    for item in (tmp_path / abandoned).iterdir():
        os.utime(item, (old, old))
    os.utime(tmp_path / abandoned, (old, old))

    cleaned = store.reconcile_orphan_uploads()

    assert cleaned == [abandoned]
    assert not (tmp_path / abandoned).exists()


def test_reconcile_failed_rmtree_not_counted_as_cleaned(tmp_path, monkeypatch):
    """rmtree 失败（权限/占用）不得计入 cleaned——如实报告而非虚报。"""
    store = ApkTaskStore(tmp_path / "tasks.sqlite3")
    orphan = uuid.uuid4().hex
    _old_dir(tmp_path / orphan, age_seconds=ORPHAN_UPLOAD_COOLDOWN_SECONDS * 2)

    real_rmtree = __import__("shutil").rmtree

    def failing_rmtree(path, *args, **kwargs):
        real_rmtree(path, *args, **kwargs)  # 先真删，模拟竞态后仍失败的场景反例：
        raise OSError(13, "Permission denied")

    monkeypatch.setattr(
        "features.firmware.apk_store.shutil.rmtree", failing_rmtree
    )

    cleaned = store.reconcile_orphan_uploads()

    assert cleaned == []


def test_reconcile_ignores_non_uuid_and_files(tmp_path):
    """非 UUID 名目录与普通文件不碰（tasks.sqlite3 等同住一个根）。"""
    store = ApkTaskStore(tmp_path / "tasks.sqlite3")
    foreign = tmp_path / "not-a-uuid-dir"
    _old_dir(foreign, age_seconds=ORPHAN_UPLOAD_COOLDOWN_SECONDS * 2)
    plain = tmp_path / "tasks.sqlite3"

    assert store.reconcile_orphan_uploads() == []
    assert foreign.exists()
    assert plain.exists()


def _analysis_state(tmp_path, task_id: str):
    task = {
        "status": "analyzing",
        "progress": 10,
        "apk_path": str(tmp_path / "app.apk"),
        "output_dir": None,
        "filename": "app.apk",
        "timestamp": 1,
        "error": None,
        "owner_id": "alice",
    }
    state = SimpleNamespace(
        apk_analysis_tasks={task_id: task},
        apk_analysis_tasks_lock=threading.RLock(),
    )
    return state, task


class _FakeJadxProcess:
    def __init__(self, returncode: int, stderr: bytes = b""):
        self.returncode = returncode
        self._stderr = stderr

    async def communicate(self):
        return (b"", self._stderr)


def _run_analysis(task_id, output_dir, fake_process, monkeypatch, tmp_path, seed=None):
    """驱动 run_jadx_analysis；seed 在 jadx 启动时机执行，模拟其产物写入。

    run_jadx_analysis 先清空临时目录再启动子进程，因此模拟 jadx 输出的
    夹具必须在 create_subprocess_exec 被调用时创建，而不是预先写好。
    """
    state, task = _analysis_state(tmp_path, task_id)
    monkeypatch.setattr(runtime, "global_state", state)
    monkeypatch.setattr(runtime, "apk_task_store", None)
    monkeypatch.setattr(runtime, "jadx_timeout", 10)
    monkeypatch.setattr(runtime, "config_manager", None)
    monkeypatch.setattr(runtime, "jadx_path", "jadx")

    async def fake_exec(*args, **kwargs):
        if seed is not None:
            seed()
        return fake_process

    async def exercise():
        with patch("asyncio.create_subprocess_exec", new=fake_exec):
            await apk.run_jadx_analysis(
                task_id, str(tmp_path / "app.apk"), output_dir
            )

    asyncio.run(exercise())
    return task


def test_jadx_partial_errors_with_sources_still_complete(tmp_path, monkeypatch):
    """rc=3（finished with errors）但本次临时目录已有源码 → completed+partial。

    GtsBackupAgentTestApp.apk 实测：jadx 1.5.6 simple 模式 19/2897 个方法
    代码生成失败，其余 4178 个 .java 与 AndroidManifest.xml 均正常输出，
    不能整体判死；同时必须标记 partial 并带回退出码与错误计数。
    """
    task_id = "00000000-0000-0000-0000-000000000020"
    output_dir = tmp_path / "out"
    tmp_output = tmp_path / "out.tmp"  # run_jadx_analysis 实际写入的目录

    def seed_jadx_output():
        (tmp_output / "sources" / "a").mkdir(parents=True)
        (tmp_output / "sources" / "a" / "B.java").write_text("class B {}")

    task = _run_analysis(
        task_id,
        str(output_dir),
        _FakeJadxProcess(returncode=3, stderr=b"ERROR - finished with errors, count: 19"),
        monkeypatch,
        tmp_path,
        seed=seed_jadx_output,
    )

    assert task["status"] == "completed"
    assert task["progress"] == 100
    assert task["output_dir"] == str(output_dir)
    assert task["partial"] is True
    assert task["jadx_exit_code"] == 3
    assert task["jadx_error_count"] == 19
    # 临时目录已原子切换为正式目录
    assert (output_dir / "sources" / "a" / "B.java").is_file()
    assert not tmp_output.exists()


def test_jadx_stale_output_does_not_masquerade_as_success(tmp_path, monkeypatch):
    """上次残留的 .java + 本次立即失败（临时目录无源码）→ error，不得冒充成功。"""
    task_id = "00000000-0000-0000-0000-000000000022"
    output_dir = tmp_path / "out"
    stale = output_dir / "sources" / "old"
    stale.mkdir(parents=True)
    (stale / "Stale.java").write_text("class Stale {}")

    task = _run_analysis(
        task_id,
        str(output_dir),
        _FakeJadxProcess(returncode=1, stderr=b"ERROR - failed to load input"),
        monkeypatch,
        tmp_path,
    )

    assert task["status"] == "error"
    assert task["partial"] is False
    assert "failed to load input" in task["error"]


def test_jadx_success_replaces_stale_output(tmp_path, monkeypatch):
    """成功运行用本次产物整体替换旧输出，旧残留文件不得留存。"""
    task_id = "00000000-0000-0000-0000-000000000023"
    output_dir = tmp_path / "out"
    stale = output_dir / "sources" / "old"
    stale.mkdir(parents=True)
    (stale / "Stale.java").write_text("class Stale {}")
    tmp_output = tmp_path / "out.tmp"

    def seed_fresh_output():
        fresh = tmp_output / "sources" / "new"
        fresh.mkdir(parents=True)
        (fresh / "Fresh.java").write_text("class Fresh {}")

    task = _run_analysis(
        task_id,
        str(output_dir),
        _FakeJadxProcess(returncode=0, stderr=b""),
        monkeypatch,
        tmp_path,
        seed=seed_fresh_output,
    )

    assert task["status"] == "completed"
    assert task["partial"] is False
    assert task["jadx_exit_code"] == 0
    assert (output_dir / "sources" / "new" / "Fresh.java").is_file()
    assert not (output_dir / "sources" / "old").exists()
    assert not tmp_output.exists()


def test_jadx_failure_without_sources_marks_error(tmp_path, monkeypatch):
    """完全无源码产出（如输入损坏）→ 保持 error 且带回 stderr 尾部。"""
    task_id = "00000000-0000-0000-0000-000000000021"
    output_dir = tmp_path / "out"
    output_dir.mkdir()

    task = _run_analysis(
        task_id,
        str(output_dir),
        _FakeJadxProcess(returncode=1, stderr=b"ERROR - failed to load input"),
        monkeypatch,
        tmp_path,
    )

    assert task["status"] == "error"
    assert task["partial"] is False
    assert task["jadx_exit_code"] == 1
    assert "failed to load input" in task["error"]
