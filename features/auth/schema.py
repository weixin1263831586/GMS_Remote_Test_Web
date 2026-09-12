from __future__ import annotations

import sqlite3
from pathlib import Path

from .rate_limit import initialize_auth_attempt_schema


def initialize_auth_schema(db_path: Path) -> None:
    """Create and migrate authentication tables."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path, timeout=30) as conn:
        conn.execute("PRAGMA busy_timeout=30000")
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower():
                raise
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS platform_users (
                id TEXT PRIMARY KEY,
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('admin', 'device_operator', 'user')),
                display_name TEXT NOT NULL DEFAULT '',
                disabled INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        user_table_sql = str(
            (
                conn.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name='platform_users'"
                ).fetchone()
                or ("",)
            )[0]
            or ""
        )
        if "device_operator" not in user_table_sql:
            conn.execute("PRAGMA legacy_alter_table=ON")
            conn.execute("ALTER TABLE platform_users RENAME TO platform_users_legacy")
            conn.execute(
                """
                CREATE TABLE platform_users (
                    id TEXT PRIMARY KEY,
                    username TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('admin', 'device_operator', 'user')),
                    display_name TEXT NOT NULL DEFAULT '',
                    disabled INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                INSERT INTO platform_users (
                    id, username, password_hash, role, display_name,
                    disabled, created_at, updated_at
                )
                SELECT id, username, password_hash, role, display_name,
                       disabled, created_at, updated_at
                FROM platform_users_legacy
                """
            )
            conn.execute("DROP TABLE platform_users_legacy")
            conn.execute("PRAGMA legacy_alter_table=OFF")
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
        initialize_auth_attempt_schema(conn)
        existing_cols = {
            row[1]
            for row in conn.execute("PRAGMA table_info('platform_sessions')").fetchall()
        }
        if "elevated_until" not in existing_cols:
            conn.execute("ALTER TABLE platform_sessions ADD COLUMN elevated_until TEXT")
        if "elevated_by_user_id" not in existing_cols:
            conn.execute("ALTER TABLE platform_sessions ADD COLUMN elevated_by_user_id TEXT")
        # Agent Service Token (ADR 0006): long-lived credentials
        # for build-server agents. Only SHA256(token) is stored; the raw token
        # is returned once at creation and kept in a 0600 file client-side.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS platform_agent_tokens (
                id TEXT PRIMARY KEY,
                token_hash TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                owner_user_id TEXT NOT NULL,
                scopes TEXT NOT NULL DEFAULT '',
                allowed_workers TEXT NOT NULL DEFAULT '*',
                allowed_devices TEXT NOT NULL DEFAULT '*',
                created_at TEXT NOT NULL,
                expires_at TEXT,
                revoked_at TEXT,
                last_used_at TEXT,
                FOREIGN KEY(owner_user_id) REFERENCES platform_users(id)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_platform_agent_tokens_owner "
            "ON platform_agent_tokens(owner_user_id)"
        )
        # One-shot Approval Token (ADR 0006): server-side proof of
        # a human approval bound to one tool + device + command hash with a
        # short TTL. Replaces the client-declared authorized=true boolean.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS platform_approval_tokens (
                token_hash TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                tool TEXT NOT NULL,
                device TEXT NOT NULL,
                command_hash TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                used_at TEXT,
                FOREIGN KEY(user_id) REFERENCES platform_users(id)
            )
            """
        )
        # One-shot Enrollment Codes (ADR 0006): an admin mints a
        # short-lived pairing code; the build server exchanges it once for a
        # real Agent Service Token via gms-rt-agent-enroll.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS platform_agent_enrollments (
                code_hash TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                created_by TEXT NOT NULL,
                scopes TEXT NOT NULL DEFAULT '',
                allowed_workers TEXT NOT NULL DEFAULT '*',
                allowed_devices TEXT NOT NULL DEFAULT '*',
                expires_days INTEGER NOT NULL DEFAULT 90,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                used_at TEXT,
                FOREIGN KEY(created_by) REFERENCES platform_users(id)
            )
            """
        )
        conn.commit()
