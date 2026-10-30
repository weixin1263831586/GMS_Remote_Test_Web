"""Typed finite debug sessions with human-only approval and revocation UI."""

from __future__ import annotations

import asyncio
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field

from features.auth import require_permission
from foundation.error_model import ApiError

from . import debug_service as service
from .support import LOGIN_ASSETS, LOGIN_PANEL


router = APIRouter()
ID = r"^[a-f0-9]{32}$"
PACKAGE = r"^[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+$"


class DebugAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["launch", "stop", "tap", "swipe", "text", "keyevent"]
    activity: str | None = Field(default=None, max_length=255, pattern=r"^\.?[A-Za-z_$][A-Za-z0-9_.$]*$")
    x: int | None = Field(default=None, ge=0, le=10000)
    y: int | None = Field(default=None, ge=0, le=10000)
    end_x: int | None = Field(default=None, ge=0, le=10000)
    end_y: int | None = Field(default=None, ge=0, le=10000)
    duration_ms: int | None = Field(default=None, ge=1, le=2000)
    text: str | None = Field(default=None, min_length=1, max_length=200)
    key: str | None = Field(default=None, max_length=24)


def describe(request, session):
    return {**{key: value for key, value in session.items() if not key.startswith("_")},
            "approval_url": str(request.base_url).rstrip("/") + "/api/devices/debug-sessions/" + session["id"] + "/review"}


@router.post("/api/devices/debug-sessions")
async def prepare_session(
    request: Request, worker_id: str = Form(..., min_length=1, max_length=128),
    device: str = Form(..., min_length=1, max_length=256), package: str = Form(..., pattern=PACKAGE, max_length=255),
    duration_minutes: int = Form(default=60, ge=15, le=120),
    operations: str | None = Form(default=None, max_length=100),
    apk_file: UploadFile | None = File(default=None), installed: bool = Form(default=False),
):
    """Bind a grant to an uploaded APK or the verified installed app."""
    defaults = "launch,stop,tap,swipe,text,keyevent"
    if operations is None:
        operations = defaults if installed else "install," + defaults
    allowed = set(operations.split(","))
    if not allowed or not allowed <= service.OPERATIONS:
        raise ApiError.invalid_semantics("调试操作列表包含不支持的操作")
    session = await service.prepare(request, worker_id, device, package, duration_minutes, allowed, apk_file,
                                    installed=installed)
    return {"success": True, "session": describe(request, session)}


@router.get("/api/devices/debug-sessions/{session_id}")
def session_status(request: Request, session_id: str):
    session = service.read(request, session_id)
    session["usable"] = False
    if session["status"] == "active":
        try:
            service.check_live(session, require_baseline=True)
            session["usable"] = not bool(session["busy"])
        except ApiError as exc:
            session["blocked_reason"] = exc.message
    return {"success": True, "session": describe(request, session),
            "operations": service.store().history(session_id, session["owner_id"])}


@router.post("/api/devices/debug-sessions/{session_id}/approve")
def approve_session(request: Request, session_id: str, background: BackgroundTasks):
    session, work = service.approve(request, session_id)
    if work:
        background.add_task(service.execute_install, work)
    return {"success": True, "session": describe(request, service.store().get(session_id, session["owner_id"]))}


@router.post("/api/devices/debug-sessions/{session_id}/revoke")
def revoke_session(request: Request, session_id: str):
    return {"success": True, "session": describe(request, service.revoke(request, session_id))}


@router.post("/api/devices/debug-sessions/{session_id}/actions")
async def debug_action(request: Request, session_id: str, body: DebugAction,
                       _user=Depends(require_permission("devices.use_leased"))):
    return {"success": True, "data": await asyncio.to_thread(service.action, request, session_id, body)}


@router.get("/api/devices/debug-sessions/{session_id}/review", response_class=HTMLResponse)
def review_session(request: Request, session_id: str):
    return HTMLResponse('''<!doctype html><html lang="zh-CN"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>调试会话授权</title><link rel="stylesheet" href="/static/css/device-deployment-review.css">
''' + LOGIN_ASSETS + '''
<script src="/static/js/pages/device-debug-review.js" defer></script></head>
<body><main><h1>调试会话授权</h1><p>请登录发起申请的 Agent 所属平台账号。批准后，Agent 可在以下设备和应用范围内连续调试，直到到期或撤销。</p>
<button id="review-login-open" type="button">登录 / 切换审批账号</button>
<dl id="details"></dl><p id="status" role="status">正在读取调试请求…</p>
<button id="approve" disabled>批准调试会话</button><button id="revoke" disabled>撤销授权</button>
<a href="/">返回平台</a><h2>最近操作</h2><ol id="operations"></ol>
</main>''' + LOGIN_PANEL + '''</body></html>''')
