"""Authenticated principal type shared across the auth feature.

Split from ``constants.py`` (which now re-exports it for back-compat) to keep
that module under its migration line limit. Leaf module, like ``constants.py``:
no imports from ``service.py`` / ``agent_tokens.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class CurrentUser:
    id: str
    username: str
    role: str
    display_name: str = ""
    # Agent Service Token scopes granted to this principal beyond the role
    # defaults. Human principals keep this empty; role-based admin gates are
    # decided by ``role`` and never lifted by scopes.
    extra_permissions: frozenset[str] = frozenset()
    # Actor vs resource owner (ADR 0010): ``id`` is the ACTING principal
    # (audit identity). ``resource_owner_id`` is the account that owns
    # resources created through this principal. Human principals: both are
    # the account id. Agent principals: ``id`` is the synthetic
    # ``agent:<token_id>`` (stable for audit), ``resource_owner_id`` is the
    # token's enrolling account so resources survive token rotation
    # instead of becoming orphans.
    resource_owner_id: str = ""

    def __post_init__(self) -> None:
        if not self.resource_owner_id:
            # dataclass(frozen=True) — use object.__setattr__ for the default.
            object.__setattr__(self, "resource_owner_id", self.id)

    @property
    def actor_id(self) -> str:
        """Audit identity of the acting principal (alias of ``id``)."""
        return self.id

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "username": self.username,
            "role": self.role,
            "display_name": self.display_name,
            "permissions": sorted(self.effective_permissions()),
            "resource_owner_id": self.resource_owner_id,
        }

    def effective_permissions(self) -> frozenset[str]:
        from .constants import ROLE_PERMISSIONS

        return ROLE_PERMISSIONS.get(self.role, frozenset()) | set(self.extra_permissions)

    def has_permission(self, permission: str) -> bool:
        return "*" in self.effective_permissions() or permission in self.effective_permissions()
