from __future__ import annotations

import hmac
import ipaddress

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from foundation.responses import error_response
from foundation.config import config_manager

from .access import (
    get_authenticated_user,
    require_elevated_admin,
    require_role,
)
from .request_security import authentication_required, secure_cookies_enabled
from .service import (
    AUTH_COOKIE_NAME,
    ROLE_PERMISSIONS,
    CurrentUser,
    auth_service,
)


router = APIRouter(prefix="/api/auth")


def _set_session_cookie(response: JSONResponse, token: str) -> None:
    response.set_cookie(
        AUTH_COOKIE_NAME,
        token,
        # Keep this as a browser-session cookie. The server still enforces
        # the absolute/idle session limits in SQLite, while closing the
        # browser discards the cookie and requires authentication again.
        httponly=True,
        samesite="lax",
        secure=secure_cookies_enabled(),
        path="/",
    )


def _request_source_ip(request: Request) -> str:
    return str(request.client.host if request.client else "unknown")


def _rate_limit_response(retry_after: int) -> JSONResponse:
    response = error_response("登录尝试过于频繁，请稍后重试", status_code=429)
    response.headers["Retry-After"] = str(max(1, retry_after))
    return response


def _authenticate_client_ssh_user(
    username: str,
    password: str,
) -> CurrentUser | None:
    """Authenticate a client with a host-scoped SSH password.

    Client accounts are separate from platform administrators. The configured
    host-scoped SSH credential is preferred. For an IP-only login, the known
    client-host username is used and the supplied password is verified by an
    actual SSH ``whoami`` call when no encrypted credential is stored yet.
    """
    login_name = str(username or "").strip()
    if "@" in login_name:
        client_user, client_host = login_name.rsplit("@", 1)
    else:
        client_user = ""
        client_host = login_name
    try:
        ipaddress.ip_address(client_host)
    except ValueError:
        return None

    if not client_user:
        configured_hosts = config_manager.load_config().get("client_hosts") or {}
        client_user = str(configured_hosts.get(client_host) or "").strip()
    if not client_user:
        return None

    canonical_username = f"{client_user}@{client_host}"
    expected = config_manager.find_device_host_password(canonical_username)
    if not expected:
        # The first login may be the moment the host password is supplied. Use
        # it for a real SSH whoami check; never accept an unverified password.
        from features.users.sessions import client_manager

        success, detected_user, _error = client_manager.detect_username(
            client_host,
            client_user,
            str(password or ""),
        )
        if not success or not detected_user:
            return None
        canonical_username = f"{detected_user}@{client_host}"
    elif not hmac.compare_digest(str(expected), str(password or "")):
        return None

    username = canonical_username
    existing = auth_service.get_enabled_user(username)
    if existing and existing.role != "user":
        return None
    if not existing and auth_service.user_exists(username):
        # A disabled client account must be re-enabled by an administrator;
        # a valid SSH password must not bypass that control.
        return None
    return existing or auth_service.create_client_user(username)


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
            "auth_required": authentication_required(),
            "setup_required": auth_service.setup_required(),
            "user": user.as_dict() if user else None,
            "elevated": bool(elevated_until),
            "elevated_until": elevated_until,
        }
    )


@router.post("/elevation/reset")
async def auth_elevation_reset(request: Request):
    """Drop admin verification when a new browser tab session is opened."""
    user = get_authenticated_user(request)
    if not user:
        return error_response("请先登录", status_code=401)
    token = request.cookies.get(AUTH_COOKIE_NAME)
    auth_service.clear_elevation(token)
    return JSONResponse(content={"success": True, "elevated": False})


@router.post("/elevate")
async def auth_elevate(request: Request, req: dict):
    """Re-authenticate as admin to unlock sensitive operations.

    Verifies the supplied credentials belong to an admin, then stamps an
    elevation onto the caller's current session that lasts for the rest of
    that session. In anonymous development mode this is also the point where
    an admin session is created; ordinary page loading remains anonymous while
    sensitive actions still require credentials.
    """
    current_user = get_authenticated_user(request)
    anonymous_step_up = current_user is None and not authentication_required()
    if current_user is None and not anonymous_step_up:
        return error_response("请先登录", status_code=401)
    username = str(req.get("username", "")).strip()

    source_ip = _request_source_ip(request)
    retry_after = auth_service.auth_retry_after("elevate", username, source_ip)
    if retry_after:
        return _rate_limit_response(retry_after)

    admin = auth_service.authenticate(
        username,
        str(req.get("password", "")),
    )
    if not admin or admin.role != "admin":
        retry_after = auth_service.record_auth_failure(
            "elevate",
            username,
            source_ip,
        )
        if retry_after:
            return _rate_limit_response(retry_after)
        return error_response("管理员凭证无效", status_code=403)
    auth_service.clear_auth_failures("elevate", username, source_ip)

    token = request.cookies.get(AUTH_COOKIE_NAME)
    issued_session = False
    if anonymous_step_up:
        token = auth_service.create_session(admin.id)
        issued_session = True
    if not token:
        return error_response("当前会话无效，请重新登录", status_code=401)
    # 二次认证状态绑定当前认证会话。
    if not auth_service.elevate_session(token, admin):
        return error_response("无法提权当前会话", status_code=400)

    elevated_until = auth_service.get_elevated_until(token)
    response = JSONResponse(
        content={
            "success": True,
            "elevated": True,
            "elevated_until": elevated_until,
            "admin_verified": True,
            "user": current_user.as_dict() if current_user else admin.as_dict(),
            "client_id": current_user.id if current_user else admin.id,
        }
    )
    if issued_session:
        _set_session_cookie(response, token)
    return response


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
async def auth_login(request: Request, req: dict):
    username = str(req.get("username", "")).strip()
    source_ip = _request_source_ip(request)
    retry_after = auth_service.auth_retry_after("login", username, source_ip)
    if retry_after:
        return _rate_limit_response(retry_after)

    user = auth_service.authenticate(
        username,
        str(req.get("password", "")),
    )
    if not user:
        from fastapi.concurrency import run_in_threadpool

        user = await run_in_threadpool(
            _authenticate_client_ssh_user,
            username,
            str(req.get("password", "")),
        )
    if not user:
        retry_after = auth_service.record_auth_failure(
            "login",
            username,
            source_ip,
        )
        if retry_after:
            return _rate_limit_response(retry_after)
        return error_response("用户名或密码错误", status_code=401)
    auth_service.clear_auth_failures("login", username, source_ip)

    token = auth_service.create_session(user.id)
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
    response.delete_cookie(
        AUTH_COOKIE_NAME,
        path="/",
        httponly=True,
        secure=secure_cookies_enabled(),
        samesite="lax",
    )
    return response


@router.get("/roles")
async def auth_roles(
    _admin: CurrentUser = Depends(require_role("admin")),
):
    return {
        "success": True,
        "roles": {
            role: sorted(permissions)
            for role, permissions in ROLE_PERMISSIONS.items()
        },
    }


@router.get("/users")
async def auth_users(
    _admin: CurrentUser = Depends(require_role("admin")),
):
    users = auth_service.list_users()
    for user in users:
        user["permissions"] = sorted(
            ROLE_PERMISSIONS.get(str(user.get("role") or ""), frozenset())
        )
    return {"success": True, "users": users}


@router.post("/users")
async def auth_create_user(
    req: dict,
    _admin: CurrentUser = Depends(require_elevated_admin),
):
    try:
        user = auth_service.create_user(
            str(req.get("username") or ""),
            str(req.get("password") or ""),
            role=str(req.get("role") or "user"),
            display_name=str(req.get("display_name") or ""),
        )
    except ValueError as exc:
        return error_response(str(exc), status_code=400)
    return {"success": True, "user": user.as_dict()}


@router.patch("/users/{user_id}")
async def auth_update_user(
    user_id: str,
    req: dict,
    _admin: CurrentUser = Depends(require_elevated_admin),
):
    if "disabled" in req and not isinstance(req["disabled"], bool):
        return error_response("disabled 必须是布尔值", status_code=400)
    try:
        user = auth_service.update_user(
            user_id,
            role=(str(req["role"]) if "role" in req else None),
            display_name=(
                str(req["display_name"])
                if "display_name" in req
                else None
            ),
            disabled=req.get("disabled"),
        )
    except ValueError as exc:
        return error_response(str(exc), status_code=400)
    return {"success": True, "user": user.as_dict()}


@router.post("/users/{user_id}/reset-password")
async def auth_reset_user_password(
    user_id: str,
    req: dict,
    _admin: CurrentUser = Depends(require_elevated_admin),
):
    try:
        auth_service.set_user_password(user_id, str(req.get("password") or ""))
    except ValueError as exc:
        return error_response(str(exc), status_code=400)
    return {"success": True, "sessions_revoked": True}


@router.post("/users/{user_id}/revoke-sessions")
async def auth_revoke_user_sessions(
    user_id: str,
    _admin: CurrentUser = Depends(require_elevated_admin),
):
    revoked = auth_service.revoke_user_sessions(user_id)
    return {"success": True, "revoked_sessions": revoked}
