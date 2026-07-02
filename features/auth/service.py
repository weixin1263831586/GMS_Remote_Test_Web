from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request

from foundation.config import settings


AUTH_COOKIE_NAME = "gms_session"
PASSWORD_ALGORITHM = "pbkdf2_sha256"
PASSWORD_ITERATIONS = 260_000
SESSION_ABSOLUTE_HOURS = int(os.getenv("GMS_SESSION_ABSOLUTE_HOURS", "8"))
SESSION_IDLE_HOURS = int(os.getenv("GMS_SESSION_IDLE_HOURS", "2"))
# How long a "sensitive operation" elevation (re-auth as admin) stays valid on a
# session before the user must re-enter admin credentials. Short by design.
ELEVATION_MINUTES = int(os.getenv("GMS_ELEVATION_MINUTES", "15"))


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _to_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _from_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class CurrentUser:
    id: str
    username: str
    role: str
    display_name: str = ""

    def as_dict(self) -> dict[str, str]:
        return {
            "id": self.id,
            "username": self.username,
            "role": self.role,
            "display_name": self.display_name,
        }


class AuthService:
    def __init__(self, db_path: Path | None = None):
        self.db_path = db_path or (settings.data_root / "platform_auth.sqlite3")
        self._lock = threading.RLock()
        self._initialized = False

    def initialize(self) -> None:
        with self._lock:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS platform_users (
                        id TEXT PRIMARY KEY,
                        username TEXT NOT NULL UNIQUE,
                        password_hash TEXT NOT NULL,
                        role TEXT NOT NULL CHECK(role IN ('admin', 'user')),
                        display_name TEXT NOT NULL DEFAULT '',
                        disabled INTEGER NOT NULL DEFAULT 0,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS platform_sessions (
                        token_hash TEXT PRIMARY KEY,
                        user_id TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        last_seen_at TEXT NOT NULL,
                        expires_at TEXT NOT NULL,
                        idle_expires_at TEXT NOT NULL,
                        revoked_at TEXT,
                        FOREIGN KEY(user_id) REFERENCES platform_users(id)
                    )
                    """
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_platform_sessions_user ON platform_sessions(user_id)"
                )
                # Migration: add elevated_until to track temporary admin elevation
                # for sensitive operations (remove user / disconnect device).
                existing_cols = {
                    row[1] for row in conn.execute("PRAGMA table_info('platform_sessions')").fetchall()
                }
                if "elevated_until" not in existing_cols:
                    conn.execute("ALTER TABLE platform_sessions ADD COLUMN elevated_until TEXT")
                conn.commit()
            self._initialized = True

    def _ensure_initialized(self) -> None:
        if not self._initialized:
            self.initialize()

    def _connect(self) -> sqlite3.Connection:
        self._ensure_initialized()
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def users_exist(self) -> bool:
        with self._connect() as conn:
            row = conn.execute("SELECT 1 FROM platform_users LIMIT 1").fetchone()
            return row is not None

    def setup_required(self) -> bool:
        return not self.users_exist()

    def _hash_password(self, password: str, salt: str | None = None) -> str:
        salt = salt or secrets.token_hex(16)
        digest = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            salt.encode("ascii"),
            PASSWORD_ITERATIONS,
        ).hex()
        return f"{PASSWORD_ALGORITHM}${PASSWORD_ITERATIONS}${salt}${digest}"

    def _verify_password(self, password: str, stored: str) -> bool:
        try:
            algorithm, iterations, salt, digest = stored.split("$", 3)
            if algorithm != PASSWORD_ALGORITHM:
                return False
            candidate = hashlib.pbkdf2_hmac(
                "sha256",
                password.encode("utf-8"),
                salt.encode("ascii"),
                int(iterations),
            ).hex()
            return hmac.compare_digest(candidate, digest)
        except Exception:
            return False

    def _row_to_user(self, row: sqlite3.Row | None) -> CurrentUser | None:
        if not row:
            return None
        return CurrentUser(
            id=str(row["id"]),
            username=str(row["username"]),
            role=str(row["role"]),
            display_name=str(row["display_name"] or ""),
        )

    def _validate_username(self, username: str) -> str:
        cleaned = (username or "").strip()
        if not cleaned:
            raise ValueError("用户名不能为空")
        if len(cleaned) > 64:
            raise ValueError("用户名不能超过 64 个字符")
        if any(ch.isspace() for ch in cleaned):
            raise ValueError("用户名不能包含空白字符")
        return cleaned

    def _validate_password(self, password: str) -> None:
        if not password or len(password) < 8:
            raise ValueError("密码至少需要 8 位")

    def create_user(
        self,
        username: str,
        password: str,
        *,
        role: str = "user",
        display_name: str = "",
    ) -> CurrentUser:
        username = self._validate_username(username)
        self._validate_password(password)
        if role not in {"admin", "user"}:
            raise ValueError("角色必须是 admin 或 user")
        now = _to_iso(_utcnow())
        user_id = secrets.token_urlsafe(16)
        with self._lock, self._connect() as conn:
            try:
                conn.execute(
                    """
                    INSERT INTO platform_users (
                        id, username, password_hash, role, display_name,
                        disabled, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 0, ?, ?)
                    """,
                    (
                        user_id,
                        username,
                        self._hash_password(password),
                        role,
                        display_name.strip(),
                        now,
                        now,
                    ),
                )
                conn.commit()
            except sqlite3.IntegrityError as exc:
                raise ValueError("用户名已存在") from exc
        return CurrentUser(user_id, username, role, display_name.strip())

    def create_initial_admin(self, username: str, password: str, display_name: str = "") -> CurrentUser:
        with self._lock:
            if self.users_exist():
                raise ValueError("系统已经完成初始化")
            return self.create_user(username, password, role="admin", display_name=display_name)

    def authenticate(self, username: str, password: str) -> CurrentUser | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM platform_users WHERE username = ? AND disabled = 0",
                ((username or "").strip(),),
            ).fetchone()
        if not row or not self._verify_password(password or "", row["password_hash"]):
            return None
        return self._row_to_user(row)

    def create_session(self, user_id: str) -> str:
        token = secrets.token_urlsafe(32)
        token_hash = self.hash_token(token)
        now = _utcnow()
        expires_at = now + timedelta(hours=SESSION_ABSOLUTE_HOURS)
        idle_expires_at = now + timedelta(hours=SESSION_IDLE_HOURS)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO platform_sessions (
                    token_hash, user_id, created_at, last_seen_at,
                    expires_at, idle_expires_at, revoked_at
                ) VALUES (?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    token_hash,
                    user_id,
                    _to_iso(now),
                    _to_iso(now),
                    _to_iso(expires_at),
                    _to_iso(idle_expires_at),
                ),
            )
            conn.commit()
        return token

    def hash_token(self, token: str) -> str:
        return hashlib.sha256((token or "").encode("utf-8")).hexdigest()

    def revoke_session(self, token: str) -> None:
        token_hash = self.hash_token(token)
        with self._connect() as conn:
            conn.execute(
                "UPDATE platform_sessions SET revoked_at = ? WHERE token_hash = ?",
                (_to_iso(_utcnow()), token_hash),
            )
            conn.commit()

    def elevate_session(self, token: str, admin_user: CurrentUser, *, minutes: int | None = None) -> bool:
        """Stamp a temporary admin elevation onto the current session.

        Called after the caller has re-authenticated with admin credentials
        (verified via :meth:`authenticate`). Marks the session as elevated for
        :data:`ELEVATION_MINUTES` by default. Pass ``minutes`` to override the
        window (e.g. admin login grants elevation for the whole session so the
        user is not re-prompted for sensitive operations). Returns True if the
        session was found/updated.
        """
        if admin_user.role != "admin":
            return False
        token_hash = self.hash_token(token)
        elevated_until = _utcnow() + timedelta(minutes=minutes if minutes is not None else ELEVATION_MINUTES)
        with self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE platform_sessions
                SET elevated_until = ?
                WHERE token_hash = ? AND revoked_at IS NULL
                """,
                (_to_iso(elevated_until), token_hash),
            )
            conn.commit()
            return cur.rowcount > 0

    def get_elevated_until(self, token: str | None) -> str | None:
        """Return the ISO elevation-expiry timestamp for a session, or None.

        None means the session is not currently elevated. An expired elevation
        (past ``elevated_until``) is treated as not elevated.
        """
        if not token:
            return None
        token_hash = self.hash_token(token)
        now = _utcnow()
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT elevated_until FROM platform_sessions
                WHERE token_hash = ? AND revoked_at IS NULL
                """,
                (token_hash,),
            ).fetchone()
        if not row or not row["elevated_until"]:
            return None
        try:
            if _from_iso(row["elevated_until"]) <= now:
                return None
        except Exception:
            return None
        return row["elevated_until"]

    def get_user_for_token(self, token: str | None, *, refresh: bool = True) -> CurrentUser | None:
        if not token:
            return None
        token_hash = self.hash_token(token)
        now = _utcnow()
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT u.*
                FROM platform_sessions s
                JOIN platform_users u ON u.id = s.user_id
                WHERE s.token_hash = ?
                  AND s.revoked_at IS NULL
                  AND u.disabled = 0
                  AND s.expires_at > ?
                  AND s.idle_expires_at > ?
                """,
                (token_hash, _to_iso(now), _to_iso(now)),
            ).fetchone()
            if not row:
                return None
            if refresh:
                conn.execute(
                    """
                    UPDATE platform_sessions
                    SET last_seen_at = ?, idle_expires_at = ?
                    WHERE token_hash = ?
                    """,
                    (
                        _to_iso(now),
                        _to_iso(now + timedelta(hours=SESSION_IDLE_HOURS)),
                        token_hash,
                    ),
                )
                conn.commit()
        return self._row_to_user(row)

    def list_users(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, username, role, display_name, disabled, created_at, updated_at
                FROM platform_users
                ORDER BY username
                """
            ).fetchall()
        return [dict(row) for row in rows]


auth_service = AuthService()


def get_authenticated_user(request: Request) -> CurrentUser | None:
    user = getattr(request.state, "current_user", None)
    if isinstance(user, CurrentUser):
        return user
    token = request.cookies.get(AUTH_COOKIE_NAME)
    user = auth_service.get_user_for_token(token)
    if user:
        request.state.current_user = user
    return user


def require_authenticated_user(request: Request) -> CurrentUser:
    user = get_authenticated_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    return user


def require_role(*roles: str):
    allowed = set(roles)

    def dependency(request: Request) -> CurrentUser:
        user = require_authenticated_user(request)
        if user.role not in allowed:
            raise HTTPException(status_code=403, detail="Permission denied")
        return user

    return dependency


def is_elevated(request: Request) -> bool:
    """Whether the current session has a live admin elevation for this request.

    Cached on request.state so multiple dependency checks within one request
    don't each hit the DB.
    """
    if getattr(request.state, "is_elevated", None) is not None:
        return bool(request.state.is_elevated)
    token = request.cookies.get(AUTH_COOKIE_NAME)
    elevated_until = auth_service.get_elevated_until(token)
    request.state.is_elevated = bool(elevated_until)
    return bool(elevated_until)


def require_elevated_admin(request: Request) -> CurrentUser:
    """Dependency for sensitive operations (remove user / disconnect device).

    Requires an authenticated admin session that has been recently elevated
    (re-confirmed admin credentials within :data:`ELEVATION_MINUTES`). A
    non-elevated or non-admin caller gets 403 with ``elevation_required`` so the
    frontend can prompt for admin credentials and replay the request.
    """
    user = require_authenticated_user(request)
    if user.role != "admin" or not is_elevated(request):
        raise HTTPException(
            status_code=403,
            detail={"message": "Elevation required", "elevation_required": True},
        )
    return user
