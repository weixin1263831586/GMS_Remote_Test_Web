import asyncio
import json
import os
import subprocess
import threading
import time
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from fastapi import UploadFile

from features.firmware import chunk_uploads


def _state():
    return SimpleNamespace(
        firmware_upload_progress={},
        firmware_upload_progress_lock=threading.RLock(),
    )


def _chunk_form(content: bytes, **overrides):
    values = {
        "upload_id": "upload-1",
        "file_name": "update.img",
        "file_size": "8",
        "chunk_size": "4",
        "total_chunks": "2",
        "chunk_index": "0",
        "stage_only": "1",
        "file": UploadFile(file=BytesIO(content), filename="update.img"),
    }
    values.update(overrides)
    return values


def _payload(response):
    return json.loads(response.body.decode("utf-8"))


def test_staged_upload_is_resumable_and_owner_scoped():
    state = _state()
    with TemporaryDirectory() as root:
        first_response, merged = asyncio.run(
            chunk_uploads.handle_chunk_upload(
                _chunk_form(b"1234"), "alice", root, state, 3600
            )
        )
        final_response, merged = asyncio.run(
            chunk_uploads.handle_chunk_upload(
                _chunk_form(b"5678", chunk_index="1"),
                "alice",
                root,
                state,
                3600,
            )
        )

        assert _payload(first_response)["upload_complete"] is False
        final = _payload(final_response)
        assert final["upload_complete"] is True
        assert final["staged"] is True
        assert merged is None
        assert state.firmware_upload_progress == {}

        check_response, _ = asyncio.run(
            chunk_uploads.handle_chunk_upload(
                _chunk_form(
                    b"",
                    check_chunks="1",
                    file=None,
                    chunk_index=None,
                ),
                "alice",
                root,
                state,
                3600,
            )
        )
        assert _payload(check_response)["staged"] is True
        staged, error = chunk_uploads.load_staged_upload(root, "alice", "upload-1")
        assert error is None
        assert Path(staged["path"]).read_bytes() == b"12345678"
        assert Path(staged["path"]).name == "staged-update.img"
        other_staged, other_error = chunk_uploads.load_staged_upload(
            root, "bob", "upload-1"
        )
        assert other_staged is None
        assert "not found" in other_error.lower()


def test_short_chunk_is_rejected_without_publishing_partial_file():
    state = _state()
    with TemporaryDirectory() as root:
        response, merged = asyncio.run(
            chunk_uploads.handle_chunk_upload(
                _chunk_form(b"12"), "alice", root, state, 3600
            )
        )

        assert response.status_code == 400
        assert "size mismatch" in _payload(response)["error"].lower()
        assert merged is None
        session = Path(chunk_uploads.upload_session_dir(root, "alice", "upload-1"))
        assert not (session / "chunk_00000").exists()
        assert not list(session.glob("*.upload"))


def test_chunk_size_is_required_to_bound_total_session_storage():
    state = _state()
    with TemporaryDirectory() as root:
        response, merged = asyncio.run(
            chunk_uploads.handle_chunk_upload(
                _chunk_form(b"1234", chunk_size=""),
                "alice",
                root,
                state,
                3600,
            )
        )

        assert response.status_code == 400
        assert "chunk_size" in _payload(response)["error"]
        assert merged is None


def test_retry_after_staging_is_idempotent_and_does_not_recreate_chunks():
    state = _state()
    with TemporaryDirectory() as root:
        asyncio.run(
            chunk_uploads.handle_chunk_upload(
                _chunk_form(b"1234"), "alice", root, state, 3600
            )
        )
        asyncio.run(
            chunk_uploads.handle_chunk_upload(
                _chunk_form(b"5678", chunk_index="1"),
                "alice",
                root,
                state,
                3600,
            )
        )

        response, merged = asyncio.run(
            chunk_uploads.handle_chunk_upload(
                _chunk_form(b"1234", chunk_index="0"),
                "alice",
                root,
                state,
                3600,
            )
        )

        assert _payload(response)["staged"] is True
        assert merged is None
        session = Path(chunk_uploads.upload_session_dir(root, "alice", "upload-1"))
        assert not list(session.glob("chunk_*"))


def test_content_fingerprint_prevents_stale_resume_for_same_file_metadata():
    state = _state()
    first_fingerprint = "a" * 64
    second_fingerprint = "b" * 64
    with TemporaryDirectory() as root:
        first_response, _ = asyncio.run(
            chunk_uploads.handle_chunk_upload(
                _chunk_form(b"1234", content_fingerprint=first_fingerprint),
                "alice",
                root,
                state,
                3600,
            )
        )
        conflicting_response, merged = asyncio.run(
            chunk_uploads.handle_chunk_upload(
                _chunk_form(
                    b"abcd",
                    content_fingerprint=second_fingerprint,
                    chunk_index="0",
                ),
                "alice",
                root,
                state,
                3600,
            )
        )

        assert first_response.status_code == 200
        assert conflicting_response.status_code == 400
        assert "metadata does not match" in _payload(conflicting_response)["error"].lower()
        assert merged is None


def test_burn_lock_prevents_parallel_finalize_and_can_be_released():
    with TemporaryDirectory() as root:
        session = Path(chunk_uploads.upload_session_dir(root, "alice", "upload-1"))
        session.mkdir(parents=True)
        staged = {"session_dir": str(session)}

        first = chunk_uploads.acquire_burn_lock(staged)
        second = chunk_uploads.acquire_burn_lock(staged)
        assert first
        assert second is None

        chunk_uploads.release_burn_lock(first)
        assert chunk_uploads.acquire_burn_lock(staged)


def test_burn_lock_records_live_holder_pid():
    with TemporaryDirectory() as root:
        session = Path(chunk_uploads.upload_session_dir(root, "alice", "upload-2"))
        session.mkdir(parents=True)
        staged = {"session_dir": str(session)}

        lock = chunk_uploads.acquire_burn_lock(staged)
        try:
            holder = chunk_uploads._read_lock_holder(lock)
            assert holder == os.getpid()
            # 存活持有者：即便 mtime 被人为拨到 TTL 之外也不可抢占。
            stale_time = time.time() - chunk_uploads.MERGE_LOCK_STALE_SECONDS - 10
            os.utime(lock, (stale_time, stale_time))
            assert chunk_uploads.acquire_burn_lock(staged) is None
        finally:
            chunk_uploads.release_burn_lock(lock)


def test_orphan_burn_lock_from_dead_process_is_stolen_immediately():
    with TemporaryDirectory() as root:
        session = Path(chunk_uploads.upload_session_dir(root, "alice", "upload-3"))
        session.mkdir(parents=True)
        staged = {"session_dir": str(session)}

        # 模拟烧写中服务崩溃/重启：锁文件带一个已死 PID（fresh mtime）。
        dead_pid = _spawn_exiting_process()
        lock_path = Path(session) / ".burn.lock"
        lock_path.write_text(str(dead_pid), encoding="ascii")

        stolen = chunk_uploads.acquire_burn_lock(staged)
        try:
            assert stolen
            assert chunk_uploads._read_lock_holder(stolen) == os.getpid()
        finally:
            chunk_uploads.release_burn_lock(stolen)


def test_stale_empty_legacy_lock_expires_via_ttl():
    with TemporaryDirectory() as root:
        session = Path(chunk_uploads.upload_session_dir(root, "alice", "upload-4"))
        session.mkdir(parents=True)
        staged = {"session_dir": str(session)}

        # 旧格式空锁（无 PID）：沿用 mtime TTL 兜底。
        lock_path = Path(session) / ".burn.lock"
        lock_path.write_text("", encoding="ascii")
        assert chunk_uploads.acquire_burn_lock(staged) is None

        stale_time = time.time() - chunk_uploads.MERGE_LOCK_STALE_SECONDS - 10
        os.utime(lock_path, (stale_time, stale_time))
        reclaimed = chunk_uploads.acquire_burn_lock(staged)
        try:
            assert reclaimed
        finally:
            chunk_uploads.release_burn_lock(reclaimed)


def test_refresh_burn_lock_renew_mtime():
    with TemporaryDirectory() as root:
        session = Path(chunk_uploads.upload_session_dir(root, "alice", "upload-5"))
        session.mkdir(parents=True)
        staged = {"session_dir": str(session)}

        lock = chunk_uploads.acquire_burn_lock(staged)
        try:
            stale_time = time.time() - chunk_uploads.MERGE_LOCK_STALE_SECONDS - 10
            os.utime(lock, (stale_time, stale_time))
            chunk_uploads.refresh_burn_lock(lock)
            assert time.time() - os.path.getmtime(lock) < 60
        finally:
            chunk_uploads.release_burn_lock(lock)


def _spawn_exiting_process() -> int:
    """启动一个立即退出的子进程，返回一个确定已死的 PID。"""
    proc = subprocess.Popen(["true"])
    proc.wait()
    return proc.pid


def test_upload_tokens_do_not_collide_after_sanitizing():
    assert chunk_uploads.safe_upload_token('client/a') != chunk_uploads.safe_upload_token('client_a')
    assert chunk_uploads.safe_upload_token('x' * 121 + 'a') != chunk_uploads.safe_upload_token('x' * 121 + 'b')


def test_expired_upload_sessions_are_removed():
    with TemporaryDirectory() as root:
        expired = Path(chunk_uploads.upload_session_dir(root, 'client', 'old'))
        expired.mkdir(parents=True)
        (expired / 'update.img').write_bytes(b'firmware')
        old = time.time() - 2
        os.utime(expired, (old, old))

        chunk_uploads.cleanup_expired_upload_sessions(root, 'client', max_age=1)

        assert not expired.exists()


def test_orphan_cleanup_removes_stale_sessions_across_clients():
    """全租户兜底：其他 client 的过期暂存会话也要被回收。"""
    with TemporaryDirectory() as root:
        stale = Path(chunk_uploads.upload_session_dir(root, 'client-a', 'old'))
        stale.mkdir(parents=True)
        (stale / 'staged-update.img').write_bytes(b'firmware')
        old = time.time() - 7200
        os.utime(stale, (old, old))
        fresh = Path(chunk_uploads.upload_session_dir(root, 'client-b', 'new'))
        fresh.mkdir(parents=True)
        (fresh / 'upload_metadata.json').write_text('{}', encoding='utf-8')

        removed = chunk_uploads.cleanup_orphan_upload_sessions(root, max_age=3600)

        assert removed == 1
        assert not stale.exists()
        assert fresh.exists()


def test_orphan_cleanup_skips_live_burn_lock_holder():
    """finalize 烧写进行中（锁持有者存活）的会话不得被 janitor 回收。"""
    with TemporaryDirectory() as root:
        session = Path(chunk_uploads.upload_session_dir(root, 'client', 'burning'))
        session.mkdir(parents=True)
        (session / 'staged-update.img').write_bytes(b'firmware')
        (session / '.burn.lock').write_text(str(os.getpid()), encoding='ascii')
        old = time.time() - 7200
        os.utime(session, (old, old))

        removed = chunk_uploads.cleanup_orphan_upload_sessions(root, max_age=3600)

        assert removed == 0
        assert session.exists()


def test_orphan_cleanup_reclaims_dead_holder_lock_session():
    """锁持有者已死（服务重启/崩溃）的旧会话必须能被回收。"""
    with TemporaryDirectory() as root:
        session = Path(chunk_uploads.upload_session_dir(root, 'client', 'dead'))
        session.mkdir(parents=True)
        (session / 'staged-update.img').write_bytes(b'firmware')
        (session / '.burn.lock').write_text(str(_spawn_exiting_process()), encoding='ascii')
        old = time.time() - 7200
        os.utime(session, (old, old))

        removed = chunk_uploads.cleanup_orphan_upload_sessions(root, max_age=3600)

        assert removed == 1
        assert not session.exists()


def test_maybe_cleanup_rate_limits_repeated_calls():
    with TemporaryDirectory() as root:
        stale = Path(chunk_uploads.upload_session_dir(root, 'client', 'old'))
        stale.mkdir(parents=True)
        old = time.time() - 7200
        os.utime(stale, (old, old))

        first = chunk_uploads.maybe_cleanup_orphan_upload_sessions(
            root, 3600, min_interval=0
        )
        assert first == 1
        stale_again = Path(chunk_uploads.upload_session_dir(root, 'client', 'old2'))
        stale_again.mkdir(parents=True)
        os.utime(stale_again, (old, old))

        # 同一限频窗口内（1 小时）重复调用不扫描。
        second = chunk_uploads.maybe_cleanup_orphan_upload_sessions(
            root, 3600, min_interval=3600
        )
        assert second == 0
        assert stale_again.exists()
