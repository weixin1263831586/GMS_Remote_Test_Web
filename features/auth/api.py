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
    return JSONResponse(
        content={
            "authenticated": user is not None,
            "setup_required": auth_service.setup_required(),
            "user": user.as_dict() if user else None,
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
