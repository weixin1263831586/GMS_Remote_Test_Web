"""Glue: assemble the maintenance snapshot, persist incidents and notify.

``run_ops_sweep()`` is invoked from the automation maintenance tick (never
raises). It evaluates ``features.ops.rules.evaluate``, records new incidents
through the ops store (dedup applies there) and dispatches notifications only
for non-suppressed incidents.
"""

from __future__ import annotations

import logging
from typing import Any

from .notify import load_ops_alerts_config, notify_incident
from .rules import evaluate


logger = logging.getLogger(__name__)


def _safe_import_call(module: str, func: str) -> Any:
    try:
        target = __import__(module, fromlist=[func])
        return getattr(target, func)()
    except Exception:
        logger.debug("ops sweep skipped %s.%s", module, func, exc_info=True)
        return None


def build_snapshot(cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    from foundation.config import settings

    snapshot: dict[str, Any] = {}
    workers = _safe_import_call("features.cluster", "get_cluster_service")
    if workers is not None:
        try:
            snapshot["workers"] = workers.list_workers()
        except Exception:
            logger.debug("ops sweep could not list workers", exc_info=True)

    try:
        from features.automation import get_automation_service, get_worker_status

        snapshot["recent_runs"] = get_automation_service().list_runs(limit=100)
        snapshot["worker_status"] = get_worker_status()
    except Exception:
        logger.debug("ops sweep could not read automation state", exc_info=True)

    try:
        import shutil

        snapshot["disk_free_gb"] = shutil.disk_usage(str(settings.data_root)).free / 1024 ** 3
    except OSError:
        pass
    return snapshot


def run_ops_sweep(cfg: dict[str, Any] | None = None) -> dict[str, int]:
    """One alert sweep; safe to call from the maintenance tick every pass."""
    from . import record_incident

    effective = cfg or load_ops_alerts_config()
    try:
        snapshot = build_snapshot(effective)
    except Exception:
        # 快照失败只丢一轮评估；维护通道本身已兜底，这里保证 sweep 不外抛。
        logger.exception("ops sweep snapshot failed")
        return {"evaluated": 0, "recorded": 0, "notified": 0}
    incidents = evaluate(
        snapshot,
        consecutive_failure_threshold=int(effective.get("consecutive_failure_threshold", 3)),
        min_free_gb=float(effective.get("min_free_gb", 20)),
    )
    recorded = notified = 0
    for incident in incidents:
        result = record_incident(
            severity=incident["severity"],
            kind=incident["kind"],
            subject=incident.get("subject", ""),
            dedup_key=incident.get("dedup_key", ""),
            message=incident["message"],
            next_actions=incident.get("next_actions") or [],
            suppress_minutes=int(effective.get("suppress_minutes", 60)),
        )
        recorded += 1
        if result.get("suppressed") or not result.get("event"):
            continue
        try:
            dispatch = notify_incident(incident, cfg=effective)
            if dispatch.get("sent"):
                notified += 1
        except Exception:
            logger.exception("ops notification dispatch failed")
    return {"evaluated": len(incidents), "recorded": recorded, "notified": notified}
