"""Enrollment-code service (split from agent_tokens.py).

AgentTokenEnrollmentMixin holds the one-shot enrollment-code lifecycle
(``create_agent_enrollment`` / ``redeem_agent_enrollment``). Mixed into
AuthService after AgentTokenServiceMixin; reuses its
_connect/_lock/hash_token/normalize_scopes/_normalize_acl helpers and the
``_utcnow``/``_to_iso``/``_from_iso`` time helpers from agent_tokens.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from .constants import DEFAULT_AGENT_TOKEN_DAYS, CurrentUser


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _to_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _from_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class AgentTokenEnrollmentMixin:
    """One-shot enrollment-code minting and redemption."""

    ENROLLMENT_TTL_MINUTES = 5

    def create_agent_enrollment(
        self,
        *,
        name: str,
        creator: CurrentUser,
        scopes: list[str] | str | None,
        allowed_workers: str | list[str] | None = None,
        allowed_devices: str | list[str] | None = None,
        expires_days: int | None = DEFAULT_AGENT_TOKEN_DAYS,
        ttl_minutes: int | None = None,
    ) -> dict[str, Any]:
        """Mint a one-shot enrollment code; the raw code is returned once.

        Entropy: token_hex(3)×3 = 3×24 = 72 bits. 72 bits plus the 5-minute TTL and per-IP
        rate limiting keeps online guessing impractical; the code is stored
        hashed so a leaked DB row is not directly usable. The endpoint is
        anonymous and rate-limited per IP, but the code itself must still
        resist offline guessing (code review 2026-08: 24-bit groups — a
        single 24-bit group — were rejected as too small; the three-group
        form is what makes this acceptable).

        ``ttl_minutes`` (1–30, default ``ENROLLMENT_TTL_MINUTES``) lets an
        admin widen the exchange window for slow hand-off while keeping
        it short by default.
        """
        if ttl_minutes is None:
            ttl_minutes = self.ENROLLMENT_TTL_MINUTES
        try:
            ttl_minutes = int(ttl_minutes)
        except (TypeError, ValueError):
            raise ValueError("ttl_minutes 必须是 1–30 的整数") from None
        if not 1 <= ttl_minutes <= 30:
            raise ValueError("ttl_minutes 必须在 1–30 分钟之间")
        code = "-".join(secrets.token_hex(3).upper() for _ in range(3))
        now = _utcnow()
        expires_at = now + timedelta(minutes=ttl_minutes)
        with self._lock, self._connect() as conn:
            conn.execute(
                "DELETE FROM platform_agent_enrollments WHERE expires_at <= ? OR used_at IS NOT NULL",
                (_to_iso(now),),
            )
            conn.execute(
                """
                INSERT INTO platform_agent_enrollments (
                    code_hash, name, created_by, scopes, allowed_workers,
                    allowed_devices, expires_days, created_at, expires_at, used_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    self.hash_token(code),
                    str(name or "").strip() or "agent",
                    creator.id,
                    self.normalize_scopes(scopes),
                    self._normalize_acl(allowed_workers),
                    self._normalize_acl(allowed_devices),
                    max(1, int(expires_days or DEFAULT_AGENT_TOKEN_DAYS)),
                    _to_iso(now),
                    _to_iso(expires_at),
                ),
            )
            conn.commit()
        return {
            "code": code,
            "name": str(name or "").strip() or "agent",
            "expires_at": _to_iso(expires_at),
            "ttl_minutes": ttl_minutes,
        }

    def redeem_agent_enrollment(
        self, code: str
    ) -> tuple[dict[str, Any] | None, dict[str, str]]:
        """Atomically redeem one enrollment code; returns the token record.

        The enrollment (not the caller) decides scopes/ACLs/expiry, so a
        leaked code cannot grant more than the admin approved.

        Returns ``(record, reason)`` where a successful redeem yields the
        token record and ``{}``. Failures are distinguishable:
        ``{"reason": "used", "used_at": ...}``,
        ``{"reason": "expired", "expires_at": ...}`` or
        ``{"reason": "invalid"}``.
        """
        code = str(code or "").strip().upper()
        if not code:
            return None, {"reason": "invalid"}
        code_hash = self.hash_token(code)
        now = _utcnow()
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT name, created_by, scopes, allowed_workers, allowed_devices,
                       expires_days, expires_at, used_at
                FROM platform_agent_enrollments
                WHERE code_hash = ?
                """,
                (code_hash,),
            ).fetchone()
            if not row:
                return None, {"reason": "invalid"}
            if row["used_at"]:
                return None, {"reason": "used", "used_at": str(row["used_at"])}
            try:
                if _from_iso(row["expires_at"]) <= now:
                    return None, {
                        "reason": "expired",
                        "expires_at": str(row["expires_at"]),
                    }
            except ValueError:
                return None, {"reason": "invalid"}
            cursor = conn.execute(
                "UPDATE platform_agent_enrollments SET used_at = ? "
                "WHERE code_hash = ? AND used_at IS NULL",
                (_to_iso(now), code_hash),
            )
            if cursor.rowcount != 1:
                return None, {"reason": "used", "used_at": _to_iso(now)}
            token = secrets.token_urlsafe(32)
            token_id = f"agt_{secrets.token_hex(8)}"
            token_expires_at = now + timedelta(days=int(row["expires_days"]))
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
                    str(row["name"]),
                    str(row["created_by"]),
                    row["scopes"],
                    row["allowed_workers"],
                    row["allowed_devices"],
                    _to_iso(now),
                    _to_iso(token_expires_at),
                ),
            )
            conn.commit()
        return {
            "id": token_id,
            "name": str(row["name"]),
            "token": token,
            "scopes": [s for s in str(row["scopes"] or "").split(",") if s],
            "allowed_workers": str(row["allowed_workers"] or "*"),
            "allowed_devices": str(row["allowed_devices"] or "*"),
            "expires_at": _to_iso(token_expires_at),
        }, {}
