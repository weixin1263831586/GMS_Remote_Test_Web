"""Batched terminal channel output forwarding."""

from __future__ import annotations

import asyncio
import codecs
import logging
import threading
import time
from collections.abc import Callable
from typing import Any

from starlette.websockets import WebSocketDisconnect

from .state import global_state
from .terminal_channels import close_terminal_session_resources


logger = logging.getLogger(__name__)

TERMINAL_READ_SIZE = 16 * 1024
TERMINAL_MAX_BATCH_BYTES = 64 * 1024
TERMINAL_IDLE_SECONDS = 0.02


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

        def send_payload(data: str) -> None:
            if not data:
                return
            future = asyncio.run_coroutine_threadsafe(
                websocket.send_json({
                    "type": "terminal_data",
                    "data": data,
                }),
                event_loop,
            )
            future.result(timeout=5)

        try:
            while True:
                with global_state.terminal_lock:
                    session = global_state.terminal_ssh_sessions.get(session_id)
                if not session:
                    break
                if validate_session and not validate_session(session):
                    ended_by_backend = True
                    break
                if maintain_session and time.monotonic() >= next_maintenance:
                    if not maintain_session(session):
                        ended_by_backend = True
                        break
                    next_maintenance = time.monotonic() + maintenance_interval

                payload, eof = _read_available(session["channel"])
                if payload:
                    # PTY reads may split a UTF-8 code point across batches.
                    # Preserve decoder state instead of replacing/dropping the
                    # character at each arbitrary read boundary.
                    send_payload(decoder.decode(payload, final=False))
                if eof:
                    send_payload(decoder.decode(b"", final=True))
                    ended_by_backend = True
                    break
                if not payload:
                    time.sleep(TERMINAL_IDLE_SECONDS)
        except (OSError, TimeoutError, WebSocketDisconnect, ConnectionError, KeyError):
            ended_by_backend = True
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
                    asyncio.run_coroutine_threadsafe(
                        websocket.send_json({
                            "type": "terminal_error",
                            "error": "连接已断开",
                        }),
                        event_loop,
                    )
                except (WebSocketDisconnect, ConnectionError, KeyError):
                    pass

    thread = threading.Thread(target=run, daemon=True, name=thread_name)
    thread.start()
    return thread
