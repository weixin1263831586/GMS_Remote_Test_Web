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
from typing import Any, Protocol, TypedDict


DEFAULT_STAGE_MAX_AGE_DAYS = 7
DEFAULT_UPLOAD_TEMP_MAX_AGE_DAYS = 7


class OpsEvent(TypedDict):
    id: str
    created_at: str
    severity: str
    kind: str
    subject: str
    dedup_key: str
    message: str
    next_actions_json: str
    status: str
    acknowledged_at: str
    acknowledged_by: str
    notified_at: str
    notify_state: str
    notify_attempts: int
    notify_attempted_at: str
    notify_retry_at: str


class IncidentResult(TypedDict):
    event: OpsEvent | None
    suppressed: bool


class AlertSweepResult(TypedDict):
    evaluated: int
    recorded: int
    notified: int


class DirectorySize(TypedDict):
    path: str
    size_gb: float


class _StageCandidateFields(DirectorySize):
    age_days: float
    run_id: str
    deletable: bool


class StageCandidate(_StageCandidateFields, total=False):
    protection_reason: str


class DiskCleanupPreview(TypedDict):
    disk_free_gb: float | None
    candidates: list[StageCandidate]
    candidate_count: int
    candidate_size_gb: float
    deletable_count: int
    protected_count: int
    largest_dirs: list[DirectorySize]


class _DiskCleanupCounts(TypedDict):
    dry_run: bool
    freed_bytes: int
    removed_files: int


class DiskCleanupResult(_DiskCleanupCounts, total=False):
    include_stage: bool
    disk_free_gb: float | None
    candidates: list[StageCandidate]
    candidate_count: int
    candidate_size_gb: float
    deletable_count: int
    protected_count: int
    largest_dirs: list[DirectorySize]


class ArtifactCounters(TypedDict):
    bytes: int
    files: int
    directories: int
    aged_bytes: int
    aged_files: int
    older_7d_bytes: int
    older_30d_bytes: int
    older_90d_bytes: int
    newest: float | None
    present: bool
    complete: bool
    note: str


class ArtifactBreakdown(TypedDict):
    type: str
    bytes: int
    files: int


class _ArtifactItemFields(ArtifactCounters):
    id: str
    name: str
    group: str
    scope: str
    location: str
    cleanup: str
    priority: str
    age_days: int
    description: str
    status: str


class ArtifactItem(_ArtifactItemFields, total=False):
    breakdown: list[ArtifactBreakdown]


class FilesystemUsage(TypedDict):
    total_bytes: int | None
    free_bytes: int | None
    used_bytes: int | None


class DiskInventorySummary(TypedDict):
    measured_bytes: int
    controller_data_bytes: int
    local_worker_bytes: int
    aged_bytes: int
    scanned_categories: int
    incomplete_categories: int
    attention_categories: int
    visited_entries: int
    scan_seconds: float
    controller_scan_complete: bool
    disk: FilesystemUsage


class DiskInventoryResult(TypedDict):
    generated_at: str
    scope: str
    readonly: bool
    cache_hit: bool
    summary: DiskInventorySummary
    items: list[ArtifactItem]
    coverage: list[str]


AlertSweep = Callable[[], AlertSweepResult]


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
    ) -> IncidentResult: ...


class DiskHygiene(Protocol):
    def __call__(
        self,
        *,
        stage_max_age_days: int = DEFAULT_STAGE_MAX_AGE_DAYS,
        upload_temp_max_age_days: int = DEFAULT_UPLOAD_TEMP_MAX_AGE_DAYS,
        include_stage: bool = False,
        dry_run: bool = True,
    ) -> DiskCleanupResult: ...


class DiskInventory(Protocol):
    def __call__(self, *, refresh: bool = False) -> DiskInventoryResult: ...

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
) -> IncidentResult:
    """Record an ops incident; no-op until the ops feature registers."""
    if _incident_recorder is None:
        return {"event": None, "suppressed": True}
    return _incident_recorder(
        severity=severity, kind=kind, message=message, subject=subject,
        dedup_key=dedup_key, next_actions=next_actions, suppress_minutes=suppress_minutes,
    )


def run_alert_sweep() -> AlertSweepResult:
    if _alert_sweep is None:
        return {"evaluated": 0, "recorded": 0, "notified": 0}
    return _alert_sweep()


def run_disk_hygiene(
    *,
    stage_max_age_days: int = DEFAULT_STAGE_MAX_AGE_DAYS,
    upload_temp_max_age_days: int = DEFAULT_UPLOAD_TEMP_MAX_AGE_DAYS,
    include_stage: bool = False,
    dry_run: bool = True,
) -> DiskCleanupResult:
    if _disk_hygiene is None:
        return {"dry_run": dry_run, "freed_bytes": 0, "removed_files": 0}
    return _disk_hygiene(
        stage_max_age_days=stage_max_age_days, upload_temp_max_age_days=upload_temp_max_age_days,
        include_stage=include_stage, dry_run=dry_run,
    )


def configure_disk_inventory(inventory: DiskInventory | None) -> None:
    global _disk_inventory
    _disk_inventory = inventory


def read_disk_inventory(*, refresh: bool = False) -> DiskInventoryResult:
    """Read runtime inventory without exposing cleanup operations."""
    if _disk_inventory is None:
        raise RuntimeError("Runtime disk inventory is not configured")
    return _disk_inventory(refresh=refresh)
