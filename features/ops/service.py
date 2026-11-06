"""Ops service surface: singleton store plus resilient convenience helpers.

Cross-feature callers (automation worker, cluster watchdog) import these from
``features.ops`` — the feature public surface — never from this module
directly (ADR 0002). Helpers are fail-open: an ops-storage problem must not
stop the automation pipeline; it is logged and surfaced as an open event on
the next successful pass.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from foundation.config import settings

from .store import OPS_FLAG_PAUSED, OpsStore


logger = logging.getLogger(__name__)

_store: OpsStore | None = None
_store_lock = threading.Lock()


def get_ops_store() -> OpsStore:
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                _store = OpsStore(settings.data_root / "ops/ops.sqlite3")
    return _store


def advancement_paused() -> bool:
    """Whether the automation kill-switch currently pauses advancement.

    Fail-open by design: if the ops store cannot answer, log and continue so
    a broken ledger never wedges the pipeline silently. The failure is still
    visible via the automation worker log and this module's logger.
    """
    try:
        return get_ops_store().advancement_paused()
    except Exception:
        logger.exception("ops kill-switch check failed; advancement continues")
        return False


def set_advancement_paused(paused: bool, updated_by: str = "") -> dict[str, Any]:
    store = get_ops_store()
    store.set_flag(OPS_FLAG_PAUSED, bool(paused), updated_by=updated_by)
    result = store.record_incident(
        severity="info",
        kind="kill_switch",
        subject=updated_by,
        dedup_key=f"kill_switch:{bool(paused)}",
        message=(
            f"自动化推进已{'暂停（kill-switch 打开）' if paused else '恢复（kill-switch 关闭）'}"
            f"，操作人: {updated_by or 'unknown'}"
        ),
        suppress_minutes=0,
    )
    return {
        "advancement_paused": bool(paused),
        "event_id": (result.get("event") or {}).get("id", ""),
    }


def record_incident(**kwargs: Any) -> dict[str, Any]:
    """Record an incident; never raises (callers sit inside watchdog paths)."""
    try:
        return get_ops_store().record_incident(**kwargs)
    except Exception:
        logger.exception("ops incident recording failed")
        return {"event": None, "suppressed": True}
