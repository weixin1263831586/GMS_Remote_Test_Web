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
        conn.commit()
