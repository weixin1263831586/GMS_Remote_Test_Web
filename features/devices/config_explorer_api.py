"""Android resource config explorer router.

Exposes the "配置资源查看器" tool: list framework (or any package) config
resources with their APK default value and, optionally, the overlay-effective
value. The UI is embedded in the device-config modal; these /api endpoints are
consumed by that modal (no standalone page or sidebar entry).
"""

import asyncio
import logging
import os
import uuid
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from features.users import get_client_id_from_request
from foundation.config import APK_MAX_FILE_SIZE, APK_UPLOAD_DIR
from foundation.errors import handle_api_errors
from foundation.responses import error_response, success_response

from .config_explorer import (
    explore,
    list_all_packages,
    list_devices,
    list_features,
    list_packages,
    list_packages_with_path,
    list_props,
    pull_device_file,
)
from .locks import device_lock_manager


logger = logging.getLogger(__name__)

router = APIRouter()

_generate_help_or_continue: Callable[[bool, str, str], Any] | None = None
_create_apk_task: Callable[[str, str, str, str], Any] | None = None
_normalize_apk_filename: Callable[[str], str] | None = None
_safe_join: Callable[[str, str], str] | None = None
_cleanup_files: Callable[[list[str]], Any] | None = None


def configure_config_explorer_dependencies(
    *,
    generate_help_or_continue: Callable[[bool, str, str], Any],
    create_apk_task: Callable[[str, str, str, str], Any],
    normalize_apk_filename: Callable[[str], str],
    safe_join: Callable[[str, str], str],
    cleanup_files: Callable[[list[str]], Any],
) -> None:
    global _generate_help_or_continue
    global _create_apk_task
    global _normalize_apk_filename
    global _safe_join
    global _cleanup_files

    _generate_help_or_continue = generate_help_or_continue
    _create_apk_task = create_apk_task
    _normalize_apk_filename = normalize_apk_filename
    _safe_join = safe_join
    _cleanup_files = cleanup_files


def _help_or_continue(help: bool, method: str, path: str):
    if _generate_help_or_continue is None:
        return None
    return _generate_help_or_continue(help, method, path)


def _decompile_dependencies_ready() -> bool:
    return all(
        dep is not None
        for dep in (
            _create_apk_task,
            _normalize_apk_filename,
            _safe_join,
            _cleanup_files,
        )
    )


def _readable_device(request: Request, device_id: str) -> str:
    selected = str(device_id or "").strip()
    if not selected:
        online = [
            str(item.get("serial") or "")
            for item in list_devices()
            if item.get("state") == "device" and item.get("serial")
        ]
        if len(online) != 1:
            raise HTTPException(
                400, "device_id is required when zero or multiple devices are online"
            )
        selected = online[0]
    claim = device_lock_manager.get_lock_status(selected)
    client_id = get_client_id_from_request(request)
    if claim and claim.get("client_id") != client_id:
        raise HTTPException(409, "device is reserved by another active operation")
    return selected


@router.get("/api/config-explorer/devices")
@handle_api_errors
async def api_list_devices(request: Request, help: bool = Query(False)):
    """List adb devices available for config exploration."""
    resp = _help_or_continue(help, "GET", "/api/config-explorer/devices")
    if resp:
        return resp
    devices = await asyncio.to_thread(list_devices)
    return success_response(data={"devices": devices}, message="Success")


@router.get("/api/config-explorer/packages")
@handle_api_errors
async def api_list_packages(
    request: Request,
    device_id: str = Query("", description="adb serial; empty = default device"),
    help: bool = Query(False),
):
    """List packages that typically carry config_* resources."""
    resp = _help_or_continue(help, "GET", "/api/config-explorer/packages")
    if resp:
        return resp
    packages = await asyncio.to_thread(
        lambda: list_packages(_readable_device(request, device_id))
    )
    return success_response(data={"packages": packages}, message="Success")


@router.get("/api/config-explorer/packages/all")
@handle_api_errors
async def api_list_all_packages(
    request: Request,
    device_id: str = Query("", description="adb serial; empty = default device"),
    help: bool = Query(False),
):
    """List ALL packages on the device (pm list packages) for the package picker."""
    resp = _help_or_continue(help, "GET", "/api/config-explorer/packages/all")
    if resp:
        return resp
    packages = await asyncio.to_thread(
        lambda: list_all_packages(_readable_device(request, device_id))
    )
    return success_response(
        data={"packages": packages, "count": len(packages)}, message="Success"
    )


@router.get("/api/config-explorer/packages-with-path")
@handle_api_errors
async def api_list_packages_with_path(
    request: Request,
    device_id: str = Query("", description="adb serial; empty = default device"),
    help: bool = Query(False),
):
    """``pm list packages -f`` → [{path, package}] (device info: packages tab)."""
    resp = _help_or_continue(
        help, "GET", "/api/config-explorer/packages-with-path"
    )
    if resp:
        return resp
    rows = await asyncio.to_thread(
        lambda: list_packages_with_path(_readable_device(request, device_id))
    )
    return success_response(data={"rows": rows, "count": len(rows)}, message="Success")


@router.get("/api/config-explorer/features")
@handle_api_errors
async def api_list_features(
    request: Request,
    device_id: str = Query("", description="adb serial; empty = default device"),
    help: bool = Query(False),
):
    """``pm list features`` → [{name, version?}] (device info: features tab)."""
    resp = _help_or_continue(help, "GET", "/api/config-explorer/features")
    if resp:
        return resp
    rows = await asyncio.to_thread(
        lambda: list_features(_readable_device(request, device_id))
    )
    return success_response(data={"rows": rows, "count": len(rows)}, message="Success")


@router.get("/api/config-explorer/props")
@handle_api_errors
async def api_list_props(
    request: Request,
    device_id: str = Query("", description="adb serial; empty = default device"),
    help: bool = Query(False),
):
    """``getprop`` → [{name, value}] (device info: props tab)."""
    resp = _help_or_continue(help, "GET", "/api/config-explorer/props")
    if resp:
        return resp
    rows = await asyncio.to_thread(
        lambda: list_props(_readable_device(request, device_id))
    )
    return success_response(data={"rows": rows, "count": len(rows)}, message="Success")


@router.get("/api/config-explorer")
@handle_api_errors
async def api_explore(
    request: Request,
    package: str = Query("android", description="包名，如 android"),
    device_id: str = Query("", description="adb serial；为空时用默认设备"),
    name: str = Query("", description="资源名子串过滤（忽略大小写）"),
    type: str = Query("", description="类型过滤：bool/integer/string/dimen/array"),
    config_only: bool = Query(True, description="只显示 config_* 资源"),
    with_effective: bool = Query(
        False, description="同时计算 overlay 生效值（每资源一次 adb 调用，并发执行）"
    ),
    effective_limit: int = Query(
        0, description="生效值查询上限；0=不限（计算全部）。仅在 with_effective 时生效"
    ),
    help: bool = Query(False),
):
    """List config resources of a package with default (+optional effective) values."""
    resp = _help_or_continue(help, "GET", "/api/config-explorer")
    if resp:
        return resp

    try:
        result = await asyncio.to_thread(
            lambda: explore(
                package=package or "android",
                device_id=_readable_device(request, device_id),
                name_filter=name or None,
                type_filter=type or None,
                config_only=config_only,
                with_effective=with_effective,
                effective_limit=effective_limit,
            )
        )
    except Exception as e:
        logger.error(f"config-explorer explore failed: {e}")
        return error_response(str(e), status_code=400)

    return success_response(
        data={
            "package": result.package,
            "apk_path": result.apk_path,
            "total": result.total,
            "overlayed_count": result.overlayed_count,
            "resources": result.resources,
        },
        message="Success",
    )


class DecompileRequest(BaseModel):
    device_id: str = ""
    path: str


@router.post("/api/config-explorer/decompile")
@handle_api_errors
async def decompile_device_apk(req: DecompileRequest, request: Request):
    """Pull an on-device APK/JAR and register it as an APK-analysis task.

    The file is pulled into the APK upload dir (so the existing APK-analysis
    pipeline can decompile it), then a task is created. Returns ``task_id`` /
    ``filename`` / ``size``; the frontend then switches to the APK-analysis
    page and starts the analysis (same flow as suite→APK).
    """
    if not _decompile_dependencies_ready():
        return error_response("APK 分析依赖未初始化", status_code=500)
    if not req.path.strip():
        return error_response("缺少 APK 路径", status_code=400)
    on_device_path = req.path.strip()

    # 从包名或路径末段生成下载文件名。
    base = os.path.basename(on_device_path.rstrip("/")) or "app.apk"
    if "." not in base:
        base += ".apk"
    assert _normalize_apk_filename is not None
    assert _safe_join is not None
    assert _cleanup_files is not None
    assert _create_apk_task is not None
    filename = _normalize_apk_filename(base)
    task_id = str(uuid.uuid4())
    task_dir = _safe_join(APK_UPLOAD_DIR, task_id)
    os.makedirs(task_dir, exist_ok=True)
    apk_path = _safe_join(task_dir, filename)

    try:
        # Pull on a worker thread (blocking adb transfer).
        await asyncio.to_thread(
            lambda: pull_device_file(
                _readable_device(request, req.device_id),
                on_device_path,
                apk_path,
            )
        )
    except Exception as e:
        _cleanup_files([apk_path])
        logger.error(f"decompile pull failed: {e}")
        return error_response(f"拉取 APK 失败: {e}", status_code=400)

    if os.path.getsize(apk_path) > APK_MAX_FILE_SIZE:
        _cleanup_files([apk_path])
        return error_response(
            f"文件过大，上限 {APK_MAX_FILE_SIZE // (1024 * 1024)}MB", status_code=400
        )

    try:
        _create_apk_task(
            task_id,
            apk_path,
            filename,
            get_client_id_from_request(request),
        )
    except ValueError as exc:
        _cleanup_files([apk_path])
        return error_response(str(exc), status_code=429)
    return success_response(
        data={
            "task_id": task_id,
            "filename": filename,
            "size": os.path.getsize(apk_path),
            "source_path": on_device_path,
        },
        message="Success",
    )
