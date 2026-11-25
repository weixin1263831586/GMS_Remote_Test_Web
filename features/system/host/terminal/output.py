"""Terminal transport: PTY lifecycle, session cleanup and batched output."""

from __future__ import annotations

import asyncio
import codecs
import fcntl
import logging
import os
import pty
import select
import signal
import struct
import termios
import threading
import time
from collections.abc import Callable
from typing import Any

from starlette.websockets import WebSocketDisconnect

from features.system.host.ssh import ssh_manager

from ...state import global_state


logger = logging.getLogger(__name__)

TERMINAL_READ_SIZE = 16 * 1024
TERMINAL_MAX_BATCH_BYTES = 64 * 1024
TERMINAL_IDLE_SECONDS = 0.02


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
                # pty 子进程按设计直接 exec 用户选择的终端程序：
                # argv 列表、无 shell 拼接，serial/用户名等动态值不经解释器。
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


def _submit_output(websocket, event_loop, payload: dict):
    if event_loop.is_closed() or not event_loop.is_running():
        raise ConnectionError("terminal event loop is unavailable")
    coroutine = websocket.send_json(payload)
    try:
        return asyncio.run_coroutine_threadsafe(coroutine, event_loop)
    except RuntimeError as exc:
        # The loop can close between the availability check and submission.
        coroutine.close()
        raise ConnectionError("terminal event loop closed during output") from exc


def _read_available(channel) -> tuple[bytes, bool]:
    chunks = []
    total = 0
    eof = False
    while total < TERMINAL_MAX_BATCH_BYTES and channel.recv_ready():
        chunk = channel.recv(min(TERMINAL_READ_SIZE, TERMINAL_MAX_BATCH_BYTES - total))
        if not chunk:
            eof = True
            break
        chunks.append(chunk)
        total += len(chunk)
    return b"".join(chunks), eof


def start_terminal_output_pump(
    session_id: str,
    websocket,
    event_loop,
    *,
    thread_name: str,
    encoding_errors: str = "replace",
    validate_session: Callable[[dict[str, Any]], bool] | None = None,
    maintain_session: Callable[[dict[str, Any]], bool] | None = None,
    maintenance_interval: float = 30.0,
    notify_disconnect: bool = False,
) -> threading.Thread:
    """Forward one PTY/SSH channel with bounded batching and idle backoff."""

    def run() -> None:
        decoder = codecs.getincrementaldecoder("utf-8")(errors=encoding_errors)
        next_maintenance = time.monotonic() + maintenance_interval
        ended_by_backend = False
        first_payload_logged = False

        def send_payload(data: str) -> None:
            if not data:
                return
            future = _submit_output(websocket, event_loop, {"type": "terminal_data", "data": data})
            try:
                future.result(timeout=5)
            except BaseException:
                future.cancel()
                raise

        try:
            while True:
                with global_state.terminal_lock:
                    session = global_state.terminal_ssh_sessions.get(session_id)
                if not session:
                    # 会话被外部清理（正常关闭/接管）。仅在外部还保留着
                    # 会话行时才可能走到 notify 分支，这里只留诊断痕迹。
                    logger.info(
                        "[TERMINAL] Output pump exit %s: session gone", session_id
                    )
                    break
                if validate_session and not validate_session(session):
                    ended_by_backend = True
                    logger.warning(
                        "[TERMINAL] Output pump exit %s: device claim revoked",
                        session_id,
                    )
                    break
                if maintain_session and time.monotonic() >= next_maintenance:
                    if not maintain_session(session):
                        ended_by_backend = True
                        logger.warning(
                            "[TERMINAL] Output pump exit %s: claim renew failed",
                            session_id,
                        )
                        break
                    next_maintenance = time.monotonic() + maintenance_interval

                payload, eof = _read_available(session["channel"])
                if payload:
                    # PTY reads may split a UTF-8 code point across batches.
                    # Preserve decoder state instead of replacing/dropping the
                    # character at each arbitrary read boundary.
                    decoded = decoder.decode(payload, final=False)
                    if not first_payload_logged:
                        first_payload_logged = True
                        # ADB 会话排障关键证据：设备/宿主在服务环境里到底回显
                        # 了什么（浏览器侧的启动判定完全依赖这段字节）。
                        if session.get("mode") in {"adb", "local_adb"}:
                            logger.info(
                                "[TERMINAL] First ADB payload %s: %r",
                                session_id,
                                decoded[:240],
                            )
                    send_payload(decoded)
                if eof:
                    send_payload(decoder.decode(b"", final=True))
                    ended_by_backend = True
                    logger.info(
                        "[TERMINAL] Output pump exit %s: channel EOF", session_id
                    )
                    break
                if not payload:
                    time.sleep(TERMINAL_IDLE_SECONDS)
        except (OSError, TimeoutError, WebSocketDisconnect, ConnectionError, KeyError):
            ended_by_backend = True
            logger.info(
                "[TERMINAL] Output pump exit %s: transport error", session_id
            )
        except Exception as exc:
            ended_by_backend = True
            logger.error("[TERMINAL] Output pump failed for %s: %s", session_id, exc)
        finally:
            with global_state.terminal_lock:
                session = global_state.terminal_ssh_sessions.pop(session_id, None)
            if session:
                close_terminal_session_resources(session)
            if notify_disconnect and ended_by_backend and session:
                try:
                    _submit_output(websocket, event_loop, {
                        "type": "terminal_error", "error": "连接已断开",
                    })
                except (WebSocketDisconnect, ConnectionError, KeyError):
                    pass

    thread = threading.Thread(target=run, daemon=True, name=thread_name)
    thread.start()
    return thread
