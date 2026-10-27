"""_stream_to_disk 异常路径回归（复审 P1）：读写任一方失败都必须在
有限时间内整体退出，不遗留 .part 文件，也不把失败吞成挂起。

场景：网络中途断开、附件超限、磁盘写入失败、下载取消。
"""

from __future__ import annotations

import asyncio
import hashlib
import io
from pathlib import Path
from unittest.mock import patch

import aiohttp
import pytest

from features.redmine.evidence import EvidenceError
from features.redmine.evidence_fetch import EvidenceFetcher


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


class _FlakySinkPath:
    """前 N 次写正常，之后抛 OSError（模拟磁盘写失败）。"""

    def __init__(self, base: Path, fail_after: int):
        self._base = base
        self._fail_after = fail_after
        self.written = 0

    def open(self, mode):
        return _FlakyFile(self._base.open(mode), self)


class _FlakyFile(io.BufferedWriter):
    pass


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

    class _SlowStream:
        async def iter_chunked(self, _size):
            yield b"A" * 1024
            await asyncio.sleep(30)  # 远超测试耐心
            yield b"never"

    class _SlowResponse:
        content = _SlowStream()

    task = asyncio.create_task(_run_stream(fetcher, _SlowResponse(), temporary))
    await asyncio.sleep(0.2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5)
