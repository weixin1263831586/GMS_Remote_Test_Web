"""FastAPI principal and authorization dependencies."""

from __future__ import annotations

from fastapi import HTTPException, Request

from .request_security import authentication_required
from .service import AUTH_COOKIE_NAME, CurrentUser, auth_service


def get_authenticated_user(request: Request) -> CurrentUser | None:
    user = getattr(request.state, "current_user", None)
    if isinstance(user, CurrentUser):
        return user
    token = request.cookies.get(AUTH_COOKIE_NAME)
    user = auth_service.get_user_for_token(token)
    if user:
        request.state.current_user = user
    return user


def require_authenticated_user(request: Request) -> CurrentUser:
    user = get_authenticated_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    return user


def require_authenticated_user_when_auth_required(
    request: Request,
) -> CurrentUser | None:
    """Use an authenticated user when required, while preserving dev mode."""

    user = get_authenticated_user(request)
    if user:
        return user
    if not authentication_required():
        return None
    raise HTTPException(status_code=401, detail="Authentication required")


def principal_owner_id(request: Request) -> str:
    """Return the immutable account id used by newly-created resources."""

    return require_authenticated_user(request).id


def principal_display_name(request: Request) -> str:
    """Return the human-readable username for device claim records."""

    return require_authenticated_user(request).username


def require_resource_owner(
    request: Request,
    owner_id: object,
    *,
    not_found_detail: str = "resource not found",
) -> CurrentUser:
    """Enforce an owner boundary without revealing cross-user identifiers."""

    user = require_authenticated_user(request)
    if user.role != "admin" and str(owner_id or "") != user.id:
        raise HTTPException(status_code=404, detail=not_found_detail)
    return user


def require_resource_owner_when_auth_required(
    request: Request,
    owner_id: object,
    *,
    not_found_detail: str = "resource not found",
) -> CurrentUser | None:
    """Enforce an owner boundary in authenticated mode, allow anonymous in dev mode.

    Mirrors require_authenticated_user_when_auth_required: when authentication is
    not globally enforced (internal/dev deployments), anonymous callers may access
    shared resources such as cancelling a running test.
    """

    user = get_authenticated_user(request)
    if user:
        if user.role != "admin" and str(owner_id or "") != user.id:
            raise HTTPException(status_code=404, detail=not_found_detail)
        return user
    if not authentication_required():
        return None
    raise HTTPException(status_code=401, detail="Authentication required")


def require_role(*roles: str):
    allowed = set(roles)

    def dependency(request: Request) -> CurrentUser:
        user = require_authenticated_user(request)
        if user.role in allowed:
            return user
        # Temporary administrator verification intentionally does not mutate
        # the client's account role. Legacy admin-only dependencies must honor
        # that elevated session, otherwise the UI receives a plain
        # "Permission denied" after successful administrator verification.
        if "admin" in allowed:
            if is_elevated(request):
                return user
            raise HTTPException(
                status_code=403,
                detail={
                    "message": "Elevation required",
                    "elevation_required": True,
                },
            )
        raise HTTPException(status_code=403, detail="Permission denied")

    return dependency


def require_role_when_auth_required(*roles: str):
    """Require a role in authenticated deployments, but keep dev anonymous mode usable."""

    role_dependency = require_role(*roles)

    def dependency(request: Request) -> CurrentUser | None:
        if not authentication_required():
            return None
        return role_dependency(request)

    return dependency


def require_permission(permission: str):
    def dependency(request: Request) -> CurrentUser:
        user = require_authenticated_user(request)
        if not user.has_permission(permission):
            raise HTTPException(status_code=403, detail="Permission denied")
        return user

    return dependency


def is_elevated(request: Request) -> bool:
    """Return whether this request has a live re-authenticated elevation."""

    if getattr(request.state, "is_elevated", None) is not None:
        return bool(request.state.is_elevated)
    token = request.cookies.get(AUTH_COOKIE_NAME)
    elevated_until = auth_service.get_elevated_until(token)
    request.state.is_elevated = bool(elevated_until)
    return bool(elevated_until)


def require_elevated_admin(request: Request) -> CurrentUser:
    """Require a session verified by an administrator.

    The authenticated user may remain an ordinary client. The separate admin
    verification is stored on that same session and never changes its role.
    """

    user = require_authenticated_user(request)
    if not is_elevated(request):
        raise HTTPException(
            status_code=403,
            detail={
                "message": "Elevation required",
                "elevation_required": True,
            },
        )
    return user


def require_elevated_admin_when_auth_required(request: Request) -> CurrentUser | None:
    """Require admin verification in production while preserving dev mode."""

    if not authentication_required():
        return None
    return require_elevated_admin(request)
