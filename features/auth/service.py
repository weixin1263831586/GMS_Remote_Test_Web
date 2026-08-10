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

from foundation.config import settings

from .rate_limit import AuthRateLimitMixin
from .schema import initialize_auth_schema


AUTH_COOKIE_NAME = "gms_session"
PASSWORD_ALGORITHM = "pbkdf2_sha256"
PASSWORD_ITERATIONS = 260_000
# The browser cookie is intentionally a session cookie and is discarded when
# the browser closes. Keep the server-side absolute ceiling effectively
# non-expiring so a long-running GMS test or terminal session is not interrupted
# at an arbitrary wall-clock boundary; idle expiry, logout, password changes,
# account disabling, and administrator revocation still invalidate the session.
DEFAULT_SESSION_ABSOLUTE_HOURS = 100 * 365 * 24
SESSION_ABSOLUTE_HOURS = int(
    os.getenv("GMS_SESSION_ABSOLUTE_HOURS", str(DEFAULT_SESSION_ABSOLUTE_HOURS))
)
SESSION_IDLE_HOURS = int(os.getenv("GMS_SESSION_IDLE_HOURS", "2"))
# 二次认证状态绑定当前会话，并随会话失效或重新登录清除。
ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    "user": frozenset({
        "tests.execute",
        "resources.read_own",
        "resources.write_own",
        "devices.use_leased",
    }),
    "device_operator": frozenset({
        "tests.execute",
        "resources.read_own",
        "resources.write_own",
        "devices.use_leased",
        "devices.inventory",
        "devices.lease",
    }),
    "admin": frozenset({"*"}),
    "worker_service": frozenset({
        "worker.register",
        "worker.heartbeat",
        "worker.commands",
        "worker.artifacts",
    }),
}


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

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "username": self.username,
            "role": self.role,
            "display_name": self.display_name,
            "permissions": sorted(ROLE_PERMISSIONS.get(self.role, frozenset())),
        }

    def has_permission(self, permission: str) -> bool:
        granted = ROLE_PERMISSIONS.get(self.role, frozenset())
        return "*" in granted or permission in granted


class AuthService(AuthRateLimitMixin):
    _REQUIRED_TABLES = frozenset({"platform_users", "platform_sessions", "platform_auth_attempts"})

    def __init__(self, db_path: Path | None = None):
        self.db_path = db_path or (settings.data_root / "platform_auth.sqlite3")
        self._lock = threading.RLock()
        self._initialized = False

    def initialize(self) -> None:
        with self._lock:
            initialize_auth_schema(self.db_path)
            self._initialized = True

    def _ensure_initialized(self) -> None:
        if not self._initialized:
            self.initialize()

    def _connect(self) -> sqlite3.Connection:
        self._ensure_initialized()
        # Recreate the schema if data/ was removed after startup.
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.execute('PRAGMA busy_timeout=30000')
        conn.row_factory = sqlite3.Row
        existing_tables = {str(row[0]) for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()}
        if not self._REQUIRED_TABLES.issubset(existing_tables):
            conn.close()
            self.initialize()
            conn = sqlite3.connect(self.db_path, timeout=30)
            conn.execute('PRAGMA busy_timeout=30000')
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
        if role not in {"admin", "device_operator", "user"}:
            raise ValueError("角色必须是 admin、device_operator 或 user")
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
        username = self._validate_username(username)
        self._validate_password(password)
        cleaned_display_name = display_name.strip()
        now = _to_iso(_utcnow())
        user_id = secrets.token_urlsafe(16)
        with self._lock, self._connect() as conn:
            # Serialize the emptiness check with the insert across processes.
            conn.execute('BEGIN IMMEDIATE')
            if conn.execute('SELECT 1 FROM platform_users LIMIT 1').fetchone():
                raise ValueError("系统已经完成初始化")
            conn.execute(
                """
                INSERT INTO platform_users (
                    id, username, password_hash, role, display_name,
                    disabled, created_at, updated_at
                ) VALUES (?, ?, ?, 'admin', ?, 0, ?, ?)
                """,
                (
                    user_id,
                    username,
                    self._hash_password(password),
                    cleaned_display_name,
                    now,
                    now,
                ),
            )
            conn.commit()
        return CurrentUser(user_id, username, 'admin', cleaned_display_name)

    def authenticate(self, username: str, password: str) -> CurrentUser | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM platform_users WHERE username = ? AND disabled = 0",
                ((username or "").strip(),),
            ).fetchone()
        if not row or not self._verify_password(password or "", row["password_hash"]):
            return None
        return self._row_to_user(row)

    def get_enabled_user(self, username: str) -> CurrentUser | None:
        """Look up an enabled platform account without checking its password."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM platform_users WHERE username = ? AND disabled = 0",
                ((username or "").strip(),),
            ).fetchone()
        return self._row_to_user(row)

    def user_exists(self, username: str) -> bool:
        with self._connect() as conn:
            return conn.execute(
                "SELECT 1 FROM platform_users WHERE username = ?",
                ((username or "").strip(),),
            ).fetchone() is not None

    def create_client_user(self, username: str) -> CurrentUser:
        """Create the session identity for an SSH-authenticated client.

        The client password remains the host-scoped SSH credential. A random
        internal password is stored only to satisfy the session table's user
        model; it is never accepted as a browser credential.
        """
        existing = self.get_enabled_user(username)
        if existing:
            return existing
        try:
            return self.create_user(
                username,
                secrets.token_urlsafe(32),
                role="user",
                display_name=username,
            )
        except ValueError:
            existing = self.get_enabled_user(username)
            if existing:
                return existing
            raise

    def create_session(self, user_id: str) -> str:
        token = secrets.token_urlsafe(32)
        token_hash = self.hash_token(token)
        now = _utcnow()
        expires_at = now + timedelta(hours=SESSION_ABSOLUTE_HOURS)
        idle_expires_at = now + timedelta(hours=SESSION_IDLE_HOURS)
        with self._connect() as conn:
            # Sessions are otherwise append-only. Opportunistic cleanup on
            # login bounds the table without requiring a scheduler.
            conn.execute(
                """
                DELETE FROM platform_sessions
                WHERE revoked_at IS NOT NULL
                   OR expires_at <= ?
                   OR idle_expires_at <= ?
                """,
                (_to_iso(now), _to_iso(now)),
            )
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

    def revoke_user_sessions(self, user_id: str) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE platform_sessions
                SET revoked_at = ?
                WHERE user_id = ? AND revoked_at IS NULL
                """,
                (_to_iso(_utcnow()), user_id),
            )
            conn.commit()
            return cursor.rowcount

    def update_user(
        self,
        user_id: str,
        *,
        role: str | None = None,
        display_name: str | None = None,
        disabled: bool | None = None,
    ) -> CurrentUser:
        if role is not None and role not in {"admin", "device_operator", "user"}:
            raise ValueError("角色必须是 admin、device_operator 或 user")
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM platform_users WHERE id = ?",
                (user_id,),
            ).fetchone()
            if row is None:
                raise ValueError("用户不存在")
            removes_admin = row["role"] == "admin" and (
                (role is not None and role != "admin") or disabled is True
            )
            if removes_admin:
                active_admins = conn.execute(
                    """
                    SELECT COUNT(*) FROM platform_users
                    WHERE role = 'admin' AND disabled = 0
                    """
                ).fetchone()[0]
                if int(active_admins) <= 1:
                    raise ValueError("不能停用或降级最后一个管理员")
            next_role = role if role is not None else str(row["role"])
            next_display_name = (
                str(display_name).strip()
                if display_name is not None
                else str(row["display_name"] or "")
            )
            next_disabled = int(bool(disabled)) if disabled is not None else int(row["disabled"])
            conn.execute(
                """
                UPDATE platform_users
                SET role = ?, display_name = ?, disabled = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    next_role,
                    next_display_name,
                    next_disabled,
                    _to_iso(_utcnow()),
                    user_id,
                ),
            )
            updated = conn.execute(
                "SELECT * FROM platform_users WHERE id = ?",
                (user_id,),
            ).fetchone()
            conn.commit()
        if role is not None or disabled is not None:
            self.revoke_user_sessions(user_id)
        user = self._row_to_user(updated)
        if user is None:
            raise ValueError("用户不存在")
        return user

    def set_user_password(self, user_id: str, password: str) -> None:
        self._validate_password(password)
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE platform_users
                SET password_hash = ?, updated_at = ?
                WHERE id = ?
                """,
                (self._hash_password(password), _to_iso(_utcnow()), user_id),
            )
            if cursor.rowcount == 0:
                raise ValueError("用户不存在")
            conn.commit()
        self.revoke_user_sessions(user_id)

    def elevate_session(self, token: str, admin_user: CurrentUser, *, minutes: int | None = None) -> bool:
        """Stamp an admin verification onto the current session.

        Called after the caller has re-authenticated with admin credentials
        (verified via :meth:`authenticate`). The caller may be an ordinary
        client: the client keeps its own identity while this session records
        which admin verified it. By default the grant stays valid for the
        remainder of the session's lifetime and clears when the session
        expires or is revoked.
        """
        if admin_user.role != "admin":
            return False
        token_hash = self.hash_token(token)
        now = _utcnow()
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT expires_at FROM platform_sessions
                WHERE token_hash = ?
                  AND revoked_at IS NULL
                  AND expires_at > ?
                  AND idle_expires_at > ?
                """,
                (
                    token_hash,
                    _to_iso(now),
                    _to_iso(now),
                ),
            ).fetchone()
            if not row:
                return False
            if minutes is not None:
                elevated_until = now + timedelta(minutes=minutes)
            else:
                # Elevation lasts for the rest of the session's absolute lifetime.
                elevated_until = _from_iso(str(row["expires_at"]))
            cur = conn.execute(
                """
                UPDATE platform_sessions
                SET elevated_until = ?, elevated_by_user_id = ?
                WHERE token_hash = ?
                  AND revoked_at IS NULL
                """,
                (
                    _to_iso(elevated_until),
                    admin_user.id,
                    token_hash,
                ),
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

    def clear_elevation(self, token: str | None) -> bool:
        """Clear admin verification from one live browser session."""
        if not token:
            return False
        token_hash = self.hash_token(token)
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE platform_sessions
                SET elevated_until = NULL, elevated_by_user_id = NULL
                WHERE token_hash = ? AND revoked_at IS NULL
                """,
                (token_hash,),
            )
            conn.commit()
        return cursor.rowcount > 0

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
                SELECT u.id, u.username, u.role, u.display_name, u.disabled,
                       u.created_at, u.updated_at,
                       COUNT(CASE WHEN s.revoked_at IS NULL
                                   AND s.expires_at > ?
                                   AND s.idle_expires_at > ?
                                  THEN 1 END) AS active_session_count,
                       MAX(CASE WHEN s.revoked_at IS NULL
                                THEN s.last_seen_at END) AS last_seen_at
                FROM platform_users u
                LEFT JOIN platform_sessions s ON s.user_id = u.id
                GROUP BY u.id
                ORDER BY username
                """,
                (_to_iso(_utcnow()), _to_iso(_utcnow())),
            ).fetchall()
        return [dict(row) for row in rows]


auth_service = AuthService()
