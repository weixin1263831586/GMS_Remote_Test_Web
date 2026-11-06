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
from typing import Any


IncidentRecorder = Callable[..., dict[str, Any]]
AlertSweep = Callable[..., dict[str, int]]
DiskHygiene = Callable[..., dict[str, Any]]

_incident_recorder: IncidentRecorder | None = None
_alert_sweep: AlertSweep | None = None
_disk_hygiene: DiskHygiene | None = None


def configure_incident_recorder(recorder: IncidentRecorder | None) -> None:
    global _incident_recorder
    _incident_recorder = recorder


def configure_alert_sweep(sweep: AlertSweep | None) -> None:
    global _alert_sweep
    _alert_sweep = sweep


def configure_disk_hygiene(hygiene: DiskHygiene | None) -> None:
    global _disk_hygiene
    _disk_hygiene = hygiene


def record_incident(**kwargs: Any) -> dict[str, Any]:
    """Record an ops incident; no-op until the ops feature registers."""
    if _incident_recorder is None:
        return {"event": None, "suppressed": True}
    return _incident_recorder(**kwargs)


def run_alert_sweep() -> dict[str, int]:
    if _alert_sweep is None:
        return {"evaluated": 0, "recorded": 0, "notified": 0}
    return _alert_sweep()


def run_disk_hygiene(**kwargs: Any) -> dict[str, Any]:
    if _disk_hygiene is None:
        return {"dry_run": False, "freed_bytes": 0, "removed_files": 0}
    return _disk_hygiene(**kwargs)
