"""Cluster access seam for the users feature.

Cluster consumes users' identity helpers, so users reaches cluster only
through :mod:`foundation.cluster_port`. This wrapper keeps the users-side
None-on-unavailable semantics for single-host fallback.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from foundation import cluster_port


def configure_cluster_service_provider(
    provider: Callable[[], Any] | None,
) -> None:
    """Register the callable returning the cluster service.

    Passing ``None`` explicitly clears the registration.
    """
    cluster_port.configure_cluster_access(service_provider=provider)


def get_cluster_service() -> Any:
    """Return the cluster service, or ``None`` when unavailable."""
    try:
        return cluster_port.get_cluster_service()
    except (RuntimeError, AttributeError):
        return None
