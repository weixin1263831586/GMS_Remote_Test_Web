"""Device lock facade and identity-aware display-name resolver.

The lock implementation and singleton live in
:mod:`foundation.device_locks`; this module adds the display-name resolver
built from platform identity (users / auth).
"""

from __future__ import annotations

import contextlib

from features.auth import auth_service
from foundation.device_locks import (
    DeviceLockManager,
    configure_display_name_resolver,
    device_lock_manager,
)


def devices_display_name_resolver(client_id: str, username: str | None) -> str:
    """Resolve a lock owner's display name from user identity."""
    cleaned = str(username or "").strip()
    with contextlib.suppress(Exception):
        from features.users import resolve_client_display_id

        return resolve_client_display_id(client_id, cleaned)
    if cleaned and cleaned not in {"unknown", client_id}:
        return cleaned
    with contextlib.suppress(Exception):
        for user in auth_service.list_users():
            if str(user.get("id") or "") == str(client_id):
                return str(user.get("username") or user.get("display_name") or client_id)
    return cleaned or client_id


__all__ = [
    "DeviceLockManager",
    "configure_display_name_resolver",
    "device_lock_manager",
    "devices_display_name_resolver",
]
