"""Elevated host-level ADB tunnel controls."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Body, Depends
from fastapi.responses import JSONResponse

from features.auth import require_elevated_admin
from foundation.responses import error_response

from .adb_forward import adb_forward_manager
from .models import ADBForwardStartRequest


logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/api/adb-forward/start")
async def start_adb_forward(
    req: ADBForwardStartRequest | None = Body(default=None),
    _admin=Depends(require_elevated_admin),
):
    try:
        config = adb_forward_manager.config_manager.load_config()
        device_host = str(
            (req.device_host if req else "")
            or config.get("device_host")
            or config.get("local_server")
            or ""
        ).strip()
        result = adb_forward_manager.start_forward(
            device_host
        )
        if result.get("success"):
            return JSONResponse(content=result)
        return error_response(
            result.get("error", "ADB转发启动失败"), status_code=500
        )
    except Exception as exc:
        logger.error("Error starting ADB forward: %s", exc)
        return error_response(f"{exc!s}. 请检查配置和参数是否正确。", status_code=500)


@router.post("/api/adb-forward/stop")
async def stop_adb_forward(_admin=Depends(require_elevated_admin)):
    try:
        result = adb_forward_manager.stop_forward("test_client")
        if result.get("success"):
            return JSONResponse(content=result)
        return error_response(
            result.get("error", "ADB转发停止失败"), status_code=500
        )
    except Exception as exc:
        logger.error("Error stopping ADB forward: %s", exc)
        return error_response(f"{exc!s}. 请检查配置和参数是否正确。", status_code=500)
