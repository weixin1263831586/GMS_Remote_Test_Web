"""Redmine 凭据端点（从 api.py 拆出，S-1 拆分）。

`GET /config/credentials` 报告配置状态；`POST /config/credentials`
human-only 保存（agent token 不得写凭据，写入记
安全审计）。复用 api.py 的 helper（owner 推导 / 统计缓存失效）。
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from features.redmine import api as redmine_api
from features.redmine.api import (
    _owner_id_from_request,
    get_redmine_config_for_request,
)


router = APIRouter(prefix="/api/redmine-agent")


@router.get("/config/credentials")
async def get_credentials_status(request: Request):
    """报告登录用户的 Redmine 凭据是否已配置（不回传明文）。

    凭据统一落盘到 configs/config_runtime.json，与统计端点读取的配置一致。
    API Key 只报告是否存在，绝不回传内容。
    """
    manager = get_redmine_config_for_request(request)
    creds = manager.load_redmine_credentials() or {}
    has_api_key = bool(getattr(manager, "load_redmine_api_key", lambda: "")())
    return {"success": True, "data": {"configured": bool(creds.get("password")) or has_api_key,
                                       "username": creds.get("username", ""),
                                       "api_key_configured": has_api_key}}


@router.post("/config/credentials")
async def save_credentials(request: Request):
    """保存 Redmine 凭据到登录用户的运行时配置（human-only）。

    安全边界：只接受人工会话。Agent Service
    Token 与该账号共享 owner 存储，凭据必须由人掌握——agent 调用直接
    403 并返回修复指引；写入成功记入安全审计链（不含凭据内容）。
    """
    from fastapi import HTTPException

    from features.auth import require_human_principal_when_auth_required

    try:
        user = require_human_principal_when_auth_required(request)
    except HTTPException as exc:
        detail = getattr(exc, "detail", None)
        if isinstance(detail, dict) and detail.get("agent_forbidden"):
            # 依赖层只报“被拒”；这里补上自助修复路径：
            # agent 与 enroll 账号共享 owner 存储，人在 Web UI 配置即可。
            return JSONResponse(
                status_code=403,
                content={
                    "success": False,
                    "error": (
                        "凭据写入只允许人工会话（Agent token 被拒绝）。请由 enroll 该 agent 的账号"
                        "在 Web UI『设置』页配置 Redmine 凭据；agent 与该账号共享 owner 存储，"
                        "配置后即可读取。可用 gms-rt-redmine-credentials-status 验证。"
                    ),
                    "detail": {
                        "message": "Redmine credentials are managed by human sessions only",
                        "agent_forbidden": True,
                        "remediation": "configure credentials via the Web UI settings page of the enrolling account",
                    },
                },
            )
        raise
    body = await request.json()
    username = str(body.get("username") or "").strip()
    password = str(body.get("password") or "")
    api_key = str(body.get("api_key") or "").strip()
    if not (username and password) and not api_key:
        return JSONResponse(status_code=400, content={"success": False, "error": "需要用户名/密码或 API Key"})
    manager = get_redmine_config_for_request(request)
    if username and password and not manager.save_redmine_credentials(username, password):
        return JSONResponse(status_code=500, content={"success": False, "error": "保存凭据失败"})
    if api_key or "api_key" in body:
        if not getattr(manager, "save_redmine_api_key", lambda _k: False)(api_key):
            if api_key:
                return JSONResponse(status_code=500, content={"success": False, "error": "保存 API Key 失败"})
    redmine_api._clear_stats_caches()
    _audit_credentials_write(request, user)
    return {"success": True}


def _audit_credentials_write(request: Request, user) -> None:
    """凭据写入属于安全敏感事件：记录谁/何时/哪个 owner，绝不记录内容。"""
    try:
        from foundation.security_audit import security_audit_logger

        security_audit_logger.log_event({
            "action_type": "api",
            "source": "web",
            "operation": "save_redmine_credentials",
            "method": request.method,
            "path": request.url.path,
            "status_code": 200,
            "client_id": getattr(user, "id", "") or "",
            "username": getattr(user, "username", "") or "",
            "auth_method": getattr(request.state, "auth_method", "session"),
            "owner_id": _owner_id_from_request(request),
            "secret_material": "redacted",
        })
    except Exception:  # 审计失败不能阻断凭据保存主流程
        import logging

        logging.getLogger(__name__).warning(
            "Failed to write redmine credentials audit event", exc_info=True
        )
