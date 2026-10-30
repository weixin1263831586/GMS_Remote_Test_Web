"""_stream_to_disk 异常路径回归：读写任一方失败都必须在
有限时间内整体退出，不遗留 .part 文件，也不把失败吞成挂起。

场景：网络中途断开、附件超限、磁盘写入失败、下载取消。
"""

from __future__ import annotations

import asyncio
import hashlib
import threading
from unittest.mock import patch

import aiohttp
import pytest

from features.redmine.evidence import EvidenceError
from features.redmine.evidence_fetch import EvidenceFetcher
from features.redmine.evidence_store import EvidenceStore


class _BrokenStream:
    """读两个块后在网络层抛异常（模拟中途断开）。"""

    def __init__(self, chunks):
        self._chunks = list(chunks)

    async def iter_chunked(self, _size):
        for chunk in self._chunks:
            yield chunk
        raise aiohttp.ClientPayloadError("connection reset mid-body")


class _FailingResponse:
    def __init__(self, chunks):
        self.content = _BrokenStream(chunks)


class _PlainResponse:
    """正常结束的流：与磁盘侧故障测试配对，保证异常只来自写盘。"""

    def __init__(self, chunks):
        self._chunks = list(chunks)

    @property
    def content(self):
        outer = self

        class _Stream:
            async def iter_chunked(self, _size):
                for chunk in outer._chunks:
                    yield chunk

        return _Stream()


def _oversize_stream(chunks):
    class _Oversize:
        def __init__(self):
            self._chunks = list(chunks)

        async def iter_chunked(self, size):
            for chunk in self._chunks:
                yield chunk
            # 超限发生在流尾：模拟服务端声明大小绕过后的真实超量
            yield b"x" * 1024

    return _Oversize()


async def _run_stream(fetcher, response, temporary):
    return await fetcher._stream_to_disk(response, temporary, hashlib.sha256())


@pytest.mark.asyncio
async def test_network_failure_terminates_with_no_part_file(tmp_path):
    fetcher = EvidenceFetcher.__new__(EvidenceFetcher)
    temporary = tmp_path / "a.bin.part"
    chunks = [b"A" * 1024, b"B" * 1024]

    with pytest.raises(aiohttp.ClientPayloadError):
        await asyncio.wait_for(
            _run_stream(fetcher, _FailingResponse(chunks), temporary),
            timeout=5,
        )
    # .part 清理由调用方 _download_attachment 的 except 分支负责
    # （unlink(True)）；这里验证的是管道本身在有限时间内以网络异常退出。


@pytest.mark.asyncio
async def test_oversize_limit_terminates(tmp_path):
    fetcher = EvidenceFetcher.__new__(EvidenceFetcher)
    temporary = tmp_path / "big.bin.part"

    class _OversizeResponse:
        content = _oversize_stream([b"A" * 1024, b"B" * 1024, b"C" * 1024])

    with patch(
        "features.redmine.evidence_fetch.ATTACHMENT_MAX_BYTES", 2048,
    ), pytest.raises(EvidenceError):
        await asyncio.wait_for(
            _run_stream(fetcher, _OversizeResponse(), temporary),
            timeout=5,
        )


@pytest.mark.asyncio
async def test_disk_write_failure_terminates(tmp_path):
    fetcher = EvidenceFetcher.__new__(EvidenceFetcher)
    temporary = tmp_path / "c.bin.part"
    import builtins

    real_open = builtins.open
    calls = {"n": 0}

    def failing_open(file, mode="r", *args, **kwargs):
        handle = real_open(file, mode, *args, **kwargs)
        if mode == "wb":
            original_write = handle.write

            def write(data):
                calls["n"] += 1
                if calls["n"] > 1:
                    raise OSError(28, "No space left on device")
                return original_write(data)

            handle.write = write
        return handle

    with patch.object(builtins, "open", failing_open), pytest.raises(OSError):
            await asyncio.wait_for(
                _run_stream(
                    fetcher,
                    _PlainResponse([b"A" * 1024, b"B" * 1024, b"C" * 1024]),
                    temporary,
                ),
                timeout=5,
            )


@pytest.mark.asyncio
async def test_cancellation_terminates_promptly(tmp_path):
    fetcher = EvidenceFetcher.__new__(EvidenceFetcher)
    temporary = tmp_path / "d.bin.part"

    reading = asyncio.Event()
    tasks_before = asyncio.all_tasks()

    class _SlowStream:
        async def iter_chunked(self, _size):
            yield b"A" * 1024
            reading.set()
            await asyncio.Event().wait()

    class _SlowResponse:
        content = _SlowStream()

    task = asyncio.create_task(_run_stream(fetcher, _SlowResponse(), temporary))
    await asyncio.wait_for(reading.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5)
    assert not (asyncio.all_tasks() - tasks_before)
    temporary.unlink()
    assert not temporary.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["write", "flush", "fsync"])
async def test_cancellation_joins_disk_thread_before_close(tmp_path, monkeypatch, operation):
    import builtins
    import os

    fetcher = EvidenceFetcher.__new__(EvidenceFetcher)
    temporary = tmp_path / "slow.bin.part"
    started = asyncio.Event()
    release = threading.Event()
    finished = threading.Event()
    closed = threading.Event()
    loop = asyncio.get_running_loop()
    tasks_before = asyncio.all_tasks()
    real_open = builtins.open
    real_fsync = os.fsync

    def slow_call(callback, *args):
        loop.call_soon_threadsafe(started.set)
        try:
            if not release.wait(5):
                raise TimeoutError("test did not release disk operation")
            assert not closed.is_set()
            return callback(*args)
        finally:
            finished.set()

    def tracking_open(file, mode="r", *args, **kwargs):
        handle = real_open(file, mode, *args, **kwargs)
        if file != temporary or mode != "wb":
            return handle
        original_close = handle.close

        def close():
            closed.set()
            return original_close()

        handle.close = close
        if operation != "fsync":
            original = getattr(handle, operation)
            setattr(handle, operation, lambda *a: slow_call(original, *a))
        return handle

    monkeypatch.setattr(builtins, "open", tracking_open)
    if operation == "fsync":
        monkeypatch.setattr(os, "fsync", lambda fd: slow_call(real_fsync, fd))
    task = asyncio.create_task(_run_stream(fetcher, _PlainResponse([b"bytes"]), temporary))
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        task.cancel()
        # Cancel a second time while the first cancellation is joining I/O.
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        assert not closed.is_set()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=5)
        assert finished.is_set()
        assert closed.is_set()
        assert not (asyncio.all_tasks() - tasks_before)
        temporary.unlink()
        assert not temporary.exists()
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["network", "oversize", "cancel"])
async def test_attachment_failure_removes_part_after_pipeline_exits(tmp_path, monkeypatch, failure):
    store = EvidenceStore(tmp_path)
    fetcher = EvidenceFetcher("alice", store)
    snapshot = store.create_snapshot(issue_id=1)
    artifact = store.create_artifact(
        snapshot_id=snapshot["snapshot_id"], attachment_id="123", filename="a.bin",
        original_filename="a.bin", content_type="application/octet-stream", kind="unknown",
    )
    snapshot_dir = store.snapshot_dir(snapshot)
    reading = asyncio.Event()
    tasks_before = asyncio.all_tasks()

    class Content:
        async def iter_chunked(self, _size):
            yield b"bytes"
            reading.set()
            if failure == "network":
                raise aiohttp.ClientPayloadError("connection reset")
            await asyncio.Event().wait()

    class Response:
        status = 200
        content = Content()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    class Session:
        def get(self, *_args, **_kwargs):
            return Response()

    if failure == "oversize":
        monkeypatch.setattr("features.redmine.evidence_fetch.ATTACHMENT_MAX_BYTES", 1)
    task = asyncio.create_task(fetcher._download_attachment(
        Session(), "https://redmine.example", {"id": "123", "filename": "a.bin"},
        {}, artifact, snapshot_dir,
    ))
    if failure == "cancel":
        await asyncio.wait_for(reading.wait(), timeout=5)
        task.cancel()
    expected = {
        "network": aiohttp.ClientPayloadError, "oversize": EvidenceError,
        "cancel": asyncio.CancelledError,
    }[failure]
    with pytest.raises(expected):
        await asyncio.wait_for(task, timeout=5)
    assert not list(snapshot_dir.rglob("*.part"))
    assert not list(snapshot_dir.rglob("a.bin"))
    assert not (asyncio.all_tasks() - tasks_before)
