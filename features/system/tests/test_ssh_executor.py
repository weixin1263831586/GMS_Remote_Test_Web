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
