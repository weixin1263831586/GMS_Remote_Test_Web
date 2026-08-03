"""Android RRO 配置覆盖接口。"""

import asyncio
import logging
from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel

from features.auth import require_elevated_admin_when_auth_required
from features.users import get_client_id_from_request
from foundation.errors import handle_api_errors
from foundation.responses import error_response, success_response

from .config_override import (
    DEFAULT_TARGET_PACKAGE,
    OverrideEntry,
    OverrideStatus,
    OverrideStore,
    apply_overrides,
    build_config_xml,
    build_manifest,
    disable_verity,
    enable_verity,
    probe_status,
    reboot_device,
    revert_all,
)
from .support import device_claim_conflict_response, device_mutation_guard


logger = logging.getLogger(__name__)

router = APIRouter()


def _status_dict(s: OverrideStatus) -> dict[str, Any]:
    d = asdict(s)
    return d


class UpsertEntryRequest(BaseModel):
    device_id: str = ""
    target_package: str = DEFAULT_TARGET_PACKAGE
    resource_name: str
    resource_type: str
    value: str


def _store_for_request(request: Request) -> OverrideStore:
    return OverrideStore(owner_id=get_client_id_from_request(request))


def _mutation_claim_error(request: Request, device_id: str):
    if not str(device_id or "").strip():
        return error_response("device_id is required for device changes", status_code=400)
    return device_claim_conflict_response(
        [device_id],
        get_client_id_from_request(request),
        allow_owner=True,
    )


def _read_claim_error(request: Request, device_id: str):
    if not str(device_id or "").strip():
        return error_response("device_id is required", status_code=400)
    return device_claim_conflict_response(
        [device_id], get_client_id_from_request(request), allow_owner=True
    )


@router.get("/api/config-override/entries")
@handle_api_errors
async def api_list_entries(
    request: Request,
    device_id: str = Query("", description="adb serial；为空时用默认设备"),
):
    """List stored overrides for a device."""
    store = _store_for_request(request)
    entries = store.list_entries(device_id or None)
    return success_response(
        data={"entries": [e.to_dict() for e in entries], "count": len(entries)},
        message="Success",
    )


@router.post("/api/config-override/entries")
@handle_api_errors
async def api_upsert_entry(req: UpsertEntryRequest, request: Request):
    """Insert or update one override (validated server-side)."""
    store = _store_for_request(request)
    entry = OverrideEntry(
        resource_name=req.resource_name.strip(),
        resource_type=req.resource_type,
        value=req.value,
        target_package=req.target_package or DEFAULT_TARGET_PACKAGE,
    )
    try:
        store.upsert(req.device_id or None, entry)
    except ValueError as e:
        return error_response(str(e), status_code=400)
    return success_response(
        data={"entry": entry.to_dict()},
        message="已保存覆盖项",
    )


@router.delete("/api/config-override/entries")
@handle_api_errors
async def api_remove_entry(
    request: Request,
    device_id: str = Query(""),
    resource_name: str = Query(..., description="要删除的资源名"),
):
    """Remove one stored override by resource name."""
    store = _store_for_request(request)
    removed = store.remove(device_id or None, resource_name)
    return success_response(data={"removed": removed}, message="Success")


@router.delete("/api/config-override/entries/all")
@handle_api_errors
async def api_clear_entries(
    request: Request,
    device_id: str = Query("", description="清空该设备的 host 存储；不触碰设备"),
):
    """Clear all stored overrides for a device (host store only)."""
    store = _store_for_request(request)
    count = store.clear(device_id or None)
    return success_response(data={"removed": count}, message="Success")


@router.get("/api/config-override/status")
@handle_api_errors
async def api_status(
    request: Request,
    device_id: str = Query("", description="adb serial；为空时用默认设备"),
):
    """Read-only probe of the device's override-readiness."""
    if conflict := _read_claim_error(request, device_id):
        return conflict
    status = await asyncio.to_thread(probe_status, device_id)
    return success_response(data=_status_dict(status), message="Success")


@router.post("/api/config-override/apply")
@device_mutation_guard("config-override-apply", device_argument="device_id")
@handle_api_errors
async def api_apply(
    request: Request,
    device_id: str = Query("", description="adb serial；为空时用默认设备"),
    _admin=Depends(require_elevated_admin_when_auth_required),
):
    """Rebuild the overlay APK from all stored overrides, push, reboot.

    Returns once the push completes (rebooting=True). The device is offline for
    ~40s after this call returns; the UI should poll /status afterward.
    """
    if conflict := _mutation_claim_error(request, device_id):
        return conflict
    result = await asyncio.to_thread(apply_overrides, device_id, _store_for_request(request))
    if not result.success:
        return error_response(result.message, status_code=400)
    return success_response(data=asdict(result), message=result.message)


@router.post("/api/config-override/revert")
@device_mutation_guard("config-override-revert", device_argument="device_id")
@handle_api_errors
async def api_revert(
    request: Request,
    device_id: str = Query("", description="adb serial；为空时用默认设备"),
    _admin=Depends(require_elevated_admin_when_auth_required),
):
    """Delete the overlay APK from the device and reboot (host store kept)."""
    if conflict := _mutation_claim_error(request, device_id):
        return conflict
    result = await asyncio.to_thread(revert_all, device_id, _store_for_request(request))
    if not result.success:
        return error_response(result.message, status_code=400)
    return success_response(data=asdict(result), message=result.message)


@router.post("/api/config-override/disable-verity")
@device_mutation_guard("disable-verity", device_argument="device_id")
@handle_api_errors
async def api_disable_verity(
    request: Request,
    device_id: str = Query("", description="adb serial；为空时用默认设备"),
    _admin=Depends(require_elevated_admin_when_auth_required),
):
    """One-time bootstrap: ``adb disable-verity``. Returns needs_reboot; the UI
    then chains POST /api/config-override/reboot (or skips if already disabled).
    Required before apply() on a userdebug device whose verity is enforcing."""
    if conflict := _mutation_claim_error(request, device_id):
        return conflict
    result = await asyncio.to_thread(disable_verity, device_id)
    if not result.success:
        return error_response(result.message, status_code=400)
    return success_response(data={"action": result.action, "message": result.message, "needs_reboot": result.needs_reboot}, message=result.message)


@router.post("/api/config-override/enable-verity")
@device_mutation_guard("enable-verity", device_argument="device_id")
@handle_api_errors
async def api_enable_verity(
    request: Request,
    device_id: str = Query("", description="adb serial；为空时用默认设备"),
    _admin=Depends(require_elevated_admin_when_auth_required),
):
    """Restore verified boot. Returns needs_reboot."""
    if conflict := _mutation_claim_error(request, device_id):
        return conflict
    result = await asyncio.to_thread(enable_verity, device_id)
    if not result.success:
        return error_response(result.message, status_code=400)
    return success_response(data={"action": result.action, "message": result.message, "needs_reboot": result.needs_reboot}, message=result.message)


@router.post("/api/config-override/reboot")
@device_mutation_guard("config-override-reboot", device_argument="device_id")
@handle_api_errors
async def api_reboot(
    request: Request,
    device_id: str = Query("", description="adb serial；为空时用默认设备"),
    _admin=Depends(require_elevated_admin_when_auth_required),
):
    """Reboot the device via the local adb connection (same path as
    disable/enable-verity, so the device_id is unambiguous)."""
    if conflict := _mutation_claim_error(request, device_id):
        return conflict
    result = await asyncio.to_thread(reboot_device, device_id)
    if not result.success:
        return error_response(result.message, status_code=400)
    return success_response(data=asdict(result), message=result.message)


@router.get("/api/config-override/preview-xml")
@handle_api_errors
async def api_preview_xml(
    request: Request,
    device_id: str = Query(""),
):
    """Show the manifest + res/values/config.xml that apply() would build.

    Pure (no device I/O) — useful as a preview and as a test seam.
    """
    store = _store_for_request(request)
    entries = store.list_entries(device_id or None)
    return success_response(
        data={
            "manifest": build_manifest(),
            "config_xml": build_config_xml(entries) if entries else "",
            "entry_count": len(entries),
        },
        message="Success",
    )
