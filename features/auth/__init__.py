from .access import (
    get_authenticated_user,
    is_elevated,
    principal_owner_id,
    require_authenticated_user,
    require_authenticated_user_when_auth_required,
    require_elevated_admin,
    require_elevated_admin_when_auth_required,
    require_permission,
    require_resource_owner,
    require_resource_owner_when_auth_required,
    require_role,
    require_role_when_auth_required,
)
from .api import router
from .request_security import (
    authentication_required,
    csrf_rejection_reason,
    secure_cookies_enabled,
    validate_websocket_request,
    websocket_origin_allowed,
)
from .service import (
    AUTH_COOKIE_NAME,
    ROLE_PERMISSIONS,
    AuthService,
    CurrentUser,
    auth_service,
)


__all__ = [
    "AUTH_COOKIE_NAME",
    "ROLE_PERMISSIONS",
    "AuthService",
    "CurrentUser",
    "auth_service",
    "authentication_required",
    "csrf_rejection_reason",
    "get_authenticated_user",
    "is_elevated",
    "principal_owner_id",
    "require_authenticated_user",
    "require_authenticated_user_when_auth_required",
    "require_elevated_admin",
    "require_elevated_admin_when_auth_required",
    "require_permission",
    "require_resource_owner",
    "require_resource_owner_when_auth_required",
    "require_role",
    "require_role_when_auth_required",
    "router",
    "secure_cookies_enabled",
    "validate_websocket_request",
    "websocket_origin_allowed",
]
