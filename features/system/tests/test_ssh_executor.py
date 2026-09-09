from __future__ import annotations

import asyncio
from collections import deque

from features.system.ssh_executor import SSHExecutor


class _FakeStream:
    def __init__(self, channel):
        self.channel = channel

    def read(self):
        raise AssertionError("SSHExecutor must not sequentially read() one stream to EOF")


class _FakeChannel:
    def __init__(
        self,
        stdout_chunks=(),
        stderr_chunks=(),
        *,
        late_stdout=(),
        late_after_idle_checks=0,
        exit_code=0,
    ):
        self.stdout_chunks = deque(stdout_chunks)
        self.stderr_chunks = deque(stderr_chunks)
        self.late_stdout = deque(late_stdout)
        self.late_after_idle_checks = late_after_idle_checks
        self.idle_stdout_checks = 0
        self.exit_code = exit_code
        self.close_calls = 0

    def recv_ready(self):
        if self.stdout_chunks:
            return True
        if self.late_stdout:
            self.idle_stdout_checks += 1
            if self.idle_stdout_checks > self.late_after_idle_checks:
                self.stdout_chunks.extend(self.late_stdout)
                self.late_stdout.clear()
                return True
        return False

    def recv(self, _size):
        return self.stdout_chunks.popleft() if self.stdout_chunks else b""

    def recv_stderr_ready(self):
        return bool(self.stderr_chunks)

    def recv_stderr(self, _size):
        return self.stderr_chunks.popleft() if self.stderr_chunks else b""

    def exit_status_ready(self):
        return not self.stdout_chunks and not self.stderr_chunks

    def recv_exit_status(self):
        return self.exit_code

    def close(self):
        self.close_calls += 1


class _FakeSSH:
    def __init__(self, channel):
        self.channel = channel

    def exec_command(self, _command, timeout=None, get_pty=False):
        del timeout, get_pty
        stdout = _FakeStream(self.channel)
        stderr = _FakeStream(self.channel)
        return object(), stdout, stderr


def test_run_drains_stdout_and_stderr_without_sequential_stream_reads():
    channel = _FakeChannel(
        stdout_chunks=(b"out-1\n", b"out-2\n"),
        stderr_chunks=(b"err-1\n", b"err-2\n"),
        exit_code=17,
    )

    result = SSHExecutor().run(_FakeSSH(channel), "fake", timeout=1)

    assert result.stdout == "out-1\nout-2\n"
    assert result.stderr == "err-1\nerr-2\n"
    assert result.code == 17


def test_run_stream_keeps_tail_data_that_arrives_after_exit_status():
    async def run_case():
        channel = _FakeChannel(
            stdout_chunks=(b"first\n",),
            stderr_chunks=(b"warn\n",),
            late_stdout=(b"tail-without-newline",),
            late_after_idle_checks=2,
            exit_code=0,
        )
        events = []

        async def log_callback(line, level):
            events.append((line, level))

        result = await SSHExecutor().run_stream(
            _FakeSSH(channel),
            "fake",
            log_callback,
            timeout=1,
        )
        return result, events

    result, events = asyncio.run(run_case())

    assert result.stdout == "first\ntail-without-newline"
    assert result.stderr == "warn"
    assert result.code == 0
    assert ("first", "info") in events
    assert ("tail-without-newline", "info") in events
    assert ("warn", "error") in events


# ---------------------------------------------------------------------------
# R11（2026-09-08 审核）：channel 生命周期、deadline、输出上限与取消
# ---------------------------------------------------------------------------


class _EndlessOutputChannel(_FakeChannel):
    """Simulates a remote command that never exits and keeps producing."""

    def recv_ready(self):
        return True

    def recv(self, _size):
        return b"x" * 1024

    def exit_status_ready(self):
        return False


class _NeverEndingTailChannel(_FakeChannel):
    """Exit status is ready, but a detached remote child trickles output."""

    def __init__(self):
        super().__init__()
        self._available = 1

    def recv_ready(self):
        # 每次轮询之间恰好有一个新 chunk 就绪（模拟慢速派生进程）。
        if self._available == 0:
            self._available = 1
            return False
        return True

    def recv(self, _size):
        self._available -= 1
        return b"late-child-output\n"

    def exit_status_ready(self):
        return True


class _CancelAfterFirstRecv(_FakeChannel):
    def __init__(self):
        super().__init__(stdout_chunks=(b"boom\n",))
        self.recv_calls = 0

    def recv(self, _size):
        self.recv_calls += 1
        return super().recv(_size)


def test_run_closes_channel_on_success():
    channel = _FakeChannel(stdout_chunks=(b"ok\n",))
    result = SSHExecutor().run(_FakeSSH(channel), "fake", timeout=1)
    assert result.code == 0
    assert channel.close_calls == 1


def test_run_closes_channel_on_timeout_and_enforces_deadline_during_drain():
    import time as _time

    channel = _EndlessOutputChannel()
    started = _time.monotonic()
    result = SSHExecutor().run(_FakeSSH(channel), "fake", timeout=1)
    elapsed = _time.monotonic() - started

    assert result.code == -1
    assert "timed out" in result.stderr
    assert channel.close_calls == 1
    # 持续高输出下 recv 分支不休眠；修复前 deadline 只在外层检查，
    # 理论上可以无限运行。这里给它 5 秒上限作为回归约束。
    assert elapsed < 5, f"drain loop ignored the deadline for {elapsed:.1f}s"


def test_run_truncates_output_beyond_capture_limit():
    channel = _FakeChannel(stdout_chunks=(b"hello\n",), stderr_chunks=(b"err\n",))
    executor = SSHExecutor(max_captured_stream_bytes=1)
    result = executor.run(_FakeSSH(channel), "fake", timeout=1)

    assert result.code == 0
    assert result.stdout.endswith("...[GMS: output truncated]")
    assert result.stderr.endswith("...[GMS: output truncated]")


def test_run_gives_up_tailing_when_remote_child_keeps_channel_alive():
    channel = _NeverEndingTailChannel()
    executor = SSHExecutor(exit_tail_drain_seconds=0.0)
    result = executor.run(_FakeSSH(channel), "fake", timeout=5)

    # exit 之后的“尾部”来自派生进程；执行器必须在上限后返回而不是永久 drain。
    assert result.code == 0
    assert channel.close_calls == 1


def test_run_honors_cooperative_cancel_and_closes_channel():
    channel = _CancelAfterFirstRecv()
    result = SSHExecutor().run(
        _FakeSSH(channel), "fake", timeout=5, should_cancel=lambda: channel.recv_calls >= 1
    )

    assert result.code == -1
    assert "cancelled" in result.stderr
    assert channel.close_calls == 1


def test_run_stream_closes_channel_on_timeout():
    async def run_case():
        channel = _EndlessOutputChannel()
        events = []

        async def log_callback(line, level):
            events.append((line, level))

        result = await SSHExecutor().run_stream(
            _FakeSSH(channel), "fake", log_callback, timeout=1
        )
        return result, channel

    result, channel = asyncio.run(run_case())

    assert result.code == -1
    assert "timed out" in result.stderr
    assert channel.close_calls == 1
