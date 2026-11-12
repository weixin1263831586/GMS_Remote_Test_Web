"""Glue: assemble the maintenance snapshot, persist incidents and notify.

``run_ops_sweep()`` is invoked from the automation maintenance tick (never
raises). It evaluates ``features.ops.rules.evaluate``, records new incidents
through the ops store (dedup applies there) and owns notification delivery:
every sweep first retries events still in ``pending``/``failed`` state
(with capped exponential backoff), then dispatches newly recorded ones.
Delivery state is persisted on the event row, so a send that failed during
one pass is retried by the next one.

Watchdog paths (cluster service) only record incidents — they never notify
directly; this module is the single delivery owner.
"""

from __future__ import annotations

import logging
from typing import Any

from .notify import load_ops_alerts_config, notify_incident
from .rules import evaluate


logger = logging.getLogger(__name__)

# 一次 sweep 最多重试的待投递事件数，防止积压拖垮维护通道。
MAX_RETRY_EVENTS = 20


def _safe_import_call(module: str, func: str) -> Any:
    try:
        target = __import__(module, fromlist=[func])
        return getattr(target, func)()
    except Exception:
        logger.debug("ops sweep skipped %s.%s", module, func, exc_info=True)
        return None


def build_snapshot() -> dict[str, Any]:
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


def _dispatch(store: Any, event: dict[str, Any], cfg: dict[str, Any]) -> bool:
    """Deliver one event row and persist the outcome. Returns True on send."""
    event_id = str(event.get("id") or "")
    attempts = int(event.get("notify_attempts") or 0) + 1
    try:
        dispatch = notify_incident(event, cfg=cfg)
        # notify_incident 返回 {"ok": bool}；兼容旧 {"sent": [...]} 形状。
        if "ok" in dispatch:
            ok = bool(dispatch["ok"])
        else:
            sent = dispatch.get("sent") or []
            ok = bool(sent) and all(item.get("sent") for item in sent)
    except Exception:
        logger.exception("ops notification dispatch failed for %s", event_id)
        ok = False
    store.mark_delivery_state(
        event_id, state="sent" if ok else "failed", attempts=attempts
    )
    if not ok:
        logger.warning(
            "ops notification for %s failed (attempt %s); will retry with backoff",
            event_id, attempts,
        )
    return ok


def _deliver_pending(cfg: dict[str, Any]) -> int:
    """Retry undelivered events: single ownership point for alert delivery."""
    from .service import get_ops_store

    store = get_ops_store()
    notified = 0
    try:
        pending = store.pending_events(limit=MAX_RETRY_EVENTS, ready_only=True)
    except Exception:
        logger.exception("ops pending-event query failed")
        return 0
    for event in pending:
        try:
            if not store.notify_backoff_ready(event):
                continue
        except Exception:
            logger.exception("ops backoff check failed for %s", event.get("id"))
            continue
        if _dispatch(store, event, cfg):
            notified += 1
    return notified


def run_ops_sweep(cfg: dict[str, Any] | None = None) -> dict[str, int]:
    """One alert sweep; safe to call from the maintenance tick every pass."""
    from . import record_incident

    effective = cfg or load_ops_alerts_config()
    notified = _deliver_pending(effective)
    try:
        snapshot = build_snapshot()
    except Exception:
        # 快照失败只丢一轮评估；投递闭环由 pending 重试兜底，sweep 不外抛。
        logger.exception("ops sweep snapshot failed")
        return {"evaluated": 0, "recorded": 0, "notified": notified}
    incidents = evaluate(
        snapshot,
        consecutive_failure_threshold=int(effective.get("consecutive_failure_threshold", 3)),
        min_free_gb=float(effective.get("min_free_gb", 20)),
    )
    recorded = 0
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
        event = result.get("event")
        if not event:
            continue
        recorded += 1
    # 新记录的事件进入 pending 状态，统一走 _deliver_pending 的投递/重试闭环。
    notified += _deliver_pending(effective)
    return {"evaluated": len(incidents), "recorded": recorded, "notified": notified}
