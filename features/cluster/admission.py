"""Worker admission decisions: one source of truth for UI and scheduler.

``ClusterService.list_workers`` (UI/API directory), ``select_worker``
(scheduler), the device-claim re-check in ``ClusterRepository`` and the
automation manual-worker path all consume ``worker_admission_state`` so they
can never disagree about whether a Worker may take a new job. Before this
module the UI only blocked on offline/draining while the scheduler silently
rejected the same Worker on the disk threshold, and the memory floor was a
UI-only warning the scheduler never applied.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any


def min_disk_free_gb() -> float:
    """Cluster-wide free-disk admission floor (GB)."""
    return float(os.getenv("GMS_CLUSTER_MIN_DISK_FREE_GB", "50"))


def min_memory_available_gb() -> float:
    """Cluster-wide available-memory admission floor (GB)."""
    return float(os.getenv("GMS_CLUSTER_MIN_MEMORY_AVAILABLE_GB", "8"))


def _positive_metric(value: Any) -> float:
    """Coerce a Worker metric; 0/invalid means "unknown" and never blocks."""
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        return 0.0
    return number if number > 0 else 0.0


def worker_admission_state(
    worker: Mapping[str, Any], *, required_memory_gb: float = 0.0
) -> dict[str, Any]:
    """Return ``{"blocked": bool, "reasons": [...]}`` for one Worker.

    Reason vocabulary (stable; consumed by UI labels and error mapping):
    ``offline``, ``draining``, ``low_disk``, ``low_memory``, ``max_jobs``,
    ``external_tradefed``. Statuses other than online/busy fail closed as
    ``offline`` (or ``draining``). Metrics reported as 0/absent are unknown
    and never block on their own; ``max_jobs <= 0`` counts as a single slot.
    """
    reasons: list[str] = []
    status = str(worker.get("status") or "")
    if status == "draining":
        reasons.append("draining")
    elif status not in {"online", "busy"}:
        reasons.append("offline")

    disk_free = _positive_metric(worker.get("disk_free_gb"))
    if disk_free and disk_free < min_disk_free_gb():
        reasons.append("low_disk")

    memory_available = _positive_metric(worker.get("memory_available_gb"))
    required_memory = max(
        _positive_metric(required_memory_gb), min_memory_available_gb()
    )
    if memory_available and required_memory and memory_available < required_memory:
        reasons.append("low_memory")

    max_jobs = int(_positive_metric(worker.get("max_jobs")) or 0)
    running_jobs = int(_positive_metric(worker.get("running_jobs")) or 0)
    if running_jobs >= max(max_jobs, 1):
        reasons.append("max_jobs")

    if int(_positive_metric(worker.get("unknown_external_jobs")) or 0):
        reasons.append("external_tradefed")

    return {"blocked": bool(reasons), "reasons": reasons}
