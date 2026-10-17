"""Thin APK approval and bounded, read-only database routes."""

from __future__ import annotations

import asyncio
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from . import debug_service
from . import deployment_service as service
from .support import LOGIN_ASSETS, LOGIN_PANEL


router = APIRouter()


class DatabaseQuery(BaseModel):
    debug_session_id: str = Field(default="", pattern=r"^(?:[a-f0-9]{32})?$")
    worker_id: str = Field(min_length=1, max_length=128)
    device: str = Field(min_length=1, max_length=256)
    package: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+$", max_length=255)
    database: str = Field(pattern=r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")
    table: str = Field(default="", pattern=r"^(?:[A-Za-z_][A-Za-z0-9_]{0,63})?$")
    columns: list[Annotated[str, Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")]] = Field(default_factory=list, max_length=32)
    limit: int = Field(default=100, ge=1, le=500)
    offset: int = Field(default=0, ge=0, le=1000000)


def describe(request: Request, payload: dict):
    return {**payload, "approval_url": str(request.base_url).rstrip("/")
            + "/api/devices/deployments/" + payload["id"] + "/review"}


@router.post("/api/devices/deployments")
async def prepare_deployment(
    request: Request, background: BackgroundTasks, worker_id: str = Form(...), device: str = Form(...),
    replace_existing: bool = Form(default=True), apk_file: UploadFile = File(...),
    debug_session_id: str = Form(default="", pattern=r"^(?:[a-f0-9]{32})?$"),
):
    """Upload an APK for one human-approved transmission and installation."""
    if debug_session_id:
        payload, work = await debug_service.prepare_install(
            request, debug_session_id, worker_id, device, replace_existing, apk_file,
        )
        background.add_task(debug_service.execute_install, work)
        payload = service.store().get(payload["id"], payload["owner_id"])
        return {"success": True, "deployment": {**payload, "debug_session_id": debug_session_id}}
    payload = await service.prepare(request, worker_id, device, replace_existing, apk_file)
    return {"success": True, "deployment": describe(request, payload)}


@router.get("/api/devices/deployments/{request_id}")
def deployment_status(request: Request, request_id: str):
    user = service.authorize(request, "devices.read", human_permission="devices.use_leased")
    payload = service.store().get(request_id, user.resource_owner_id)
    service.check_acl(request, payload["worker_id"], payload["serial"])
    if payload["status"] == "interrupted" and user.has_permission("devices.use_leased"):
        if payload.get("result", {}).get("execution_phase") == "queued":
            service.cleanup_expired_queue(payload)
            payload = service.store().get(request_id, user.resource_owner_id)
        else:
            payload = service.reconcile_interrupted(request, payload)
    return {"success": True, "deployment": describe(request, payload)}


@router.post("/api/devices/deployments/{request_id}/approve")
def approve_deployment(request: Request, request_id: str, background: BackgroundTasks):
    payload, source = service.approve(request, request_id)
    background.add_task(service.execute, payload, source)
    return {"success": True, "deployment": {"id": payload["id"], "status": "queued"}}


@router.get("/api/devices/deployments/{request_id}/review", response_class=HTMLResponse)
def review_deployment(request: Request, request_id: str):
    # External assets comply with the application's CSP; names/results are
    # rendered as text by the browser, never interpolated into HTML.
    return HTMLResponse('''<!doctype html><html lang="zh-CN"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>APK 部署审批</title><link rel="stylesheet" href="/static/css/device-deployment-review.css">
''' + LOGIN_ASSETS + '''
<script src="/static/js/pages/device-deployment-review.js" defer></script></head>
<body><main><h1>APK 部署审批</h1><p>请登录发起申请的 Agent 所属平台账号。核对制品和目标后，一次批准完成传输和安装。</p>
<button id="review-login-open" type="button">登录 / 切换审批账号</button>
<dl id="details"></dl><p id="status" role="status">正在读取部署请求…</p>
<button id="approve" disabled>批准并安装</button><a href="/">返回平台</a>
</main>''' + LOGIN_PANEL + '''</body></html>''')


@router.post("/api/devices/db/query")
async def database_query(request: Request, body: DatabaseQuery):
    service.authorize(request, "devices.database.read")
    return {"success": True, "data": await asyncio.to_thread(debug_service.database_query, request, body)}
