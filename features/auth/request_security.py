from __future__ import annotations

import hmac
import os
from urllib.parse import urlsplit

from starlette.requests import HTTPConnection

from .service import AUTH_COOKIE_NAME, CurrentUser, auth_service


SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}


def env_flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def authentication_required() -> bool:
    """返回是否全局强制浏览器和 API 认证。"""

    environment = os.getenv("GMS_ENV", "development").strip().lower()
    return env_flag("GMS_AUTH_REQUIRED", environment == "production")


def secure_cookies_enabled() -> bool:
    environment = os.getenv("GMS_ENV", "development").strip().lower()
    return env_flag("GMS_SECURE_COOKIES", environment == "production")


def bootstrap_token() -> str:
    """一次性初始化令牌；设置后 /api/auth/setup 必须携带它。"""
    return os.getenv("GMS_BOOTSTRAP_TOKEN", "").strip()


def bootstrap_token_required() -> bool:
    """Return whether first-run setup must supply the bootstrap token."""
    return bool(bootstrap_token())


def bootstrap_token_matches(connection: HTTPConnection) -> bool:
    """Constant-time comparison of the X-GMS-Bootstrap-Token header."""
    expected = bootstrap_token()
    if not expected:
        return True
    supplied = str(connection.headers.get("x-gms-bootstrap-token") or "")
    return hmac.compare_digest(expected, supplied)


def _normalized_origin(value: str) -> str:
    try:
        parsed = urlsplit(str(value or "").strip())
    except ValueError:
        return ""
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return ""
    default_port = 443 if parsed.scheme == "https" else 80
    port = parsed.port or default_port
    suffix = "" if port == default_port else f":{port}"
    return f"{parsed.scheme}://{parsed.hostname.lower()}{suffix}"


def _allowed_origins(connection: HTTPConnection) -> set[str]:
    host = str(connection.headers.get("host") or "").strip()
    scheme = str(connection.url.scheme or "http").lower()
    if scheme == "ws":
        scheme = "http"
    elif scheme == "wss":
        scheme = "https"
    expected = _normalized_origin(f"{scheme}://{host}")
    configured = {
        _normalized_origin(item)
        for item in os.getenv("GMS_ALLOWED_ORIGINS", "").split(",")
        if item.strip()
    }
    configured.discard("")
    if expected:
        configured.add(expected)
    return configured


def same_origin(connection: HTTPConnection, candidate: str) -> bool:
    normalized = _normalized_origin(candidate)
    return bool(normalized and normalized in _allowed_origins(connection))


def csrf_rejection_reason(connection: HTTPConnection) -> str:
    """Return an error string when an unsafe browser request is cross-site.

    Modern browsers always send ``Origin`` or Fetch Metadata for unsafe requests.
    Non-browser API clients may omit both and are not CSRF-capable, so they remain
    usable. Applying the origin check before login also protects first-run setup
    from being claimed by a malicious site in the administrator's browser.
    """

    if connection.scope.get("method", "GET").upper() in SAFE_METHODS:
        return ""

    fetch_site = str(connection.headers.get("sec-fetch-site") or "").lower()
    if fetch_site == "cross-site":
        return "Cross-site request blocked"

    origin = str(connection.headers.get("origin") or "").strip()
    if origin:
        return "" if same_origin(connection, origin) else "Request origin is not allowed"

    referer = str(connection.headers.get("referer") or "").strip()
    if referer:
        return "" if same_origin(connection, referer) else "Request referer is not allowed"

    if fetch_site:
        return "Browser request is missing Origin and Referer"
    return ""


def websocket_origin_allowed(connection: HTTPConnection) -> bool:
    """Validate browser WebSocket origins while preserving non-browser clients."""

    origin = str(connection.headers.get("origin") or "").strip()
    if not origin:
        return not str(connection.headers.get("sec-fetch-site") or "").strip()
    return same_origin(connection, origin)


def validate_websocket_request(
    connection: HTTPConnection,
) -> tuple[CurrentUser | None, int | None]:
    """Authenticate a WebSocket handshake and return an optional close code."""

    if not websocket_origin_allowed(connection):
        return None, 4403
    user = auth_service.get_user_for_token(
        connection.cookies.get(AUTH_COOKIE_NAME),
    )
    if authentication_required() and not user:
        return None, 4401
    return user, None
