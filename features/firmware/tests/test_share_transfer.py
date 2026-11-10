"""Exercise real Paramiko readv with an in-memory SFTP response queue."""

import math
import threading
from collections import deque
from contextlib import contextmanager
from types import SimpleNamespace

import paramiko
import pytest
from paramiko.message import Message
from paramiko.sftp import CMD_CLOSE, CMD_DATA, CMD_READ

from features.firmware import shares_api as api


class QueuedSftp:
    def __init__(self, payload, start, length):
        self.payload = payload
        self.remaining = length
        self.end = start + length
        self.pending = deque()
        self.condition = threading.Condition()
        self.request_count = 0
        self.peak_pending = 0
        self.batch_remaining = 0
        self.timeout = None
        self.sock = SimpleNamespace(settimeout=self.settimeout)

    def settimeout(self, timeout):
        self.timeout = timeout

    def normalize(self, path):
        return "/firmware" if path == "." else path

    def open(self, path, mode):
        assert path == "/firmware/update.img"
        return paramiko.SFTPFile(self, b"image", mode)

    def _log(self, *args):
        pass

    def _request(self, command, *args):
        assert command == CMD_CLOSE, "sequential network reads must use the pipeline"
        return None, None

    def _async_request(self, file, command, handle, offset, length):
        assert command == CMD_READ
        assert offset + length <= self.end
        with self.condition:
            self.request_count += 1
            self.pending.append((file, self.request_count, offset, length))
            self.peak_pending = max(self.peak_pending, len(self.pending))
            self.condition.notify_all()
            return self.request_count

    def _read_response(self):
        with self.condition:
            if not self.batch_remaining:
                # A slow peer can hold responses until this window is issued.
                self.batch_remaining = math.ceil(min(self.remaining, 1024**2) / 32768)
                assert self.condition.wait_for(lambda: len(self.pending) == self.batch_remaining, timeout=2)
            file, number, offset, length = self.pending.popleft()
            self.batch_remaining -= 1
            self.remaining -= length
        response = Message()
        response.add_string(self.payload[offset:offset + length])
        file._async_response(CMD_DATA, Message(response.asbytes()), number)


@pytest.mark.parametrize("start,length", [(0, 3 * 1024**2 + 17), (17, 1024**2 + 51), (111, 13)])
def test_pipeline_is_bounded_and_preserves_exact_full_or_range_content(monkeypatch, start, length):
    payload = bytes(range(256)) * (math.ceil((start + length) / 256) + 1)
    sftp = QueuedSftp(payload, start, length)

    @contextmanager
    def client(*args, **kwargs):
        yield sftp, {}

    monkeypatch.setattr(api, "_sftp_client", client)
    chunks = list(api._remote_file_iterator("source", "user", "/firmware/update.img", {}, start, length))
    assert b"".join(chunks) == payload[start:start + length]
    assert max(map(len, chunks)) <= 1024**2
    assert 1 <= sftp.peak_pending <= 32
    if length >= 1024**2:
        assert sftp.peak_pending > 1
    assert sftp.timeout == 30


def test_closing_a_download_early_closes_the_remote_handle(monkeypatch):
    sftp = QueuedSftp(b"f" * 2 * 1024**2, 0, 2 * 1024**2)
    remote = sftp.open("/firmware/update.img", "rb")
    monkeypatch.setattr(sftp, "open", lambda *args: remote)

    @contextmanager
    def client(*args, **kwargs):
        yield sftp, {}

    monkeypatch.setattr(api, "_sftp_client", client)
    stream = api._remote_file_iterator("source", "user", "/firmware/update.img", {}, 0, 2 * 1024**2)
    assert len(next(stream)) == 1024**2
    stream.close()
    assert remote.closed
    assert not sftp.pending
