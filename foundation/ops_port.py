"""Composition-root seams for the ops foundation (ADR 0030).

Automation (advancement gate, alert sweep, disk hygiene) and the cluster
watchdog (incident recording) consume ops capabilities through these ports so
neither feature imports ``features.ops`` directly — that would close a
cross-feature dependency cycle. The ops feature registers its implementations
at startup via ``features.ops.register_ops_ports``; unconfigured ports fail
open (no incidents recorded, no sweep, advancement not paused).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol


DEFAULT_STAGE_MAX_AGE_DAYS = 7
DEFAULT_UPLOAD_TEMP_MAX_AGE_DAYS = 7

AlertSweep = Callable[[], dict[str, int]]


class IncidentRecorder(Protocol):
    def __call__(
        self,
        *,
        severity: str,
        kind: str,
        message: str,
        subject: str = '',
        dedup_key: str = '',
        next_actions: list[dict[str, Any]] | None = None,
        suppress_minutes: int = 60,
    ) -> dict[str, Any]: ...


class DiskHygiene(Protocol):
    def __call__(
        self,
        *,
        stage_max_age_days: int = DEFAULT_STAGE_MAX_AGE_DAYS,
        upload_temp_max_age_days: int = DEFAULT_UPLOAD_TEMP_MAX_AGE_DAYS,
        include_stage: bool = False,
        dry_run: bool = True,
    ) -> dict[str, Any]: ...


class DiskInventory(Protocol):
    def __call__(self, *, refresh: bool = False) -> dict[str, Any]: ...

_incident_recorder: IncidentRecorder | None = None
_alert_sweep: AlertSweep | None = None
_disk_hygiene: DiskHygiene | None = None
_disk_inventory: DiskInventory | None = None


def configure_incident_recorder(recorder: IncidentRecorder | None) -> None:
    global _incident_recorder
    _incident_recorder = recorder


def configure_alert_sweep(sweep: AlertSweep | None) -> None:
    global _alert_sweep
    _alert_sweep = sweep


def configure_disk_hygiene(hygiene: DiskHygiene | None) -> None:
    global _disk_hygiene
    _disk_hygiene = hygiene


def record_incident(
    *,
    severity: str,
    kind: str,
    message: str,
    subject: str = '',
    dedup_key: str = '',
    next_actions: list[dict[str, Any]] | None = None,
    suppress_minutes: int = 60,
) -> dict[str, Any]:
    """Record an ops incident; no-op until the ops feature registers."""
    if _incident_recorder is None:
        return {"event": None, "suppressed": True}
    return _incident_recorder(
        severity=severity, kind=kind, message=message, subject=subject,
        dedup_key=dedup_key, next_actions=next_actions, suppress_minutes=suppress_minutes,
    )


def run_alert_sweep() -> dict[str, int]:
    if _alert_sweep is None:
        return {"evaluated": 0, "recorded": 0, "notified": 0}
    return _alert_sweep()


def run_disk_hygiene(
    *,
    stage_max_age_days: int = DEFAULT_STAGE_MAX_AGE_DAYS,
    upload_temp_max_age_days: int = DEFAULT_UPLOAD_TEMP_MAX_AGE_DAYS,
    include_stage: bool = False,
    dry_run: bool = True,
) -> dict[str, Any]:
    if _disk_hygiene is None:
        return {"dry_run": False, "freed_bytes": 0, "removed_files": 0}
    return _disk_hygiene(
        stage_max_age_days=stage_max_age_days, upload_temp_max_age_days=upload_temp_max_age_days,
        include_stage=include_stage, dry_run=dry_run,
    )


def configure_disk_inventory(inventory: DiskInventory | None) -> None:
    global _disk_inventory
    _disk_inventory = inventory


def read_disk_inventory(*, refresh: bool = False) -> dict[str, Any]:
    """Read runtime inventory without exposing cleanup operations."""
    if _disk_inventory is None:
        raise RuntimeError("Runtime disk inventory is not configured")
    return _disk_inventory(refresh=refresh)
