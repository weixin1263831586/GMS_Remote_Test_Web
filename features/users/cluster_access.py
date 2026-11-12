"""Cluster access seam for the users feature.

Cluster consumes users' identity helpers, so users reaches cluster only
through :mod:`foundation.cluster_port`. This wrapper keeps the users-side
None-on-unavailable semantics for single-host fallback.
"""

from __future__ import annotations

from typing import Any

from foundation import cluster_port


def get_cluster_service() -> Any:
    """Return the cluster service, or ``None`` when unavailable."""
    try:
        return cluster_port.get_cluster_service()
    except (RuntimeError, AttributeError):
        return None
