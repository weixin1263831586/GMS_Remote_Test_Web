"""kkagent 子进程基础设施：进程树清理、输出截断读取、环境隔离。"""

from __future__ import annotations

import asyncio
import os
import signal
from typing import Any


# kkagent 子进程不得继承宿主进程的 Agent 身份环境：多 owner 分析时，
# 泄漏的 GMS_RT_PROFILE / token 路径会让 MCP 以错误 owner（或 gms 服务
# 账号）取证。身份只能经 env_extra 显式注入（见 DailyBriefService）。
# GMS_MCP_TOOLSETS 同理：toolset 收敛由晨报侧显式注入（evidence-only），
# 继承宿主的全量 toolset 会放大 Prompt Injection 影响面。
MCP_IDENTITY_ENV_KEYS = frozenset({
    "GMS_RT_PROFILE",
    "GMS_AGENT_PROFILE",
    "GMS_AGENT_CLIENT",
    "GMS_REMOTE_TEST_SERVER",
    "GMS_AUTH_TOKEN_FILE",
    "GMS_AGENT_AUTH_MODE",
    "GMS_MCP_TOOLSETS",
})

PROCESS_STOP_GRACE_SECONDS = 5.0

# 子进程输出捕获上限：流式读取，固定保留头部 + 尾部，中间部分丢弃，
# 避免冗长日志把 stdout/stderr 全量吃进内存后才截断。
CAPTURE_HEAD_BYTES = 16 * 1024
CAPTURE_TAIL_BYTES = 256 * 1024

# stream-json 单行上限：GMS MCP 单次 tool_result 输出上限为 1 MiB，
# NDJSON 的单行必须能容纳它（64KB 的 StreamReader 默认上限会直接崩溃）。
STREAM_LINE_LIMIT_BYTES = 4 * 1024 * 1024


async def terminate_process_tree(process: asyncio.subprocess.Process) -> None:
    """TERM→grace→KILL 并回收独立进程组。"""
    if process.returncode is not None:
        return
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    else:  # pragma: no cover - Windows fallback
        process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=PROCESS_STOP_GRACE_SECONDS)
        return
    except asyncio.TimeoutError:
        pass
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    else:  # pragma: no cover - Windows fallback
        process.kill()
    await process.wait()


async def settle_reader_future(reader: asyncio.Future[Any]) -> None:
    """回收 communicate/gather，避免其异常在 event loop 关闭后泄漏。"""
    if not reader.done():
        # 子进程已在调用前收敛；此时取消仅剩的 pipe reader
        # 比无界等待更稳健，也不会遗留孤儿子进程。
        reader.cancel()
    try:
        await reader
    except asyncio.CancelledError:
        pass
    except Exception:
        pass


class CappedCapture:
    """流式累积文本输出，内存占用上限约 head+tail 字节。"""

    def __init__(self, head: int = CAPTURE_HEAD_BYTES, tail: int = CAPTURE_TAIL_BYTES):
        self._head = head
        self._tail = tail
        self._head_buf = bytearray()
        self._tail_buf = bytearray()

    def feed(self, chunk: bytes | str) -> None:
        data = chunk.encode("utf-8", errors="replace") if isinstance(chunk, str) else chunk
        if len(self._head_buf) < self._head:
            take = min(self._head - len(self._head_buf), len(data))
            self._head_buf.extend(data[:take])
            data = data[take:]
        if data:
            self._tail_buf.extend(data)
            if len(self._tail_buf) > self._tail:
                del self._tail_buf[: len(self._tail_buf) - self._tail]

    def text(self) -> str:
        truncated = len(self._tail_buf) >= self._tail
        parts = [bytes(self._head_buf)]
        if truncated and self._tail_buf:
            parts.append(b"\n...[truncated]...\n")
        parts.append(bytes(self._tail_buf))
        return b"".join(parts).decode("utf-8", errors="replace")


async def read_stream_capped(
    stream: asyncio.StreamReader,
    head: int = CAPTURE_HEAD_BYTES,
    tail: int = CAPTURE_TAIL_BYTES,
) -> bytes:
    """读取子进程输出到 EOF，内存占用上限约 head+tail 字节。"""
    capture = CappedCapture(head, tail)
    while True:
        chunk = await stream.read(65536)
        if not chunk:
            break
        capture.feed(chunk)
    return capture.text().encode("utf-8", errors="replace")


def child_env(env_extra: dict[str, str]) -> dict[str, str]:
    """kkagent 子进程环境：剥离继承的 Agent 身份，再显式注入目标身份。"""
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in MCP_IDENTITY_ENV_KEYS
    }
    env.update(env_extra)
    env.setdefault("NO_COLOR", "1")
    return env


__all__ = [
    "CAPTURE_HEAD_BYTES",
    "CAPTURE_TAIL_BYTES",
    "MCP_IDENTITY_ENV_KEYS",
    "PROCESS_STOP_GRACE_SECONDS",
    "STREAM_LINE_LIMIT_BYTES",
    "CappedCapture",
    "child_env",
    "read_stream_capped",
    "settle_reader_future",
    "terminate_process_tree",
]
