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


def _retry_delay_seconds(attempts: int) -> int:
    return min(1800, 60 * (2 ** min(max(0, attempts - 1), 5)))


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
                    notified_at TEXT NOT NULL DEFAULT '',
                    notify_state TEXT NOT NULL DEFAULT 'pending',
                    notify_attempts INTEGER NOT NULL DEFAULT 0,
                    notify_attempted_at TEXT NOT NULL DEFAULT '',
                    notify_retry_at TEXT NOT NULL DEFAULT ''
                )
            """)
            # Phase migrations: the delivery-state columns were added after
            # the first release of the ledger; keep idempotent ALTERs here.
            existing_cols = {
                str(row[1])
                for row in conn.execute("PRAGMA table_info(ops_events)").fetchall()
            }
            if "notify_state" not in existing_cols:
                conn.execute(
                    "ALTER TABLE ops_events ADD COLUMN notify_state "
                    "TEXT NOT NULL DEFAULT 'pending'"
                )
            if "notify_attempts" not in existing_cols:
                conn.execute(
                    "ALTER TABLE ops_events ADD COLUMN notify_attempts "
                    "INTEGER NOT NULL DEFAULT 0"
                )
            if "notify_attempted_at" not in existing_cols:
                conn.execute(
                    "ALTER TABLE ops_events ADD COLUMN notify_attempted_at "
                    "TEXT NOT NULL DEFAULT ''"
                )
            if "notify_retry_at" not in existing_cols:
                conn.execute(
                    "ALTER TABLE ops_events ADD COLUMN notify_retry_at "
                    "TEXT NOT NULL DEFAULT ''"
                )
                # Older ledgers have no attempt timestamp. Preserve their
                # previous due time once; subsequent failures use the real attempt.
                for row in conn.execute(
                    "SELECT id, created_at, notify_attempts FROM ops_events "
                    "WHERE notify_state='failed' AND notify_attempts > 0"
                ).fetchall():
                    attempted = _parse_iso(row["created_at"]) or datetime.now(timezone.utc)
                    retry = attempted + timedelta(seconds=_retry_delay_seconds(row["notify_attempts"]))
                    conn.execute(
                        "UPDATE ops_events SET notify_attempted_at=?, notify_retry_at=? WHERE id=?",
                        (attempted.strftime("%Y-%m-%dT%H:%M:%SZ"), retry.strftime("%Y-%m-%dT%H:%M:%SZ"), row["id"]),
                    )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_ops_events_delivery "
                "ON ops_events(notify_state, notify_retry_at, created_at)"
            )
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
        event_id = "ops-" + uuid.uuid4().hex[:12]
        # 去重查询与插入必须在同一 BEGIN IMMEDIATE 事务内：分开两次连接
        # 时两个进程可能同时读到“无新鲜事件”然后各插一条。
        if not (dedup_key and suppress_minutes > 0):
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
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """SELECT * FROM ops_events
                   WHERE dedup_key=? AND status='open'
                   ORDER BY created_at DESC LIMIT 1""",
                (dedup_key,),
            ).fetchone()
            if row is not None:
                created = _parse_iso(row["created_at"])
                age = (
                    datetime.now(timezone.utc) - created
                    if created is not None
                    else None
                )
                if age is not None and age <= timedelta(minutes=suppress_minutes):
                    return {"event": dict(row), "suppressed": True}
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

    # ---- delivery state ----------------------------------------------------

    def pending_events(self, *, limit: int = 20, ready_only: bool = False) -> list[dict[str, Any]]:
        """Events whose notification is still pending or failed (retryable).

        ``ready_only`` filters due times before LIMIT so events in backoff
        cannot hide fresh alerts behind a fixed-size queue head.
        """
        query = "SELECT * FROM ops_events WHERE notify_state IN ('pending', 'failed')"
        params: list[Any] = []
        if ready_only:
            query += " AND (notify_retry_at='' OR notify_retry_at <= ?)"
            params.append(utc_now_iso())
        query += " ORDER BY created_at ASC, id ASC LIMIT ?"
        params.append(max(1, min(int(limit), 100)))
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def mark_delivery_state(
        self, event_id: str, *, state: str, attempts: int
    ) -> None:
        """Persist notification outcome: sent (with notified_at) or failed."""
        allowed = {"pending", "failed", "sent", "skipped"}
        if state not in allowed:
            raise ValueError(f"invalid notify_state: {state}")
        attempted_at = utc_now_iso() if state in {"sent", "failed"} else ""
        retry_at = ""
        if state == "failed":
            attempted = _parse_iso(attempted_at)
            delay = _retry_delay_seconds(attempts)
            retry_at = (attempted + timedelta(seconds=delay)).replace(microsecond=0)
            # 秒级存储会截断亚秒部分：若截断让窗口实际缩短（attempted 落在
            # 秒内后段），补一整秒，保证退避窗口不小于声明的延迟。
            if retry_at < attempted + timedelta(seconds=delay):
                retry_at += timedelta(seconds=1)
            retry_at = retry_at.strftime("%Y-%m-%dT%H:%M:%SZ")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """UPDATE ops_events
                   SET notify_state=?, notify_attempts=?, notified_at=?,
                       notify_attempted_at=?, notify_retry_at=?
                   WHERE id=?""",
                (
                    state,
                    int(attempts),
                    attempted_at if state == "sent" else "",
                    attempted_at,
                    retry_at,
                    event_id,
                ),
            )

    def notify_backoff_ready(self, event: dict[str, Any]) -> bool:
        """Exponential backoff for failed deliveries: 1, 2, 4… up to 30 min.

        ``notify_attempts == 0`` means never attempted (a fresh event):
        always immediately ready so alerts are not delayed by one sweep
        cycle. Backoff only applies once a real send has failed.
        """
        attempts = int(event.get("notify_attempts") or 0)
        if attempts <= 0:
            return True
        retry_at = _parse_iso(str(event.get("notify_retry_at") or ""))
        if retry_at is not None:
            return datetime.now(timezone.utc) >= retry_at
        attempted = _parse_iso(str(event.get("notify_attempted_at") or event.get("created_at") or ""))
        if attempted is None:
            return True
        age = (datetime.now(timezone.utc) - attempted).total_seconds()
        return age >= _retry_delay_seconds(attempts)

    def open_event_count(self) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM ops_events WHERE status='open'"
            ).fetchone()
        return int(row["n"] if row is not None else 0)
