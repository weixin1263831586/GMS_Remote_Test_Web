"""Short-lived, session-bound grants for noVNC proxy access."""

from __future__ import annotations

import hashlib
import os
import secrets
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

from features.auth import CurrentUser, auth_service


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _hash(value: str) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


class NoVNCAccessService:
    """Persist noVNC grants in the authentication database.

    Every grant is bound to one authenticated browser session, one user, and
    one Worker.  Keeping grants in SQLite avoids process-local token state while
    their deliberately short lifetime limits replay exposure.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()

    @staticmethod
    def _ttl_seconds() -> int:
        try:
            configured = int(os.getenv("GMS_NOVNC_TOKEN_TTL_SECONDS", "120"))
        except ValueError:
            configured = 120
        return min(300, max(30, configured))

    def _connect(self) -> sqlite3.Connection:
        auth_service.initialize()
        conn = sqlite3.connect(auth_service.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS novnc_access_grants (
                token_hash TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                worker_id TEXT NOT NULL,
                session_hash TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                revoked_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_novnc_access_scope
            ON novnc_access_grants(user_id, worker_id, expires_at)
            """
        )
        return conn

    def issue(self, user: CurrentUser, session_token: str, worker_id: str) -> tuple[str, str]:
        if not session_token:
            raise ValueError("当前登录会话无效")
        normalized_worker = str(worker_id or "").strip()
        if not normalized_worker:
            raise ValueError("缺少 Worker 标识")

        token = secrets.token_urlsafe(32)
        now = _utcnow()
        expires_at = now + timedelta(seconds=self._ttl_seconds())
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                DELETE FROM novnc_access_grants
                WHERE revoked_at IS NOT NULL OR expires_at <= ?
                """,
                (_iso(now),),
            )
            conn.execute(
                """
                INSERT INTO novnc_access_grants (
                    token_hash, user_id, worker_id, session_hash,
                    created_at, expires_at, revoked_at
                ) VALUES (?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    _hash(token),
                    user.id,
                    normalized_worker,
                    _hash(session_token),
                    _iso(now),
                    _iso(expires_at),
                ),
            )
            conn.commit()
        return token, _iso(expires_at)

    def validate(
        self,
        token: str,
        user: CurrentUser,
        session_token: str,
        worker_id: str,
    ) -> bool:
        if not token or not session_token:
            return False
        now = _iso(_utcnow())
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT 1
                FROM novnc_access_grants
                WHERE token_hash = ?
                  AND user_id = ?
                  AND worker_id = ?
                  AND session_hash = ?
                  AND revoked_at IS NULL
                  AND expires_at > ?
                """,
                (
                    _hash(token),
                    user.id,
                    str(worker_id or "").strip(),
                    _hash(session_token),
                    now,
                ),
            ).fetchone()
        return row is not None

    def revoke_session(self, session_token: str) -> None:
        if not session_token:
            return
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE novnc_access_grants
                SET revoked_at = ?
                WHERE session_hash = ? AND revoked_at IS NULL
                """,
                (_iso(_utcnow()), _hash(session_token)),
            )
            conn.commit()


novnc_access_service = NoVNCAccessService()
