"""Durable cleanup after a completed ATS run; never replay device work."""

from __future__ import annotations

import json
import logging

from foundation.error_model import ApiError

from .models import utc_now_iso


logger = logging.getLogger(__name__)


def cleanup_state(run: dict) -> str:
    if "resource_cleanup_state" in run:
        return str(run["resource_cleanup_state"] or "")
    try:
        result = json.loads(run.get("result_json") or "{}")
        return str(result.get("resource_cleanup", {}).get("state") or "")
    except (ValueError, TypeError, AttributeError):
        return ""


def cleanup_completed_run(store, executor, run: dict, *, lease_owner: str) -> dict:
    current = store.get_run(run["id"]) or run
    if (current.get("status") != "completed" or not current.get("device_reservation_id")
            or cleanup_state(current) == "released" or current.get("lease_owner") != lease_owner):
        return current
    release = getattr(executor, "release_resources", None)
    if not callable(release):
        return current
    try:
        result = release(current)
        code = "" if result.get("success") else ApiError.conflict("").code
    except ValueError as exc:
        result, code = {"success": False, "error": str(exc)}, ApiError.conflict("").code
    except Exception:
        logger.warning("Completed Run resource cleanup failed for %s", current["id"], exc_info=True)
        result, code = {"success": False, "error": "预约服务暂不可用，请稍后重试"}, ApiError.dependency_unavailable("").code
    try:
        snapshot = json.loads(current.get("result_json") or "{}")
    except (ValueError, TypeError):
        snapshot = {}
    if not isinstance(snapshot, dict):
        snapshot = {}
    previous = snapshot.get("resource_cleanup") or {}
    if not isinstance(previous, dict):
        previous = {}
    cleanup = {"state": "released" if result.get("success") else "pending", "code": code,
               "error": str(result.get("error") or ""), "checked_at": utc_now_iso(),
               "reservation_id": current["device_reservation_id"]}
    snapshot["resource_cleanup"] = cleanup
    updated, applied = store.update_run_if_status(
        current["id"], "completed", expected_lease_owner=lease_owner,
        result_json=json.dumps(snapshot, ensure_ascii=False),
        finished_at=current.get("finished_at") or current.get("updated_at") or utc_now_iso(),
    )
    if applied and (previous.get("state"), previous.get("error")) != (cleanup["state"], cleanup["error"]):
        store.append_event(current["id"], "completed", "info" if result.get("success") else "warning",
                           "设备预约已释放" if result.get("success") else cleanup["error"], cleanup,
                           event_type="run.resource_cleanup")
    return updated
