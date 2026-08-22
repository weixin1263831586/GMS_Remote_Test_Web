"""Operational liveness, readiness, details, and metrics endpoints."""

from __future__ import annotations

import asyncio
import hmac
from datetime import datetime

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from features.auth import CurrentUser, require_role
from features.system.health import readiness
from features.system.metrics import metrics_token, render_metrics
from foundation.product import APPLICATION_VERSION, SERVICE_NAME


router = APIRouter()


@router.get('/api/system/health')
async def health_check():
    """Compatibility liveness probe; dependency readiness has its own route."""
    return JSONResponse(content={
        'status': 'alive',
        'service': SERVICE_NAME,
        'version': APPLICATION_VERSION,
        'timestamp': datetime.now().isoformat(),
    })


@router.get('/api/system/health/live')
async def liveness_check():
    return {'status': 'alive', 'timestamp': datetime.now().isoformat()}


@router.get('/api/system/health/ready')
async def readiness_check(request: Request):
    result = await asyncio.to_thread(readiness, request.app)
    return JSONResponse(
        status_code=200 if result['ready'] else 503,
        content={
            'status': 'ready' if result['ready'] else 'not_ready',
            'degraded': bool(result['degraded_checks']),
            'timestamp': datetime.now().isoformat(),
        },
    )


@router.get('/api/system/health/details')
async def health_details(
    request: Request,
    _admin: CurrentUser = Depends(require_role('admin')),
):
    result = await asyncio.to_thread(readiness, request.app, force=True)
    return JSONResponse(
        status_code=200 if result['ready'] else 503,
        content=result,
    )


@router.get('/metrics', include_in_schema=False)
async def prometheus_metrics(
    authorization: str = Header(default='', alias='Authorization'),
):
    expected = metrics_token()
    if not expected:
        raise HTTPException(status_code=503, detail='GMS_METRICS_TOKEN is required')
    scheme, separator, supplied = authorization.partition(' ')
    if (
        not separator
        or scheme.lower() != 'bearer'
        or not hmac.compare_digest(supplied.strip(), expected)
    ):
        raise HTTPException(status_code=401, detail='Invalid metrics credential')
    return Response(
        content=await asyncio.to_thread(render_metrics),
        media_type='text/plain; version=0.0.4; charset=utf-8',
        headers={'Cache-Control': 'no-store'},
    )
