"""Cluster device inventory routes and presentation annotations."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query

from features.auth import CurrentUser, require_authenticated_user_when_auth_required

from .api import service


router = APIRouter()
logger = logging.getLogger(__name__)


def _annotate_adb_proxy_source(devices: list[dict]) -> list[dict]:
    """Stamp ADB Proxy devices with their source worker name/address."""
    from features.devices import get_adb_proxy_service

    source_by_serial: dict[str, str] = {}
    adb_proxy_service = get_adb_proxy_service()
    assignments = list(adb_proxy_service.assignments().values())
    for assignment in assignments:
        target = str(assignment.get("target_worker_id") or "")
        for serial in assignment.get("devices") or []:
            serial = str(serial or "").strip()
            if serial:
                source_by_serial[serial] = target
    if not source_by_serial:
        return devices
    for device in devices:
        if str(device.get("transport") or "") != "adb_proxy":
            continue
        serial = str(device.get("serial") or "")
        target_worker = source_by_serial.get(serial)
        if not target_worker:
            continue
        for assignment in assignments:
            if (
                serial in (assignment.get("devices") or [])
                and str(assignment.get("target_worker_id") or "") == target_worker
            ):
                properties = device.get("properties") or {}
                properties.setdefault(
                    "adb_proxy_source_worker_id",
                    str(assignment.get("source_worker_id") or ""),
                )
                properties.setdefault(
                    "adb_proxy_source_name",
                    str(assignment.get("source_name") or ""),
                )
                properties.setdefault(
                    "adb_proxy_source_address",
                    str(assignment.get("source_address") or ""),
                )
                device["properties"] = properties
                break
    return devices


@router.get("/devices")
def list_devices(
    worker_id: str = Query(default=""),
    _user: CurrentUser | None = Depends(
        require_authenticated_user_when_auth_required
    ),
):
    svc = service()
    if not svc.effective_enabled:
        if worker_id and worker_id != svc.config.local_worker_id:
            raise HTTPException(409, "cluster mode is disabled")
        worker_id = svc.config.local_worker_id
    devices = svc.repository.list_devices(worker_id)
    worker_statuses = {
        str(worker.get("id") or ""): str(worker.get("status") or "offline")
        for worker in svc.list_workers()
    }
    for device in devices:
        # A heartbeat inventory can outlive its Worker. Do not advertise a
        # previously available device as usable after that host goes offline.
        if worker_statuses.get(str(device.get("worker_id") or "")) == "offline":
            device["state"] = "offline"
    try:
        from features.users import resolve_client_display_id

        for device in devices:
            claim_owner_id = str(device.get("claim_owner_id") or "").strip()
            claim_username = str(device.get("claim_username") or "").strip()
            owner_id = claim_owner_id or claim_username
            if owner_id:
                is_self = bool(
                    _user
                    and (
                        _user.id == claim_owner_id
                        or _user.username in {claim_owner_id, claim_username}
                    )
                )
                device["claimed_by"] = (
                    resolve_client_display_id(owner_id)
                    if _user is None or _user.role == "admin" or is_self
                    else "occupied"
                )
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        logger.warning(
            "failed to annotate device claim owners for %s: %s",
            worker_id or "all workers",
            exc,
        )
    for device in devices:
        device.pop("claim_owner_id", None)
        device.pop("claim_username", None)
    try:
        from features.devices import annotate_cluster_usbip_devices

        devices = annotate_cluster_usbip_devices(devices, worker_id)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        logger.warning(
            "failed to annotate USB/IP inventory for %s: %s",
            worker_id or "all workers",
            exc,
        )
    try:
        _annotate_adb_proxy_source(devices)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        logger.warning(
            "failed to annotate ADB Proxy source for %s: %s",
            worker_id or "all workers",
            exc,
        )
    return {"success": True, "devices": devices}


@router.post("/workers/{worker_id}/refresh")
async def refresh_worker_inventory(
    worker_id: str,
    inventory: str = Query(
        default="devices",
        pattern="^(devices|suites|devices,suites|suites,devices)$",
    ),
    _user: CurrentUser | None = Depends(
        require_authenticated_user_when_auth_required
    ),
):
    """Trigger a real Worker-side inventory refresh (not a cached read).

    Sends ``refresh_devices`` / ``refresh_suites`` commands to the Worker
    Agent so it re-runs ``adb devices`` / suite scanning immediately, then
    applies the returned snapshot to the Controller repository. This closes
    the gap where "刚插入设备/刚回 adb/刚解压 suite"后点刷新仍看到旧值，
    必须等下一个 heartbeat。
    """
    import asyncio

    from .api import _local_execute, _require_cluster_enabled, _run_worker_command

    svc = service()
    _require_cluster_enabled(remote=worker_id != svc.config.local_worker_id)
    if svc.repository.get_worker(worker_id) is None:
        raise HTTPException(404, "worker not found")

    requested = {item.strip() for item in inventory.split(",") if item.strip()}
    result: dict = {}
    if "devices" in requested:
        try:
            result.update(await _run_worker_command(
                worker_id, "refresh_devices", {}, timeout=15))
        except HTTPException as exc:
            if exc.status_code == 504:
                raise HTTPException(
                    504, "worker did not answer the refresh in time") from exc
            raise
        devices = result.get("devices")
        if isinstance(devices, list):
            svc.repository.refresh_worker_devices(worker_id, devices)
    if "suites" in requested:
        if worker_id == svc.config.local_worker_id:
            suites = await asyncio.to_thread(
                _local_execute, "refresh_suites", {})
        else:
            suites = await _run_worker_command(
                worker_id, "refresh_suites", {}, timeout=30)
        if isinstance(suites, dict) and isinstance(suites.get("suites"), list):
            svc.repository.replace_worker_suites(worker_id, suites["suites"])

    devices_list = svc.repository.list_devices(worker_id)
    return {"success": True, "devices": devices_list,
            "refreshed": sorted(requested)}
