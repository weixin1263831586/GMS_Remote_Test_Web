"""SQLite persistence for the ops foundation: alert ledger and ops flags.

Two small tables under ``data/ops/ops.sqlite3``:

- ``ops_events``  — deduplicated operational incidents (worker offline,
  consecutive failures, disk pressure, kill-switch changes). Ack is a
  bookkeeping action, never a delete: the ledger stays auditable.
- ``ops_flags``   — a tiny key/value store for global operations state
  (currently only ``advancement_paused``, the kill-switch).

Migrations follow the repo hard rule: a process-safe ``BEGIN IMMEDIATE``
write lock plus idempotent statements, safe to re-run from Web, Worker and
CLI processes.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


OPS_FLAG_PAUSED = "advancement_paused"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


class OpsStore:
    _REQUIRED_TABLES = frozenset({"ops_events", "ops_flags"})

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self._schema_lock = threading.RLock()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _open_connection(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def _connect(self) -> sqlite3.Connection:
        conn = self._open_connection()
        existing_tables = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        if not self._REQUIRED_TABLES.issubset(existing_tables):
            conn.close()
            self._init_schema()
            conn = self._open_connection()
        return conn

    def _init_schema(self) -> None:
        # 进程安全（仓库硬规则）：BEGIN IMMEDIATE 串行化建表迁移，语句幂等。
        with self._schema_lock, self._open_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS ops_events (
                    id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    subject TEXT NOT NULL DEFAULT '',
                    dedup_key TEXT NOT NULL DEFAULT '',
                    message TEXT NOT NULL,
                    next_actions_json TEXT NOT NULL DEFAULT '[]',
                    status TEXT NOT NULL DEFAULT 'open',
                    acknowledged_at TEXT NOT NULL DEFAULT '',
                    acknowledged_by TEXT NOT NULL DEFAULT '',
                    notified_at TEXT NOT NULL DEFAULT ''
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_ops_events_status "
                "ON ops_events(status, created_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_ops_events_dedup "
                "ON ops_events(dedup_key, created_at)"
            )
            conn.execute("""
                CREATE TABLE IF NOT EXISTS ops_flags (
                    key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    updated_by TEXT NOT NULL DEFAULT ''
                )
            """)

    # ---- flags -----------------------------------------------------------

    def get_flag(self, key: str, default: Any = None) -> Any:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT value_json FROM ops_flags WHERE key=?", (key,)
            ).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["value_json"])
        except (TypeError, json.JSONDecodeError):
            return default

    def set_flag(self, key: str, value: Any, updated_by: str = "") -> None:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """INSERT INTO ops_flags (key, value_json, updated_at, updated_by)
                   VALUES (?,?,?,?)
                   ON CONFLICT(key) DO UPDATE SET
                     value_json=excluded.value_json,
                     updated_at=excluded.updated_at,
                     updated_by=excluded.updated_by""",
                (key, json.dumps(value), utc_now_iso(), updated_by),
            )

    def flag_meta(self, key: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT updated_at, updated_by FROM ops_flags WHERE key=?", (key,)
            ).fetchone()
        if row is None:
            return {"updated_at": "", "updated_by": ""}
        return {"updated_at": row["updated_at"], "updated_by": row["updated_by"]}

    # ---- kill-switch -----------------------------------------------------

    def advancement_paused(self) -> bool:
        return bool(self.get_flag(OPS_FLAG_PAUSED, False))

    # ---- events ----------------------------------------------------------

    def record_incident(
        self,
        *,
        severity: str,
        kind: str,
        subject: str = "",
        dedup_key: str = "",
        message: str,
        next_actions: list[dict[str, Any]] | None = None,
        suppress_minutes: int = 60,
    ) -> dict[str, Any]:
        """Insert one incident unless an identical open one is still fresh.

        ``suppress_minutes <= 0`` disables suppression (kill-switch changes
        are always recorded). The returned dict carries ``suppressed`` so the
        caller can skip notifications for deduplicated repeats.
        """
        now_text = utc_now_iso()
        if dedup_key and suppress_minutes > 0:
            with self._connect() as conn:
                row = conn.execute(
                    """SELECT * FROM ops_events
                       WHERE dedup_key=? AND status='open'
                       ORDER BY created_at DESC LIMIT 1""",
                    (dedup_key,),
                ).fetchone()
            if row is not None:
                created = _parse_iso(row["created_at"])
                if created is not None:
                    age = datetime.now(timezone.utc) - created
                    if age <= timedelta(minutes=suppress_minutes):
                        return {"event": dict(row), "suppressed": True}
        event_id = "ops-" + uuid.uuid4().hex[:12]
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """INSERT INTO ops_events
                   (id, created_at, severity, kind, subject, dedup_key,
                    message, next_actions_json)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (
                    event_id,
                    now_text,
                    severity,
                    kind,
                    subject,
                    dedup_key,
                    message,
                    json.dumps(next_actions or [], ensure_ascii=False),
                ),
            )
        return {"event": self.get_event(event_id), "suppressed": False}

    def get_event(self, event_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM ops_events WHERE id=?", (event_id,)
            ).fetchone()
        return dict(row) if row is not None else None

    def list_events(
        self, *, status: str = "", limit: int = 100
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM ops_events"
        params: list[Any] = []
        if status in {"open", "ack"}:
            query += " WHERE status=?"
            params.append(status)
        query += " ORDER BY created_at DESC, id DESC LIMIT ?"
        params.append(max(1, min(int(limit), 500)))
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def ack_event(self, event_id: str, username: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """UPDATE ops_events SET status='ack',
                       acknowledged_at=?, acknowledged_by=?
                   WHERE id=? AND status='open'""",
                (utc_now_iso(), username, event_id),
            )
        return self.get_event(event_id)

    def open_event_count(self) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM ops_events WHERE status='open'"
            ).fetchone()
        return int(row["n"] if row is not None else 0)
