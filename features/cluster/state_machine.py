"""Validated state transitions for durable Cluster Jobs."""

from __future__ import annotations


TERMINAL_JOB_STATUSES = {"completed", "failed", "cancelled"}

_ALLOWED_TRANSITIONS = {
    "assigned": {"dispatching", "stopping", "completed", "failed", "cancelled", "worker_lost"},
    "dispatching": {"running", "stopping", "completed", "failed", "cancelled", "worker_lost"},
    "running": {"stopping", "completed", "failed", "cancelled", "worker_lost"},
    "stopping": {"cancelled", "failed", "worker_lost"},
    # Worker 恢复时仅允许 worker_lost 转回 running，不创建新 Attempt。
    "worker_lost": {"running", "stopping", "completed", "failed", "cancelled"},
    "completed": set(),
    "failed": set(),
    "cancelled": set(),
}


class InvalidJobTransitionError(ValueError):
    pass


def validate_job_transition(from_status: str, to_status: str) -> None:
    if from_status == to_status:
        return
    allowed = _ALLOWED_TRANSITIONS.get(from_status)
    if allowed is None or to_status not in allowed:
        raise InvalidJobTransitionError(
            f"invalid cluster job transition: {from_status or '<empty>'} -> {to_status}"
        )
