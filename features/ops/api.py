"""Ops API: kill-switch, worker/ledger status and incident events.

Mutating routes are admin-gated (``require_role("admin")``) to match the
cluster configuration routes; reads require authentication only when auth is
enabled. Every failure uses ``foundation.error_model.ApiError`` — no
hand-rolled status codes.
"""

from __future__ import annotations

import shutil
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from features.auth import (
    CurrentUser,
    require_authenticated_user_when_auth_required,
    require_role,
)
from foundation.config import settings
from foundation.error_model import ApiError

from .store import OPS_FLAG_PAUSED


router = APIRouter(prefix="/api/ops", tags=["ops"])


class KillSwitchRequest(BaseModel):
    paused: bool
    reason: str = Field(default="", max_length=500)


def _disk_summary() -> dict[str, Any]:
    try:
        usage = shutil.disk_usage(str(settings.data_root))
    except OSError:
        return {"available_gb": None, "total_gb": None, "error": "disk usage unavailable"}
    gib = 1024 ** 3
    return {
        "available_gb": round(usage.free / gib, 2),
        "total_gb": round(usage.total / gib, 2),
    }


@router.get("/status")
def ops_status(
    _user: CurrentUser | None = Depends(require_authenticated_user_when_auth_required),
) -> dict[str, Any]:
    from features.automation import get_worker_status

    from .service import get_ops_store

    store = get_ops_store()
    try:
        worker = get_worker_status()
    except Exception:
        worker = {"running": False, "error": "automation worker status unavailable"}
    return {
        "success": True,
        "data": {
            "advancement_paused": store.advancement_paused(),
            "flag": store.flag_meta(OPS_FLAG_PAUSED),
            "worker": worker,
            "disk": _disk_summary(),
            "open_alerts": store.open_event_count(),
        },
    }


@router.post("/kill-switch")
def set_kill_switch(
    body: KillSwitchRequest,
    admin: CurrentUser = Depends(require_role("admin")),
) -> dict[str, Any]:
    from .service import set_advancement_paused

    result = set_advancement_paused(body.paused, updated_by=admin.username)
    return {"success": True, "data": {**result, "reason": body.reason}}


@router.get("/events")
def list_ops_events(
    status: str = Query(default="", pattern="^(open|ack)?$"),
    limit: int = Query(default=100, ge=1, le=500),
    _user: CurrentUser | None = Depends(require_authenticated_user_when_auth_required),
) -> dict[str, Any]:
    from .service import get_ops_store

    items = get_ops_store().list_events(status=status, limit=limit)
    return {"success": True, "data": {"items": items}}


@router.post("/events/{event_id}/ack")
def ack_ops_event(
    event_id: str,
    request: Request,
    admin: CurrentUser = Depends(require_role("admin")),
) -> dict[str, Any]:
    from .service import get_ops_store

    event = get_ops_store().ack_event(event_id, admin.username)
    if event is None:
        raise ApiError.not_found("Ops event not found")
    return {"success": True, "data": event}


class DiskCleanupRequest(BaseModel):
    dry_run: bool = True
    include_stage: bool = False
    stage_max_age_days: int = Field(default=7, ge=1, le=3650)


@router.get("/disk")
def ops_disk_overview(
    _user: CurrentUser | None = Depends(require_authenticated_user_when_auth_required),
) -> dict[str, Any]:
    from . import disk_hygiene

    return {
        "success": True,
        "data": disk_hygiene.preview_cleanup(),
    }


@router.post("/disk/cleanup")
def ops_disk_cleanup(
    body: DiskCleanupRequest,
    _admin: CurrentUser = Depends(require_role("admin")),
) -> dict[str, Any]:
    from . import disk_hygiene

    result = disk_hygiene.run_cleanup(
        stage_max_age_days=body.stage_max_age_days,
        include_stage=body.include_stage,
        dry_run=body.dry_run,
    )
    if not body.dry_run and result.get("removed_files"):
        from .service import record_incident

        freed_gb = result.get("freed_bytes", 0) / 1024 ** 3
        record_incident(
            severity="info",
            kind="disk_hygiene",
            subject="controller-data-root",
            message=(
                f"磁盘清理释放 {freed_gb:.2f} GB（{result.get('removed_files')} 个过期暂存/临时文件）"
            ),
            suppress_minutes=0,
        )
    return {"success": True, "data": result}
