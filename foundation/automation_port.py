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


AdvancementGate = Callable[[], bool]

_advancement_gate: AdvancementGate | None = None


def configure_advancement_gate(gate: AdvancementGate | None) -> None:
    """Register or clear the ops kill-switch gate (ADR 0030)."""
    global _advancement_gate
    _advancement_gate = gate


def advancement_paused() -> bool:
    """Whether the ops kill-switch pauses run advancement.

    Fail-open: an unconfigured (or failing) gate never wedges the pipeline.
    """
    if _advancement_gate is None:
        return False
    return bool(_advancement_gate())


ActiveRunIdsProvider = Callable[[], set[str]]

_active_run_ids_provider: ActiveRunIdsProvider | None = None


def configure_active_run_ids_provider(
    provider: ActiveRunIdsProvider | None,
) -> None:
    """Register or clear the non-terminal automation run-id provider."""
    global _active_run_ids_provider
    _active_run_ids_provider = provider


def active_run_ids() -> set[str] | None:
    """Run IDs still queued/in-flight; ``None`` when the lookup fails.

    Ops disk hygiene consumes this to skip staged firmware a live run may
    replay (fail-closed). An unconfigured provider also returns ``None`` so
    cleanup never guesses "nothing is referenced".
    """
    if _active_run_ids_provider is None:
        return None
    try:
        return _active_run_ids_provider()
    except Exception:
        return None
