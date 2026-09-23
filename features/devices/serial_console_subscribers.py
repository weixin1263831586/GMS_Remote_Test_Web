"""Thread-safe lifecycle signals for serial-console subscribers."""

from __future__ import annotations

import asyncio
import contextlib
import threading
from collections.abc import Iterable
from typing import Any


def _enqueue_close(queue: asyncio.Queue[Any]) -> None:
    while not queue.empty():
        with contextlib.suppress(asyncio.QueueEmpty):
            queue.get_nowait()
    with contextlib.suppress(asyncio.QueueFull):
        queue.put_nowait(None)


def signal_subscriber_close(subscribers: Iterable[Any]) -> None:
    """Wake subscribers with a priority close signal, tolerating stale loops."""

    for subscriber in subscribers:
        with contextlib.suppress(RuntimeError):
            subscriber.loop.call_soon_threadsafe(_enqueue_close, subscriber.queue)


def schedule_idle_stop(service: Any, port_key: str, *, delay: float = 5.0) -> None:
    """Keep the physical port open briefly so a page refresh can reconnect."""

    if service._desired(port_key):
        return

    def stop_if_still_idle() -> None:
        if not service._desired(port_key):
            service._stop_worker(port_key)

    timer = threading.Timer(delay, stop_if_still_idle)
    timer.daemon = True
    timer.start()


__all__ = ["schedule_idle_stop", "signal_subscriber_close"]
