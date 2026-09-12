"""Agent Service Token / Approval Token / Enrollment services.

Extracted from service.py so the auth service stays under
the reviewable-size limit. AgentTokenServiceMixin is mixed into AuthService
and reuses its _connect/_lock/hash_token helpers.
"""

from __future__ import annotations

import secrets
import sqlite3  # noqa: F401  (kept for parity with service.py helpers)
from datetime import datetime, timedelta, timezone
from typing import Any

from .constants import (
    AGENT_ROLE,
    AGENT_SCOPES,
    DEFAULT_AGENT_TOKEN_DAYS,
    CurrentUser,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _to_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _from_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _last_seen_recent(last_seen_at: str | None, now: datetime) -> bool:
    if not last_seen_at:
        return False
    try:
        return (now - _from_iso(last_seen_at)) < timedelta(seconds=60)
    except ValueError:
        return False


class AgentTokenServiceMixin:
    """Agent token / approval / enrollment storage; mixed into AuthService."""

    # ------------------------------------------------------------------
    # Agent Service Tokens (ADR 0006)
    # ------------------------------------------------------------------

    @staticmethod
    def normalize_scopes(scopes: list[str] | str | None) -> str:
        """Validate and normalize a scope list into a comma-joined string."""
        if scopes is None:
            return ""
        if isinstance(scopes, str):
            scopes = [part.strip() for part in scopes.split(",")]
        cleaned: list[str] = []
        for scope in scopes:
            name = str(scope or "").strip()
            if not name:
                continue
            if name not in AGENT_SCOPES:
                raise ValueError(f"未知权限范围: {name}")
            if name not in cleaned:
                cleaned.append(name)
        return ",".join(cleaned)

    @staticmethod
    def _normalize_acl(value: str | list[str] | None) -> str:
        """Normalize a worker/device ACL into '*' or a comma-joined list."""
        if value is None:
            return "*"
        items = (
            [part.strip() for part in str(value).split(",")]
            if isinstance(value, str)
            else [str(part).strip() for part in value]
        )
        items = [item for item in items if item]
        if not items or "*" in items:
            return "*"
        return ",".join(items)

    def create_agent_token(
        self,
        *,
        name: str,
        owner: CurrentUser,
        scopes: list[str] | str | None,
        allowed_workers: str | list[str] | None = None,
        allowed_devices: str | list[str] | None = None,
        expires_days: int | None = DEFAULT_AGENT_TOKEN_DAYS,
    ) -> dict[str, Any]:
        """Create one agent service token; the raw token is returned once."""
        token_name = str(name or "").strip()
        if not token_name or len(token_name) > 64:
            raise ValueError("token 名称必填且不超过 64 个字符")
        scope_str = self.normalize_scopes(scopes)
        workers = self._normalize_acl(allowed_workers)
        devices = self._normalize_acl(allowed_devices)
        token = secrets.token_urlsafe(32)
        token_id = f"agt_{secrets.token_hex(8)}"
        now = _utcnow()
        expires_at = (
            _to_iso(now + timedelta(days=max(1, int(expires_days))))
            if expires_days is not None
            else None
        )
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO platform_agent_tokens (
                    id, token_hash, name, owner_user_id, scopes,
                    allowed_workers, allowed_devices,
                    created_at, expires_at, revoked_at, last_used_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL)
                """,
                (
                    token_id,
                    self.hash_token(token),
                    token_name,
                    owner.id,
                    scope_str,
                    workers,
                    devices,
                    _to_iso(now),
                    expires_at,
                ),
            )
            conn.commit()
        return {
            "id": token_id,
            "name": token_name,
            "token": token,  # 只在创建时返回一次
            "scopes": [s for s in scope_str.split(",") if s],
            "allowed_workers": workers,
            "allowed_devices": devices,
            "expires_at": expires_at,
        }

    def list_agent_tokens(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, name, owner_user_id, scopes, allowed_workers,
                       allowed_devices, created_at, expires_at, revoked_at,
                       last_used_at
                FROM platform_agent_tokens
                ORDER BY created_at
                """
            ).fetchall()
        tokens: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["scopes"] = [s for s in str(row["scopes"] or "").split(",") if s]
            tokens.append(item)
        return tokens

    def revoke_agent_token(self, token_id: str) -> bool:
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                "UPDATE platform_agent_tokens SET revoked_at = ? "
                "WHERE id = ? AND revoked_at IS NULL",
                (_to_iso(_utcnow()), token_id),
            )
            conn.commit()
            return cursor.rowcount > 0

    def get_agent_token_principal(
        self, token: str
    ) -> tuple[CurrentUser | None, dict[str, Any] | None]:
        """Resolve a Bearer agent token into its principal and record.

        Returns (principal, record); principal is None when the token is
        unknown, revoked, expired, or its owner account is disabled.
        """
        if not token:
            return None, None
        token_hash = self.hash_token(token)
        now = _utcnow()
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT t.id, t.name, t.owner_user_id, t.scopes,
                       t.allowed_workers, t.allowed_devices,
                       t.expires_at, t.revoked_at, t.last_used_at,
                       u.username, u.display_name, u.disabled AS owner_disabled
                FROM platform_agent_tokens t
                JOIN platform_users u ON u.id = t.owner_user_id
                WHERE t.token_hash = ?
                """,
                (token_hash,),
            ).fetchone()
            if not row or row["revoked_at"] or row["owner_disabled"]:
                return None, None
            if row["expires_at"]:
                try:
                    if _from_iso(row["expires_at"]) <= now:
                        return None, None
                except ValueError:
                    return None, None
            # Throttle last_used_at writes like session last_seen (60s).
            if not _last_seen_recent(row["last_used_at"], now):
                conn.execute(
                    "UPDATE platform_agent_tokens SET last_used_at = ? "
                    "WHERE token_hash = ? AND (last_used_at IS NULL OR last_used_at < ?)",
                    (_to_iso(now), token_hash, _to_iso(now - timedelta(seconds=60))),
                )
                conn.commit()
        record = dict(row)
        record.pop("owner_disabled", None)
        scopes = frozenset(
            s for s in str(row["scopes"] or "").split(",") if s
        )
        principal = CurrentUser(
            id=f"agent:{row['id']}",
            username=f"agent:{row['name']}",
            role=AGENT_ROLE,
            display_name=str(row["name"]),
            extra_permissions=scopes,
        )
        return principal, record

    @staticmethod
    def agent_acl_allows(record: dict[str, Any] | None, kind: str, value: str) -> bool:
        """Check allowed_workers/allowed_devices from a token record."""
        if record is None:
            return False
        allowed = str(record.get(f"allowed_{kind}") or "*")
        if allowed == "*":
            return True
        return value in {part.strip() for part in allowed.split(",") if part.strip()}


# Enrollment-code lifecycle moved to agent_enrollment.py (line-budget split);
# re-exported so ``from .agent_tokens import AgentTokenEnrollmentMixin`` and
# the AuthService mixin order stay stable for back-compat.
from .agent_enrollment import AgentTokenEnrollmentMixin  # noqa: E402,F401
