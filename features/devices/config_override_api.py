"""Config override router.

Exposes the "配置覆盖" tool (RRO-based config_* override) consumed by the
device-config modal's "⚡ override" tab. Mirrors config_explorer_api
conventions: ``@handle_api_errors``, ``success_response``/``error_response``,
``Query`` params, ``asyncio.to_thread`` for blocking adb/aapt2 work. No DI.

Endpoints:
  GET    /api/config-override/entries        list stored overrides
  POST   /api/config-override/entries        upsert one override (validates)
  DELETE /api/config-override/entries        remove one override
  DELETE /api/config-override/entries/all    clear stored overrides
  GET    /api/config-override/status         read-only device readiness probe
  POST   /api/config-override/apply          rebuild APK + push + reboot
  POST   /api/config-override/revert         delete APK + reboot
  POST   /api/config-override/disable-verity one-time bootstrap (needs reboot)
  POST   /api/config-override/enable-verity  restore verified boot (needs reboot)
  POST   /api/config-override/reboot         reboot device (same adb path)
  GET    /api/config-override/preview-xml    show XML that would be built (no I/O)
"""

import asyncio
import logging
from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel

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


@router.get("/api/config-override/entries")
@handle_api_errors
async def api_list_entries(
    request: Request,
    device_id: str = Query("", description="adb serial；为空时用默认设备"),
):
    """List stored overrides for a device."""
    store = OverrideStore()
    entries = store.list_entries(device_id or None)
    return success_response(
        data={"entries": [e.to_dict() for e in entries], "count": len(entries)},
        message="Success",
    )


@router.post("/api/config-override/entries")
@handle_api_errors
async def api_upsert_entry(req: UpsertEntryRequest):
    """Insert or update one override (validated server-side)."""
    store = OverrideStore()
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
    store = OverrideStore()
    removed = store.remove(device_id or None, resource_name)
    return success_response(data={"removed": removed}, message="Success")


@router.delete("/api/config-override/entries/all")
@handle_api_errors
async def api_clear_entries(
    request: Request,
    device_id: str = Query("", description="清空该设备的 host 存储；不触碰设备"),
):
    """Clear all stored overrides for a device (host store only)."""
    store = OverrideStore()
    count = store.clear(device_id or None)
    return success_response(data={"removed": count}, message="Success")


@router.get("/api/config-override/status")
@handle_api_errors
async def api_status(
    request: Request,
    device_id: str = Query("", description="adb serial；为空时用默认设备"),
):
    """Read-only probe of the device's override-readiness."""
    status = await asyncio.to_thread(probe_status, device_id or None)
    return success_response(data=_status_dict(status), message="Success")


@router.post("/api/config-override/apply")
@handle_api_errors
async def api_apply(
    request: Request,
    device_id: str = Query("", description="adb serial；为空时用默认设备"),
):
    """Rebuild the overlay APK from all stored overrides, push, reboot.

    Returns once the push completes (rebooting=True). The device is offline for
    ~40s after this call returns; the UI should poll /status afterward.
    """
    result = await asyncio.to_thread(apply_overrides, device_id or None)
    if not result.success:
        return error_response(result.message, status_code=400)
    return success_response(data=asdict(result), message=result.message)


@router.post("/api/config-override/revert")
@handle_api_errors
async def api_revert(
    request: Request,
    device_id: str = Query("", description="adb serial；为空时用默认设备"),
):
    """Delete the overlay APK from the device and reboot (host store kept)."""
    result = await asyncio.to_thread(revert_all, device_id or None)
    if not result.success:
        return error_response(result.message, status_code=400)
    return success_response(data=asdict(result), message=result.message)


@router.post("/api/config-override/disable-verity")
@handle_api_errors
async def api_disable_verity(
    request: Request,
    device_id: str = Query("", description="adb serial；为空时用默认设备"),
):
    """One-time bootstrap: ``adb disable-verity``. Returns needs_reboot; the UI
    then chains POST /api/config-override/reboot (or skips if already disabled).
    Required before apply() on a userdebug device whose verity is enforcing."""
    result = await asyncio.to_thread(disable_verity, device_id or None)
    if not result.success:
        return error_response(result.message, status_code=400)
    return success_response(data={"action": result.action, "message": result.message, "needs_reboot": result.needs_reboot}, message=result.message)


@router.post("/api/config-override/enable-verity")
@handle_api_errors
async def api_enable_verity(
    request: Request,
    device_id: str = Query("", description="adb serial；为空时用默认设备"),
):
    """Restore verified boot. Returns needs_reboot."""
    result = await asyncio.to_thread(enable_verity, device_id or None)
    if not result.success:
        return error_response(result.message, status_code=400)
    return success_response(data={"action": result.action, "message": result.message, "needs_reboot": result.needs_reboot}, message=result.message)


@router.post("/api/config-override/reboot")
@handle_api_errors
async def api_reboot(
    request: Request,
    device_id: str = Query("", description="adb serial；为空时用默认设备"),
):
    """Reboot the device via the local adb connection (same path as
    disable/enable-verity, so the device_id is unambiguous)."""
    result = await asyncio.to_thread(reboot_device, device_id or None)
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
    store = OverrideStore()
    entries = store.list_entries(device_id or None)
    return success_response(
        data={
            "manifest": build_manifest(),
            "config_xml": build_config_xml(entries) if entries else "",
            "entry_count": len(entries),
        },
        message="Success",
    )
