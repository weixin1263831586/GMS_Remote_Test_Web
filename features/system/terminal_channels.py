"""PTY channel and resource lifecycle primitives for terminal sessions."""

from __future__ import annotations

import fcntl
import logging
import os
import pty
import select
import signal
import struct
import termios
import time
from typing import Any

from features.system.ssh import ssh_manager


logger = logging.getLogger(__name__)


class LocalPtyChannel:
    """Minimal Paramiko-like PTY channel for local terminal sessions."""

    def __init__(
        self,
        command: list[str],
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ):
        self.command = command
        self.cwd = cwd or os.path.expanduser("~")
        self.env = env or os.environ.copy()
        self.pid, self.fd = pty.fork()
        self.closed = False
        self._reaped = False

        if self.pid == 0:
            try:
                os.chdir(self.cwd)
                os.execvpe(command[0], command, self.env)
            except Exception as exc:
                os.write(
                    2,
                    f"Failed to start local terminal: {exc}\n".encode(
                        "utf-8", errors="ignore"
                    ),
                )
                os._exit(127)

        os.set_blocking(self.fd, False)

    def recv_ready(self) -> bool:
        if self.closed:
            return False
        readable, _, _ = select.select([self.fd], [], [], 0)
        return bool(readable)

    def recv(self, size: int) -> bytes:
        if self.closed:
            return b""
        try:
            return os.read(self.fd, size)
        except BlockingIOError:
            return b""
        except OSError:
            self.closed = True
            return b""

    def send(self, data: str | bytes) -> int:
        if self.closed:
            return 0
        if isinstance(data, str):
            data = data.encode("utf-8", errors="ignore")
        return os.write(self.fd, data)

    def resize_pty(self, width: int = 120, height: int = 30) -> None:
        if self.closed:
            return
        packed = struct.pack("HHHH", height, width, 0, 0)
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ, packed)

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            os.close(self.fd)
        except OSError:
            pass
        self._terminate_child()

    def _terminate_child(self) -> None:
        """Terminate and reap the PTY child so terminals do not become zombies."""
        if self._reaped:
            return
        for sig, grace_seconds in (
            (signal.SIGHUP, 0.2),
            (signal.SIGTERM, 0.5),
            (signal.SIGKILL, 0.0),
        ):
            try:
                os.kill(self.pid, sig)
            except ProcessLookupError:
                self._reap_child(block=False)
                return
            except OSError:
                self._reap_child(block=False)
                return
            deadline = time.monotonic() + grace_seconds
            while True:
                if self._reap_child(block=False):
                    return
                if grace_seconds <= 0 or time.monotonic() >= deadline:
                    break
                time.sleep(0.02)
        self._reap_child(block=True)

    def _reap_child(self, *, block: bool) -> bool:
        try:
            waited_pid, _status = os.waitpid(
                self.pid, 0 if block else os.WNOHANG
            )
        except ChildProcessError:
            self._reaped = True
            return True
        except OSError:
            return False
        if waited_pid == self.pid:
            self._reaped = True
            return True
        return False


def close_terminal_session_resources(session_info: dict[str, Any]) -> None:
    mode = session_info.get("mode")
    channel = session_info.get("channel")
    ssh = session_info.get("ssh")
    try:
        if channel and mode in {"local", "local_adb", "adb"}:
            channel.close()
    except Exception:
        pass
    try:
        if mode == "adb" and ssh:
            ssh_manager.return_connection(ssh)
        elif ssh:
            ssh.close()
    except Exception:
        pass
    claim_registry = session_info.get("claim_registry")
    claim_source_id = str(session_info.get("claim_source_id") or "")
    if claim_registry is not None and claim_source_id:
        try:
            claim_registry.release(claim_source_id, status="released")
        except Exception:
            logger.exception(
                "Failed to release terminal device claim %s", claim_source_id
            )
