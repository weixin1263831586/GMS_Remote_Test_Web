"""FastAPI principal and authorization dependencies."""

from __future__ import annotations

from fastapi import HTTPException, Request

from .request_security import authentication_required
from .service import AUTH_COOKIE_NAME, CurrentUser, auth_service


def get_authenticated_user(request: Request) -> CurrentUser | None:
    user = getattr(request.state, "current_user", None)
    if isinstance(user, CurrentUser):
        return user
    # Bearer credentials from the Authorization header, in priority order:
    # (1) a short-TTL machine capability token (ADR 0012) minted in-process
    # by the automation worker, (2) an Agent Service Token (ADR 0006).
    # An invalid/unknown Bearer token fails closed — it must not silently
    # fall back to cookie auth or dev-mode anonymity
    # (request.state.credentials_rejected marks the difference).
    auth_header = str(request.headers.get("Authorization") or "")
    if auth_header.startswith("Bearer "):
        credential = auth_header[len("Bearer "):].strip()
        if credential.startswith("gmscap_v1_"):
            from .authority import verify_capability_token

            principal = verify_capability_token(credential)
            if principal is None:
                request.state.auth_method = "invalid_capability_token"
                request.state.credentials_rejected = True
                return None
            request.state.current_user = principal
            request.state.auth_method = "machine_authority"
            return principal
        principal, record = auth_service.get_agent_token_principal(credential)
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


def require_human_principal(request: Request) -> CurrentUser:
    """Refuse agent/machine principals on always-authenticated endpoints.

    Mutation surfaces (e.g. ATS run create/cancel/retry) already require an
    authenticated principal; this adds the human-only rule of ADR 0012 on
    top, so neither agent tokens nor machine capabilities pass.
    """

    user = require_authenticated_user(request)
    if getattr(request.state, "auth_method", None) in {
        "agent_token", "machine_authority"
    }:
        raise HTTPException(
            status_code=403,
            detail={
                "message": "Agent tokens cannot call this endpoint",
                "agent_forbidden": True,
            },
        )
    return user


def require_human_principal_when_auth_required(
    request: Request,
) -> CurrentUser | None:
    """Authenticate like today, but refuse non-human principals.

    The MCP tool allowlist is not a security boundary — an agent
    token can call REST endpoints directly with its Bearer credential. VPN,
    SSH helpers and suite management are human-operator surfaces: without a
    matching agent scope they must fail closed here, server-side.
    Machine capability principals (ADR 0012) are likewise refused: the
    automation worker must not create chained runs through its own stage
    authority.
    """

    user = require_authenticated_user_when_auth_required(request)
    if user is not None and getattr(
        request.state, "auth_method", None
    ) in {"agent_token", "machine_authority"}:
        raise HTTPException(
            status_code=403,
            detail={
                "message": "Agent tokens cannot call this endpoint",
                "agent_forbidden": True,
            },
        )
    return user


def principal_owner_id(request: Request) -> str:
    """Return the account id that should OWN newly-created resources.

    Actor vs resource owner (ADR 0010): an agent service token creates
    resources on behalf of its enrolling account, so the resource owner is
    the token's ``owner_user_id`` — not the synthetic ``agent:<token_id>``.
    Revoking/rotating the token then keeps those resources visible to the
    account instead of orphaning them. Audit trails that need the acting
    principal record ``user.id`` / ``user.actor_id`` alongside.
    """

    return require_authenticated_user(request).resource_owner_id


def principal_owner_id_when_auth_required(request: Request) -> str | None:
    """Resource-owner id when authenticated, anonymous passthrough in dev mode.

    Mirrors ``principal_owner_id`` for endpoints that stay usable without a
    global login (dev deployments): anonymous callers keep their explicit
    ``owner_id_from_request`` value, authenticated callers always own
    through the account id (ADR 0010) — never the synthetic actor id.
    """

    user = get_authenticated_user(request)
    if user is not None:
        return user.resource_owner_id
    if not authentication_required():
        return None
    raise HTTPException(status_code=401, detail="Authentication required")


def principal_actor_id(request: Request) -> str:
    """Return the ACTING principal id for audit attribution."""

    return require_authenticated_user(request).actor_id


def principal_display_name(request: Request) -> str:
    """Return the human-readable username for device claim records."""

    return require_authenticated_user(request).username


def require_resource_owner(
    request: Request,
    owner_id: object,
    *,
    not_found_detail: str = "resource not found",
) -> CurrentUser:
    """Enforce an owner boundary without revealing cross-user identifiers.

    Ownership is compared against the RESOURCE owner account (ADR 0010):
    an agent token may access resources owned by its enrolling account.
    """

    user = require_authenticated_user(request)
    if user.role != "admin" and str(owner_id or "") != user.resource_owner_id:
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
        if user.role != "admin" and str(owner_id or "") != user.resource_owner_id:
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
    # Elevation lives on a human *cookie* session. A Bearer principal
    # (agent token or machine capability, valid or not) must never inherit
    # the elevation of a leftover browser/CLI cookie that happens to ride
    # along in the same request — that would let a machine capability
    # combine with a human admin elevation and amplify its authority far
    # beyond the snapshotted run plan (ADR 0010 / ADR 0012). Bearer and
    # elevation are mutually exclusive by definition.
    if getattr(request.state, "auth_method", None) in {
        "agent_token",
        "machine_authority",
        "invalid_agent_token",
        "invalid_capability_token",
    }:
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
