"""Authentication and runtime identity helpers for system WebSockets."""

from __future__ import annotations

import logging

from fastapi import WebSocket

from features.auth import (
    AUTH_COOKIE_NAME,
    CurrentUser,
    auth_service,
    validate_websocket_request,
)
from features.users import format_client_display_id, get_client_ip
from foundation.config import config_manager


logger = logging.getLogger(__name__)


def get_websocket_client_ip(websocket: WebSocket) -> str:
    """Resolve browser client IP for WebSocket requests."""

    return get_client_ip(websocket)


def get_websocket_client_identity(
    websocket: WebSocket,
    path_client_id: str,
    current_user: CurrentUser | None = None,
) -> tuple[str, str, str]:
    """Return the isolated runtime key, display id, and username."""

    user = current_user or auth_service.get_user_for_token(
        websocket.cookies.get(AUTH_COOKIE_NAME),
    )
    client_ip = get_websocket_client_ip(websocket)
    if user:
        display_id = format_client_display_id(user.username, client_ip)
        if path_client_id.startswith("terminal_"):
            return f"{user.id}:{path_client_id}", display_id, user.username
        return user.id, display_id, user.username

    username = "unknown"
    try:
        config = config_manager.load_config()
        username = (
            str((config.get("client_hosts") or {}).get(client_ip) or "").strip()
            or "unknown"
        )
    except Exception:
        username = "unknown"

    display_id = format_client_display_id(username, client_ip)
    if path_client_id.startswith("terminal_"):
        return path_client_id, display_id or path_client_id, username
    resolved_id = display_id or path_client_id or "unknown"
    if path_client_id and path_client_id != resolved_id:
        logger.debug(
            "WebSocket anonymous client_id adjusted: path=%s resolved=%s",
            path_client_id,
            resolved_id,
        )
    return resolved_id, display_id or resolved_id, username


async def authorize_websocket_identity(
    websocket: WebSocket,
    path_client_id: str,
) -> tuple[str, str, str] | None:
    """Validate the handshake and resolve identity, closing rejected clients."""

    current_user, close_code = validate_websocket_request(websocket)
    if close_code:
        await websocket.close(code=close_code)
        return None
    websocket.state.current_user = current_user
    if (
        path_client_id.startswith("terminal_")
        and current_user is not None
        and not auth_service.get_elevated_until(
            websocket.cookies.get(AUTH_COOKIE_NAME)
        )
    ):
        await websocket.close(code=4403)
        return None
    return get_websocket_client_identity(
        websocket,
        path_client_id,
        current_user,
    )
