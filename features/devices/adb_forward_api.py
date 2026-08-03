"""Elevated host-level ADB tunnel controls."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query
from fastapi.responses import JSONResponse

from features.auth import require_elevated_admin
from features.cluster.worker_auth import authenticate_worker
from foundation.responses import error_response

from .adb_forward import adb_forward_manager
from .adb_proxy_security import pair_code_for_worker, validate_pair_grant
from .adb_proxy_service import adb_proxy_service
from .models import (
    ADBForwardStartRequest,
    ADBForwardStopRequest,
    ADBProxyPairCodeRequest,
)


logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/api/adb-forward/status")
async def adb_forward_status(_admin=Depends(require_elevated_admin)):
    return JSONResponse(content=adb_proxy_service.status())


@router.get("/api/adb-forward/logs")
async def adb_proxy_logs(
    worker_id: str = Query(min_length=1, max_length=128),
    _admin=Depends(require_elevated_admin),
):
    return JSONResponse(content=await adb_proxy_service.logs(worker_id))


@router.post("/api/adb-forward/start")
async def start_adb_forward(
    req: ADBForwardStartRequest | None = Body(default=None),
    _admin=Depends(require_elevated_admin),
):
    try:
        if req and req.source_worker_id and req.target_worker_id:
            result = await adb_proxy_service.connect(
                req.source_worker_id,
                req.target_worker_id,
                req.devices,
            )
            return JSONResponse(content=result)

        # Preserve the former controller-only SSH tunnel API for old clients.
        # The web UI uses the worker-aware adbproxy-rs flow above.
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
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Error starting ADB forward: %s", exc)
        return error_response(f"{exc!s}. 请检查配置和参数是否正确。", status_code=500)


@router.post("/api/adb-forward/stop")
async def stop_adb_forward(
    req: ADBForwardStopRequest | None = Body(default=None),
    _admin=Depends(require_elevated_admin),
):
    try:
        if req and req.source_worker_id and req.target_worker_id:
            result = await adb_proxy_service.disconnect(
                req.source_worker_id,
                req.target_worker_id,
            )
            return JSONResponse(content=result)
        result = adb_forward_manager.stop_forward("test_client")
        if result.get("success"):
            return JSONResponse(content=result)
        return error_response(
            result.get("error", "ADB转发停止失败"), status_code=500
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Error stopping ADB forward: %s", exc)
        return error_response(f"{exc!s}. 请检查配置和参数是否正确。", status_code=500)


@router.post("/api/cluster/workers/{worker_id}/adb-proxy/pair-code")
async def worker_adb_proxy_pair_code(
    worker_id: str,
    body: ADBProxyPairCodeRequest,
    authorization: str | None = Header(default=None),
    worker_session: str = Header(default="", alias="X-GMS-Worker-Session"),
    worker_generation: int = Header(default=0, alias="X-GMS-Worker-Generation"),
):
    """Return a pair code only to the target Worker holding a short-lived grant."""
    authenticate_worker(worker_id, authorization)
    from features.cluster.commands_api import _require_worker_session

    _require_worker_session(worker_id, worker_session, worker_generation)
    from features.cluster import get_cluster_service

    local_worker_id = get_cluster_service().config.local_worker_id
    try:
        validate_pair_grant(
            body.access_token,
            body.source_worker_id,
            worker_id,
            local_worker_id,
        )
        pair_code = pair_code_for_worker(
            body.source_worker_id,
            local_worker_id,
            body.access_token,
        )
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(403, str(exc)) from exc
    # Use an already-redacted response key so middleware cannot persist the
    # short-lived infrastructure credential in an audit response body.
    return JSONResponse(
        content={"success": True, "access_token": pair_code},
        headers={"Cache-Control": "no-store"},
    )
