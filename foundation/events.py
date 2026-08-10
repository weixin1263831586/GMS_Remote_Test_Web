"""In-process event bus for broadcasting resource state changes to WebSocket clients.

The bus is a thin pub/sub layer: backend components call ``event_bus.emit()``
when state changes (worker heartbeat, job transition, device lock change) and
registered listeners forward those events to connected browsers via the existing
``safe_websocket_send`` mechanism.

Design goals:
- Zero external dependencies (no Redis, no asyncio queues).
- Thread-safe (called from FastAPI sync endpoints and background threads).
- Non-blocking: emit never raises even if a listener fails.
- Opt-in: polling remains as a fallback for WS stall/disconnect scenarios.
"""

from __future__ import annotations

import logging
import threading
from collections import defaultdict
from collections.abc import Callable
from typing import Any


logger = logging.getLogger(__name__)

# Well-known event types emitted by the bus.
EVENT_WORKER_UPDATED = "worker.updated"
EVENT_DEVICE_LOCK_CHANGED = "device_lock.changed"
EVENT_JOB_TRANSITION = "job.transition"

Listener = Callable[[str, dict[str, Any]], None]


class EventBus:
    """Thread-safe in-process pub/sub for resource events."""

    def __init__(self) -> None:
        self._listeners: dict[str, list[Listener]] = defaultdict(list)
        self._lock = threading.Lock()

    def subscribe(self, event_type: str, listener: Listener) -> None:
        """Register *listener* to be called when ``event_type`` is emitted.

        Use ``"*"`` to receive every event regardless of type.
        """
        with self._lock:
            self._listeners[event_type].append(listener)

    def emit(self, event_type: str, payload: dict[str, Any] | None = None) -> None:
        """Broadcast *event_type* with *payload* to all matching listeners.

        Listeners that raise are logged and swallowed so one failing callback
        cannot block the emitter.
        """
        with self._lock:
            listeners = list(self._listeners.get(event_type, []))
            listeners.extend(self._listeners.get("*", []))
        for listener in listeners:
            try:
                listener(event_type, payload or {})
            except Exception:
                logger.debug(
                    "event listener for %s raised", event_type, exc_info=True
                )


# Singleton bus used across the application.
event_bus = EventBus()
