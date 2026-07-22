"""SQLite persistence for automation runs and events."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from features.automation.models import (
    TERMINAL_STATUSES,
    utc_now_iso,
    validate_run_transition,
)


RUN_COLUMNS = [
    "id", "trace_id", "state_version", "recovery_count", "last_recovered_at",
    "source_type", "source_key", "profile_id", "project", "branch",
    "gerrit_change_id", "gerrit_patchset", "gerrit_subject", "owner", "created_by",
    "status", "current_stage", "jenkins_job", "jenkins_queue_url", "jenkins_build_number",
    "jenkins_build_url", "artifact_url", "artifact_path", "build_artifact_id",
    "worker_id", "device_reservation_id", "flash_stage_id", "flash_command_id",
    "cluster_job_id", "attempt_id", "devices_json", "test_plan_json",
    "report_timestamp", "report_id", "result_json", "error",
    "created_at", "updated_at", "started_at", "finished_at", "lease_owner",
    "lease_expires_at",
]

RUN_SUMMARY_COLUMNS = [
    "id", "trace_id", "state_version", "recovery_count", "last_recovered_at",
    "source_type", "source_key", "profile_id", "project", "branch",
    "gerrit_change_id", "gerrit_patchset", "gerrit_subject", "owner", "created_by",
    "status", "current_stage", "jenkins_build_number", "artifact_url",
    "artifact_path", "build_artifact_id", "worker_id", "device_reservation_id",
    "flash_command_id", "cluster_job_id", "attempt_id", "devices_json",
    "report_timestamp", "report_id", "error",
    "created_at", "updated_at", "started_at", "finished_at",
]


class AutomationStore:
    _REQUIRED_TABLES = frozenset({
        "automation_runs",
        "automation_run_events",
        "automation_run_secrets",
    })

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
        with self._schema_lock, self._open_connection() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS automation_runs (
                    id TEXT PRIMARY KEY,
                    trace_id TEXT NOT NULL DEFAULT '',
                    state_version INTEGER NOT NULL DEFAULT 1,
                    recovery_count INTEGER NOT NULL DEFAULT 0,
                    last_recovered_at TEXT NOT NULL DEFAULT '',
                    source_type TEXT NOT NULL,
                    source_key TEXT NOT NULL,
                    profile_id TEXT NOT NULL,
                    project TEXT NOT NULL,
                    branch TEXT NOT NULL,
                    gerrit_change_id TEXT NOT NULL,
                    gerrit_patchset TEXT NOT NULL,
                    gerrit_subject TEXT NOT NULL,
                    owner TEXT NOT NULL,
                    created_by TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    current_stage TEXT NOT NULL,
                    jenkins_job TEXT NOT NULL,
                    jenkins_queue_url TEXT NOT NULL DEFAULT '',
                    jenkins_build_number TEXT NOT NULL,
                    jenkins_build_url TEXT NOT NULL,
                    artifact_url TEXT NOT NULL,
                    artifact_path TEXT NOT NULL,
                    build_artifact_id TEXT NOT NULL DEFAULT '',
                    worker_id TEXT NOT NULL DEFAULT '',
                    device_reservation_id TEXT NOT NULL DEFAULT '',
                    flash_stage_id TEXT NOT NULL DEFAULT '',
                    flash_command_id TEXT NOT NULL DEFAULT '',
                    cluster_job_id TEXT NOT NULL DEFAULT '',
                    attempt_id TEXT NOT NULL DEFAULT '',
                    devices_json TEXT NOT NULL,
                    test_plan_json TEXT NOT NULL,
                    report_timestamp TEXT NOT NULL,
                    report_id TEXT NOT NULL DEFAULT '',
                    result_json TEXT NOT NULL,
                    error TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT NOT NULL,
                    lease_owner TEXT NOT NULL DEFAULT '',
                    lease_expires_at TEXT NOT NULL DEFAULT ''
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_automation_runs_status ON automation_runs(status, updated_at)")
            self._ensure_column(conn, "automation_runs", "jenkins_queue_url", "TEXT NOT NULL DEFAULT ''")
            for column, definition in {
                "created_by": "TEXT NOT NULL DEFAULT ''",
                "build_artifact_id": "TEXT NOT NULL DEFAULT ''",
                "worker_id": "TEXT NOT NULL DEFAULT ''",
                "device_reservation_id": "TEXT NOT NULL DEFAULT ''",
                "flash_stage_id": "TEXT NOT NULL DEFAULT ''",
                "flash_command_id": "TEXT NOT NULL DEFAULT ''",
                "cluster_job_id": "TEXT NOT NULL DEFAULT ''",
                "attempt_id": "TEXT NOT NULL DEFAULT ''",
                "report_id": "TEXT NOT NULL DEFAULT ''",
                "lease_owner": "TEXT NOT NULL DEFAULT ''",
                "lease_expires_at": "TEXT NOT NULL DEFAULT ''",
                "trace_id": "TEXT NOT NULL DEFAULT ''",
                "state_version": "INTEGER NOT NULL DEFAULT 1",
                "recovery_count": "INTEGER NOT NULL DEFAULT 0",
                "last_recovered_at": "TEXT NOT NULL DEFAULT ''",
            }.items():
                self._ensure_column(conn, "automation_runs", column, definition)
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_automation_runs_source_key ON automation_runs(source_key) WHERE source_key != ''")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS automation_run_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    level TEXT NOT NULL,
                    message TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    trace_id TEXT NOT NULL DEFAULT '',
                    event_type TEXT NOT NULL DEFAULT 'log',
                    operation_id TEXT NOT NULL DEFAULT '',
                    from_status TEXT NOT NULL DEFAULT '',
                    to_status TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_automation_events_run ON automation_run_events(run_id, id)")
            for column, definition in {
                "trace_id": "TEXT NOT NULL DEFAULT ''",
                "event_type": "TEXT NOT NULL DEFAULT 'log'",
                "operation_id": "TEXT NOT NULL DEFAULT ''",
                "from_status": "TEXT NOT NULL DEFAULT ''",
                "to_status": "TEXT NOT NULL DEFAULT ''",
            }.items():
                self._ensure_column(conn, "automation_run_events", column, definition)
            conn.execute(
                "UPDATE automation_runs SET trace_id=id WHERE trace_id=''"
            )
            conn.execute(
                """UPDATE automation_run_events SET trace_id=COALESCE(
                       (SELECT trace_id FROM automation_runs
                        WHERE automation_runs.id=automation_run_events.run_id),
                       run_id)
                   WHERE trace_id=''"""
            )
            conn.execute("""
                CREATE TABLE IF NOT EXISTS automation_run_secrets (
                    run_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    encrypted_value TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (run_id, name),
                    FOREIGN KEY (run_id) REFERENCES automation_runs(id) ON DELETE CASCADE
                )
            """)
        self.db_path.chmod(0o600)

    @staticmethod
    def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row else None

    @staticmethod
    def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
        columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def create_run(
        self,
        run: dict[str, Any],
        *,
        encrypted_secrets: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        values = {column: str(run.get(column, "")) for column in RUN_COLUMNS}
        placeholders = ", ".join("?" for _ in RUN_COLUMNS)
        columns_sql = ", ".join(RUN_COLUMNS)
        with self._connect() as conn:
            conn.execute(
                f"INSERT INTO automation_runs ({columns_sql}) VALUES ({placeholders})",
                [values[column] for column in RUN_COLUMNS],
            )
            for name, encrypted_value in (encrypted_secrets or {}).items():
                conn.execute(
                    """INSERT INTO automation_run_secrets
                       (run_id, name, encrypted_value, updated_at)
                       VALUES (?, ?, ?, ?)""",
                    (
                        values["id"],
                        str(name),
                        str(encrypted_value),
                        utc_now_iso(),
                    ),
                )
        return self.get_run(values["id"])

    def get_run_secret(self, run_id: str, name: str) -> str:
        with self._connect() as conn:
            row = conn.execute(
                """SELECT encrypted_value FROM automation_run_secrets
                   WHERE run_id = ? AND name = ?""",
                (run_id, name),
            ).fetchone()
        return str(row["encrypted_value"]) if row else ""

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM automation_runs WHERE id = ?", (run_id,)).fetchone()
        return self._row_to_dict(row)

    def get_run_by_source_key(self, source_key: str) -> dict[str, Any] | None:
        if not source_key:
            return None
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM automation_runs WHERE source_key = ?", (source_key,)).fetchone()
        return self._row_to_dict(row)

    def list_runs(
        self, status: str = "", limit: int = 50, created_by: str = ""
    ) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit or 50), 500))
        where = []
        params: list[Any] = []
        if status:
            where.append("status = ?")
            params.append(status)
        if created_by:
            where.append("created_by = ?")
            params.append(created_by)
        where_sql = f" WHERE {' AND '.join(where)}" if where else ""
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM automation_runs{where_sql} "
                "ORDER BY created_at DESC, id DESC LIMIT ?",
                [*params, limit],
            ).fetchall()
        return [dict(row) for row in rows]

    def list_run_summaries(
        self, status: str = "", limit: int = 50, created_by: str = ""
    ) -> list[dict[str, Any]]:
        """Return list fields without transferring potentially multi-megabyte result JSON."""
        limit = max(1, min(int(limit or 50), 500))
        columns = ", ".join(RUN_SUMMARY_COLUMNS)
        where = []
        params: list[Any] = []
        if status:
            where.append("status = ?")
            params.append(status)
        if created_by:
            where.append("created_by = ?")
            params.append(created_by)
        where_sql = f" WHERE {' AND '.join(where)}" if where else ""
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT {columns} FROM automation_runs{where_sql} "
                "ORDER BY created_at DESC, id DESC LIMIT ?",
                [*params, limit],
            ).fetchall()
        return [dict(row) for row in rows]

    def list_active_runs(self, limit: int = 100) -> list[dict[str, Any]]:
        """Return runnable work in least-recently-advanced order.

        Scheduling from ``list_runs`` is subtly unfair because that method is
        intentionally newest-first for the UI. A long-polling newest run can
        otherwise monopolize every worker tick and starve all older work.
        """
        limit = max(1, min(int(limit or 100), 500))
        terminal = sorted(TERMINAL_STATUSES)
        placeholders = ", ".join("?" for _ in terminal)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM automation_runs
                WHERE status NOT IN ({placeholders})
                ORDER BY updated_at ASC, created_at ASC, id ASC
                LIMIT ?
                """,
                [*terminal, limit],
            ).fetchall()
        return [dict(row) for row in rows]

    def update_run(self, run_id: str, **updates: Any) -> dict[str, Any]:
        allowed = set(RUN_COLUMNS) - {
            "id", "created_at", "state_version", "recovery_count", "last_recovered_at"
        }
        clean = {key: str(value) for key, value in updates.items() if key in allowed}
        clean["updated_at"] = utc_now_iso()
        assignments = ", ".join(f"{key} = ?" for key in clean)
        with self._connect() as conn:
            conn.execute(
                f"UPDATE automation_runs SET {assignments} WHERE id = ?",
                [*clean.values(), run_id],
            )
        return self.get_run(run_id)

    def update_run_if_status(
        self, run_id: str, expected_status: str, **updates: Any
    ) -> tuple[dict[str, Any], bool]:
        """Compare-and-swap a transition so cancel and worker races cannot revive a run."""
        allowed = set(RUN_COLUMNS) - {
            "id", "created_at", "state_version", "recovery_count", "last_recovered_at"
        }
        clean = {key: str(value) for key, value in updates.items() if key in allowed}
        target_status = clean.get("status", expected_status)
        validate_run_transition(expected_status, target_status)
        clean["updated_at"] = utc_now_iso()
        assignments = ", ".join(f"{key} = ?" for key in clean)
        if target_status != expected_status:
            assignments += ", state_version = state_version + 1"
        with self._connect() as conn:
            cursor = conn.execute(
                f"UPDATE automation_runs SET {assignments} WHERE id = ? AND status = ?",
                [*clean.values(), run_id, expected_status],
            )
        current = self.get_run(run_id)
        if current is None:
            raise ValueError(f"automation run not found: {run_id}")
        return current, cursor.rowcount == 1

    @staticmethod
    def _lease_expiry(seconds: int) -> str:
        return (datetime.now(timezone.utc) + timedelta(seconds=max(10, seconds))).isoformat(
            timespec="microseconds"
        ).replace("+00:00", "Z")

    def claim_next_active(self, owner: str, lease_seconds: int = 120) -> dict[str, Any] | None:
        terminal = sorted(TERMINAL_STATUSES)
        placeholders = ", ".join("?" for _ in terminal)
        now = utc_now_iso()
        expiry = self._lease_expiry(lease_seconds)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                f"""
                SELECT id,lease_owner FROM automation_runs
                WHERE status NOT IN ({placeholders})
                  AND (lease_owner='' OR lease_expires_at='' OR lease_expires_at < ?)
                ORDER BY updated_at ASC, created_at ASC, id ASC LIMIT 1
                """,
                [*terminal, now],
            ).fetchone()
            if row is None:
                return None
            recovered = bool(row["lease_owner"] and row["lease_owner"] != owner)
            cursor = conn.execute(
                """UPDATE automation_runs SET lease_owner=?,lease_expires_at=?,
                          recovery_count=recovery_count+?,
                          last_recovered_at=CASE WHEN ? THEN ? ELSE last_recovered_at END
                   WHERE id=? AND (lease_owner='' OR lease_expires_at='' OR lease_expires_at < ?)""",
                (owner, expiry, 1 if recovered else 0, 1 if recovered else 0,
                 now, row["id"], now),
            )
            if cursor.rowcount != 1:
                return None
        if recovered:
            self.append_event(
                row["id"],
                "recovery",
                "warning",
                "Controller reclaimed an expired automation lease",
                {"previous_owner": row["lease_owner"], "new_owner": owner},
                event_type="run.recovered",
                operation_id=owner,
            )
        return self.get_run(row["id"])

    def claim_run(self, run_id: str, owner: str, lease_seconds: int = 120) -> bool:
        now = utc_now_iso()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute(
                "SELECT lease_owner FROM automation_runs WHERE id=?", (run_id,)
            ).fetchone()
            recovered = bool(
                current and current["lease_owner"] and current["lease_owner"] != owner
            )
            cursor = conn.execute(
                """UPDATE automation_runs SET lease_owner=?,lease_expires_at=?,
                          recovery_count=recovery_count+?,
                          last_recovered_at=CASE WHEN ? THEN ? ELSE last_recovered_at END
                   WHERE id=? AND (lease_owner='' OR lease_owner=? OR lease_expires_at='' OR lease_expires_at < ?)""",
                (owner, self._lease_expiry(lease_seconds), 1 if recovered else 0,
                 1 if recovered else 0, now, run_id, owner, now),
            )
        if cursor.rowcount == 1 and recovered:
            self.append_event(
                run_id,
                "recovery",
                "warning",
                "Controller reclaimed an expired automation lease",
                {"previous_owner": current["lease_owner"], "new_owner": owner},
                event_type="run.recovered",
                operation_id=owner,
            )
        return cursor.rowcount == 1

    def release_claim(self, run_id: str, owner: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE automation_runs SET lease_owner='',lease_expires_at='' WHERE id=? AND lease_owner=?",
                (run_id, owner),
            )

    def renew_claim(self, run_id: str, owner: str, lease_seconds: int = 120) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                """UPDATE automation_runs SET lease_expires_at=?
                   WHERE id=? AND lease_owner=?""",
                (self._lease_expiry(lease_seconds), run_id, owner),
            )
        return cursor.rowcount == 1

    def append_event(
        self,
        run_id: str,
        stage: str,
        level: str,
        message: str,
        payload: dict[str, Any] | None = None,
        *,
        event_type: str = "log",
        operation_id: str = "",
        from_status: str = "",
        to_status: str = "",
    ) -> dict[str, Any]:
        created_at = utc_now_iso()
        run = self.get_run(run_id) or {}
        trace_id = str(run.get("trace_id") or run_id)
        structured_payload = {
            "run_id": run_id,
            "trace_id": trace_id,
            "operation_id": operation_id,
            **(payload or {}),
        }
        payload_json = json.dumps(
            structured_payload, ensure_ascii=False, separators=(",", ":")
        )
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO automation_run_events
                    (run_id,stage,level,message,payload_json,trace_id,event_type,
                     operation_id,from_status,to_status,created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (run_id, stage, level, message, payload_json, trace_id, event_type,
                 operation_id, from_status, to_status, created_at),
            )
            event_id = cur.lastrowid
            row = conn.execute("SELECT * FROM automation_run_events WHERE id = ?", (event_id,)).fetchone()
        return dict(row)

    def list_events(self, run_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM automation_run_events WHERE run_id = ? ORDER BY id ASC",
                (run_id,),
            ).fetchall()
        return [dict(row) for row in rows]
