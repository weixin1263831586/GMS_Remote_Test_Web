"""Persistent authentication rate limiting shared by all app workers."""

from __future__ import annotations

import hashlib
import math
import os
import sqlite3
from datetime import timedelta
from typing import Any

from .timestamps import from_utc_iso, to_utc_iso, utc_now


AUTH_FAILURE_WINDOW_MINUTES = int(
    os.getenv("GMS_AUTH_FAILURE_WINDOW_MINUTES", "15"),
)
AUTH_BLOCK_MINUTES = int(os.getenv("GMS_AUTH_BLOCK_MINUTES", "15"))
AUTH_MAX_ACCOUNT_IP_FAILURES = int(os.getenv("GMS_AUTH_MAX_FAILURES", "5"))
AUTH_MAX_IP_FAILURES = int(os.getenv("GMS_AUTH_MAX_IP_FAILURES", "30"))


def initialize_auth_attempt_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS platform_auth_attempts (
            attempt_key TEXT PRIMARY KEY,
            failed_count INTEGER NOT NULL,
            window_started_at TEXT NOT NULL,
            blocked_until TEXT,
            updated_at TEXT NOT NULL
        )
        """,
    )


class AuthRateLimitMixin:
    """SQLite-backed credential-attempt limiter for ``AuthService``."""

    _lock: Any

    def _connect(self) -> sqlite3.Connection:
        raise NotImplementedError

    def _auth_attempt_keys(
        self,
        purpose: str,
        username: str,
        source_ip: str,
    ) -> tuple[tuple[str, int], tuple[str, int]]:
        normalized_user = (username or "").strip().casefold()
        normalized_ip = (source_ip or "unknown").strip().lower()

        def key(scope: str, value: str) -> str:
            payload = f"{purpose}:{scope}:{value}".encode()
            return hashlib.sha256(payload).hexdigest()

        return (
            (
                key("account_ip", f"{normalized_user}@{normalized_ip}"),
                AUTH_MAX_ACCOUNT_IP_FAILURES,
            ),
            (key("ip", normalized_ip), AUTH_MAX_IP_FAILURES),
        )

    def auth_retry_after(
        self,
        purpose: str,
        username: str,
        source_ip: str,
    ) -> int:
        """Return seconds until another credential attempt is allowed."""

        now = utc_now()
        keys = [item[0] for item in self._auth_attempt_keys(purpose, username, source_ip)]
        placeholders = ",".join("?" for _ in keys)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT blocked_until
                FROM platform_auth_attempts
                WHERE attempt_key IN ({placeholders})
                  AND blocked_until IS NOT NULL
                """,
                keys,
            ).fetchall()
        retry_after = 0
        for row in rows:
            try:
                remaining = (from_utc_iso(row["blocked_until"]) - now).total_seconds()
            except (TypeError, ValueError):
                continue
            retry_after = max(retry_after, math.ceil(remaining))
        return max(retry_after, 0)

    def record_auth_failure(
        self,
        purpose: str,
        username: str,
        source_ip: str,
    ) -> int:
        """Persist a failed credential attempt across workers and restarts."""

        now = utc_now()
        window_cutoff = now - timedelta(minutes=AUTH_FAILURE_WINDOW_MINUTES)
        blocked_until = now + timedelta(minutes=AUTH_BLOCK_MINUTES)
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for attempt_key, threshold in self._auth_attempt_keys(
                purpose,
                username,
                source_ip,
            ):
                row = conn.execute(
                    """
                    SELECT failed_count, window_started_at, blocked_until
                    FROM platform_auth_attempts
                    WHERE attempt_key = ?
                    """,
                    (attempt_key,),
                ).fetchone()
                count = 1
                window_started_at = now
                existing_blocked_until = None
                if row:
                    try:
                        existing_window = from_utc_iso(row["window_started_at"])
                    except (TypeError, ValueError):
                        existing_window = now
                    if existing_window > window_cutoff:
                        count = int(row["failed_count"] or 0) + 1
                        window_started_at = existing_window
                    existing_blocked_until = row["blocked_until"]
                next_blocked_until = (
                    to_utc_iso(blocked_until)
                    if count >= max(1, threshold)
                    else existing_blocked_until
                )
                conn.execute(
                    """
                    INSERT INTO platform_auth_attempts (
                        attempt_key, failed_count, window_started_at,
                        blocked_until, updated_at
                    ) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(attempt_key) DO UPDATE SET
                        failed_count = excluded.failed_count,
                        window_started_at = excluded.window_started_at,
                        blocked_until = excluded.blocked_until,
                        updated_at = excluded.updated_at
                    """,
                    (
                        attempt_key,
                        count,
                        to_utc_iso(window_started_at),
                        next_blocked_until,
                        to_utc_iso(now),
                    ),
                )
            conn.execute(
                """
                DELETE FROM platform_auth_attempts
                WHERE updated_at < ?
                  AND (blocked_until IS NULL OR blocked_until < ?)
                """,
                (to_utc_iso(window_cutoff), to_utc_iso(now)),
            )
            conn.commit()
        return self.auth_retry_after(purpose, username, source_ip)

    def clear_auth_failures(
        self,
        purpose: str,
        username: str,
        source_ip: str,
    ) -> None:
        """Clear the account/IP counter after successful authentication."""

        account_key = self._auth_attempt_keys(purpose, username, source_ip)[0][0]
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM platform_auth_attempts WHERE attempt_key = ?",
                (account_key,),
            )
            conn.commit()
