"""Access port for automation worker status.

System health and metrics consume scheduler status through this seam so the
system feature does not import the automation feature directly.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


WorkerStatusProvider = Callable[[], dict[str, Any]]

_worker_status_provider: WorkerStatusProvider | None = None


def configure_worker_status_provider(
    provider: WorkerStatusProvider | None,
) -> None:
    """Register or clear the automation worker-status provider."""
    global _worker_status_provider
    _worker_status_provider = provider


def get_worker_status() -> dict[str, Any]:
    """Return scheduler status, or raise when automation is unavailable."""
    if _worker_status_provider is None:
        raise RuntimeError("automation worker status is not configured")
    return _worker_status_provider()
