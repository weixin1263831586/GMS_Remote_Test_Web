"""Worker availability transition events shared by repository operations."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from foundation.events import EVENT_WORKER_AVAILABILITY_CHANGED, event_bus


def emit_worker_availability(worker: Mapping[str, Any], status: str) -> None:
    """Broadcast a real Worker online/offline transition to UI subscribers."""
    event_bus.emit(EVENT_WORKER_AVAILABILITY_CHANGED, {
        "worker_id": str(worker["id"]),
        "name": str(worker["name"] or ""),
        "status": status,
    })
