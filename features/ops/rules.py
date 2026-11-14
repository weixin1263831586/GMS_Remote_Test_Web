"""Alert rule engine: evaluate a maintenance snapshot into incidents.

Pure functions only — ``evaluate(snapshot)`` returns a list of incident
descriptions without touching storage or the network. The automation
maintenance tick assembles the snapshot and persists/notify the results via
``features.ops.service.record_incident`` and ``features.ops.notify``.

Snapshot keys (all optional; missing keys simply skip their rules):
- ``workers``: ``features.cluster.ClusterService.list_workers()`` output
- ``recent_runs``: latest automation runs (list of dicts with
  ``profile_id``/``status``/``id``)
- ``disk_free_gb`` / ``min_free_gb``: controller data-root pressure
- ``worker_status``: ``features.automation.get_worker_status()`` output
"""

from __future__ import annotations

from typing import Any


# Automation stage failures that indicate the unattended pipeline needs a
# human (or a policy change) when they repeat consecutively on one profile.
CONSECUTIVE_FAILURE_STAGES = frozenset({
    "jenkins_failed", "artifact_missing", "flash_failed", "aoa_failed",
    "test_failed", "analysis_failed", "reporting_failed", "failed",
})

DEFAULT_CONSECUTIVE_FAILURE_THRESHOLD = 3
DEFAULT_MIN_FREE_GB = 20.0


def evaluate(
    snapshot: dict[str, Any],
    *,
    consecutive_failure_threshold: int = DEFAULT_CONSECUTIVE_FAILURE_THRESHOLD,
    min_free_gb: float = DEFAULT_MIN_FREE_GB,
) -> list[dict[str, Any]]:
    incidents: list[dict[str, Any]] = []
    incidents.extend(_offline_worker_incidents(snapshot.get("workers") or []))
    incidents.extend(
        _consecutive_failure_incidents(
            snapshot.get("recent_runs") or [],
            threshold=consecutive_failure_threshold,
        )
    )
    incidents.extend(
        _disk_pressure_incident(
            snapshot.get("disk_free_gb"), min_free_gb=min_free_gb
        )
    )
    incidents.extend(_worker_loop_incident(snapshot.get("worker_status")))
    return incidents


def _offline_worker_incidents(workers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    incidents = []
    for worker in workers:
        if str(worker.get("status") or "") != "offline":
            continue
        worker_id = str(worker.get("id") or "unknown")
        reasons = worker.get("admission_reasons") or worker.get("warnings") or []
        suffix = f"；{'；'.join(str(item) for item in reasons[:3])}" if reasons else ""
        incidents.append({
            "severity": "warning",
            "kind": "worker_offline",
            "subject": worker_id,
            "dedup_key": f"worker_offline:{worker_id}",
            "message": f"Worker {worker_id} 已离线{suffix}",
            "next_actions": [
                {"action": "检查 Worker 进程与心跳", "command": "systemctl status gms-worker"},
            ],
        })
    return incidents


def _consecutive_failure_incidents(
    runs: list[dict[str, Any]], *, threshold: int
) -> list[dict[str, Any]]:
    if threshold <= 0:
        return []
    # Group by profile, keep the most recent ordering as delivered.
    streaks: dict[str, list[dict[str, Any]]] = {}
    for run in runs:
        profile = str(run.get("profile_id") or "-")
        streaks.setdefault(profile, []).append(run)
    incidents = []
    for profile, profile_runs in streaks.items():
        count = 0
        failed_stage = ""
        for run in profile_runs:
            status = str(run.get("status") or "")
            if status in CONSECUTIVE_FAILURE_STAGES:
                count += 1
                failed_stage = failed_stage or status
            else:
                break  # a non-failure run breaks the consecutive streak
        if count < threshold:
            continue
        incidents.append({
            "severity": "critical",
            "kind": "consecutive_failures",
            "subject": profile,
            "dedup_key": f"consecutive_failures:{profile}:{failed_stage}",
            "message": (
                f"Profile {profile} 连续 {count} 次运行失败（最近阶段: {failed_stage}）"
            ),
            "next_actions": [
                {"action": "查看最近失败运行", "command": "/automation?tab=runs"},
                {"action": "检查事件诊断", "command": "/automation?tab=events"},
            ],
        })
    return incidents


def _disk_pressure_incident(
    disk_free_gb: Any, *, min_free_gb: float
) -> list[dict[str, Any]]:
    if disk_free_gb is None or min_free_gb <= 0:
        return []
    try:
        free = float(disk_free_gb)
    except (TypeError, ValueError):
        return []
    if free >= min_free_gb:
        return []
    return [{
        "severity": "critical",
        "kind": "disk_pressure",
        "subject": "controller-data-root",
        "dedup_key": "disk_pressure:controller",
        "message": f"数据目录磁盘仅剩 {free:.1f} GB（阈值 {min_free_gb:.1f} GB），烧写/报告可能静默失败",
        "next_actions": [
            {"action": "清理可重建文件", "command": "/automation?tab=ops"},
            {"action": "检查磁盘占用", "command": "df -h"},
        ],
    }]


def _worker_loop_incident(worker_status: Any) -> list[dict[str, Any]]:
    if not isinstance(worker_status, dict):
        return []
    if not worker_status.get("running"):
        return [{
            "severity": "critical",
            "kind": "worker_loop_stalled",
            "subject": "automation-worker",
            "dedup_key": "worker_loop_stalled:automation",
            "message": "自动化 Worker 循环未运行，队列不会推进",
            "next_actions": [
                {"action": "检查 Worker 启停状态", "command": "/automation?tab=ops"},
            ],
        }]
    ago = worker_status.get("last_tick_seconds_ago")
    interval = worker_status.get("interval_seconds") or 15
    if ago is None:
        return []
    try:
        ago = float(ago)
        interval = float(interval)
    except (TypeError, ValueError):
        return []
    if ago <= interval * 3:
        return []
    return [{
        "severity": "critical",
        "kind": "worker_loop_stalled",
        "subject": "automation-worker",
        "dedup_key": "worker_loop_stalled:automation",
        "message": f"自动化 Worker tick 假死：{ago:.0f}s 未推进（间隔 {interval:.0f}s）",
        "next_actions": [
            {"action": "查看 Worker 状态", "command": "/api/automation/worker/status"},
        ],
    }]
