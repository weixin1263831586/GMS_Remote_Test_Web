from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from foundation.responses import error_response

from .service import (
    AUTH_COOKIE_NAME,
    SESSION_ABSOLUTE_HOURS,
    auth_service,
    get_authenticated_user,
)


router = APIRouter(prefix="/api/auth")


def _set_session_cookie(response: JSONResponse, token: str) -> None:
    response.set_cookie(
        AUTH_COOKIE_NAME,
        token,
        max_age=SESSION_ABSOLUTE_HOURS * 3600,
        httponly=True,
        samesite="lax",
        secure=False,
        path="/",
    )


@router.get("/status")
async def auth_status(request: Request):
    user = get_authenticated_user(request)
    elevated_until = (
        auth_service.get_elevated_until(request.cookies.get(AUTH_COOKIE_NAME))
        if user
        else None
    )
    return JSONResponse(
        content={
            "authenticated": user is not None,
            "setup_required": auth_service.setup_required(),
            "user": user.as_dict() if user else None,
            "elevated": bool(elevated_until),
            "elevated_until": elevated_until,
        }
    )


@router.post("/elevate")
async def auth_elevate(request: Request, req: dict):
    """Re-authenticate as admin to temporarily unlock sensitive operations.

    Verifies the supplied credentials belong to an admin, then stamps a short
    elevation window onto the caller's current session. The session must already
    exist (the user is logged in); elevation just grants admin-level actions
    (remove user / disconnect device) for ``ELEVATION_MINUTES``.
    """
    current = get_authenticated_user(request)
    if not current:
        return error_response("请先登录", status_code=401)

    admin = auth_service.authenticate(
        str(req.get("username", "")),
        str(req.get("password", "")),
    )
    if not admin or admin.role != "admin":
        return error_response("管理员凭证无效", status_code=403)

    token = request.cookies.get(AUTH_COOKIE_NAME)
    if not token or not auth_service.elevate_session(token, admin):
        return error_response("无法提权当前会话", status_code=400)

    elevated_until = auth_service.get_elevated_until(token)
    return JSONResponse(
        content={
            "success": True,
            "elevated": True,
            "elevated_until": elevated_until,
        }
    )


@router.post("/setup")
async def auth_setup(req: dict):
    try:
        user = auth_service.create_initial_admin(
            str(req.get("username", "")),
            str(req.get("password", "")),
            str(req.get("display_name", "")),
        )
        token = auth_service.create_session(user.id)
    except ValueError as exc:
        return error_response(str(exc), status_code=400)

    # 初始管理员登录即视为已提权（整个会话有效），避免敏感操作再弹提权框。
    auth_service.elevate_session(token, user, minutes=SESSION_ABSOLUTE_HOURS * 60)

    response = JSONResponse(
        content={
            "success": True,
            "authenticated": True,
            "setup_required": False,
            "user": user.as_dict(),
            "client_id": user.id,
        }
    )
    _set_session_cookie(response, token)
    return response


@router.post("/login")
async def auth_login(req: dict):
    user = auth_service.authenticate(
        str(req.get("username", "")),
        str(req.get("password", "")),
    )
    if not user:
        return error_response("用户名或密码错误", status_code=401)

    token = auth_service.create_session(user.id)
    # 管理员登录即视为已提权（整个会话有效），做敏感操作时不再弹提权框；
    # 普通用户不受影响（无 admin 权限，敏感端点仍返回 403）。
    if user.role == "admin":
        auth_service.elevate_session(token, user, minutes=SESSION_ABSOLUTE_HOURS * 60)
    response = JSONResponse(
        content={
            "success": True,
            "authenticated": True,
            "setup_required": False,
            "user": user.as_dict(),
            "client_id": user.id,
        }
    )
    _set_session_cookie(response, token)
    return response


@router.post("/logout")
async def auth_logout(request: Request):
    token = request.cookies.get(AUTH_COOKIE_NAME)
    if token:
        auth_service.revoke_session(token)
    response = JSONResponse(content={"success": True})
    response.delete_cookie(AUTH_COOKIE_NAME, path="/")
    return response
