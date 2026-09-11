"""FastAPI principal and authorization dependencies."""

from __future__ import annotations

from fastapi import HTTPException, Request

from .request_security import authentication_required
from .service import AUTH_COOKIE_NAME, CurrentUser, auth_service


def get_authenticated_user(request: Request) -> CurrentUser | None:
    user = getattr(request.state, "current_user", None)
    if isinstance(user, CurrentUser):
        return user
    # Agent Service Token (2026-09-08 audit §二): Bearer credentials from the
    # Authorization header. An invalid/unknown Bearer token fails closed —
    # it must not silently fall back to cookie auth or dev-mode anonymity
    # (request.state.credentials_rejected marks the difference).
    auth_header = str(request.headers.get("Authorization") or "")
    if auth_header.startswith("Bearer "):
        principal, record = auth_service.get_agent_token_principal(
            auth_header[len("Bearer "):].strip()
        )
        request.state.agent_token_record = record
        if principal is None:
            request.state.auth_method = "invalid_agent_token"
            request.state.credentials_rejected = True
            return None
        request.state.current_user = principal
        request.state.auth_method = "agent_token"
        return principal
    request.state.auth_method = "session"
    token = request.cookies.get(AUTH_COOKIE_NAME)
    user = auth_service.get_user_for_token(token)
    if user:
        request.state.current_user = user
    return user


def require_authenticated_user(request: Request) -> CurrentUser:
    user = get_authenticated_user(request)
    if not user:
        # Rejected credentials (invalid agent token) must 401/403 even in
        # dev mode — never downgrade to an anonymous principal.
        if getattr(request.state, "credentials_rejected", False):
            raise HTTPException(
                status_code=401, detail="Invalid agent credentials"
            )
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


def require_human_principal_when_auth_required(
    request: Request,
) -> CurrentUser | None:
    """Authenticate like today, but refuse Agent Service Token principals.

    The MCP tool allowlist is not a security boundary — an agent
    token can call REST endpoints directly with its Bearer credential. VPN,
    SSH helpers and suite management are human-operator surfaces: without a
    matching agent scope they must fail closed here, server-side.
    """

    user = require_authenticated_user_when_auth_required(request)
    if user is not None and getattr(
        request.state, "auth_method", None
    ) == "agent_token":
        raise HTTPException(
            status_code=403,
            detail={
                "message": "Agent tokens cannot call this endpoint",
                "agent_forbidden": True,
            },
        )
    return user


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
        if user.has_permission(permission):
            return user
        # 与 require_role 一致：临时管理员验证（elevation）也满足权限检查，
        # 否则 UI 在成功二次验证后仍会收到 Permission denied。
        if is_elevated(request):
            return user
        raise HTTPException(status_code=403, detail="Permission denied")

    return dependency


def require_permission_when_auth_required(permission: str):
    """Require a permission in authenticated deployments, but keep dev anonymous mode."""

    permission_dependency = require_permission(permission)

    def dependency(request: Request) -> CurrentUser | None:
        if not authentication_required():
            return None
        return permission_dependency(request)

    return dependency


def is_elevated(request: Request) -> bool:
    """Return whether this request has a live re-authenticated elevation."""
    # Elevation lives on a human *cookie* session. An
    # agent_token principal must never inherit the elevation of a leftover
    # browser/CLI cookie that happens to ride along in the same request —
    # that would collapse the agent's scope isolation. Bearer and elevation
    # are mutually exclusive by definition.
    if getattr(request.state, "auth_method", None) == "agent_token":
        return False
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


def require_agent_scope(scope: str):
    """Require an agent principal carrying ``scope`` (human roles also pass)."""

    def dependency(request: Request) -> CurrentUser:
        user = require_authenticated_user(request)
        if user.has_permission(scope):
            return user
        raise HTTPException(
            status_code=403,
            detail={
                "message": f"Agent token scope '{scope}' required",
                "scope_required": scope,
            },
        )

    return dependency


def ensure_agent_worker_allowed(request: Request, worker_id: str) -> None:
    """Enforce the token's allowed_workers ACL (no-op for human sessions)."""

    if getattr(request.state, "auth_method", None) != "agent_token":
        return
    record = getattr(request.state, "agent_token_record", None)
    if not auth_service.agent_acl_allows(record, "workers", str(worker_id or "")):
        raise HTTPException(
            status_code=403,
            detail=f"Agent token is not allowed to target worker '{worker_id}'",
        )


def ensure_agent_device_allowed(request: Request, serial: str) -> None:
    """Enforce the token's allowed_devices ACL (no-op for human sessions)."""

    if getattr(request.state, "auth_method", None) != "agent_token":
        return
    record = getattr(request.state, "agent_token_record", None)
    if not auth_service.agent_acl_allows(record, "devices", str(serial or "")):
        raise HTTPException(
            status_code=403,
            detail=f"Agent token is not allowed to target device '{serial}'",
        )
