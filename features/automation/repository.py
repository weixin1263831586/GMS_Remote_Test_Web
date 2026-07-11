"""SQLite persistence for automation runs and events."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from features.automation.models import TERMINAL_STATUSES, utc_now_iso


RUN_COLUMNS = [
    "id", "source_type", "source_key", "profile_id", "project", "branch",
    "gerrit_change_id", "gerrit_patchset", "gerrit_subject", "owner",
    "status", "current_stage", "jenkins_job", "jenkins_queue_url", "jenkins_build_number",
    "jenkins_build_url", "artifact_url", "artifact_path", "devices_json",
    "test_plan_json", "report_timestamp", "result_json", "error",
    "created_at", "updated_at", "started_at", "finished_at",
]

RUN_SUMMARY_COLUMNS = [
    "id", "source_type", "source_key", "profile_id", "project", "branch",
    "gerrit_change_id", "gerrit_patchset", "gerrit_subject", "owner",
    "status", "current_stage", "jenkins_build_number", "artifact_url",
    "artifact_path", "devices_json", "report_timestamp", "error",
    "created_at", "updated_at", "started_at", "finished_at",
]


class AutomationStore:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS automation_runs (
                    id TEXT PRIMARY KEY,
                    source_type TEXT NOT NULL,
                    source_key TEXT NOT NULL,
                    profile_id TEXT NOT NULL,
                    project TEXT NOT NULL,
                    branch TEXT NOT NULL,
                    gerrit_change_id TEXT NOT NULL,
                    gerrit_patchset TEXT NOT NULL,
                    gerrit_subject TEXT NOT NULL,
                    owner TEXT NOT NULL,
                    status TEXT NOT NULL,
                    current_stage TEXT NOT NULL,
                    jenkins_job TEXT NOT NULL,
                    jenkins_queue_url TEXT NOT NULL DEFAULT '',
                    jenkins_build_number TEXT NOT NULL,
                    jenkins_build_url TEXT NOT NULL,
                    artifact_url TEXT NOT NULL,
                    artifact_path TEXT NOT NULL,
                    devices_json TEXT NOT NULL,
                    test_plan_json TEXT NOT NULL,
                    report_timestamp TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    error TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT NOT NULL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_automation_runs_status ON automation_runs(status, updated_at)")
            self._ensure_column(conn, "automation_runs", "jenkins_queue_url", "TEXT NOT NULL DEFAULT ''")
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_automation_runs_source_key ON automation_runs(source_key) WHERE source_key != ''")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS automation_run_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    level TEXT NOT NULL,
                    message TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_automation_events_run ON automation_run_events(run_id, id)")

    @staticmethod
    def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row else None

    @staticmethod
    def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
        columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def create_run(self, run: dict[str, Any]) -> dict[str, Any]:
        values = {column: str(run.get(column, "")) for column in RUN_COLUMNS}
        placeholders = ", ".join("?" for _ in RUN_COLUMNS)
        columns_sql = ", ".join(RUN_COLUMNS)
        with self._connect() as conn:
            conn.execute(
                f"INSERT INTO automation_runs ({columns_sql}) VALUES ({placeholders})",
                [values[column] for column in RUN_COLUMNS],
            )
        return self.get_run(values["id"])

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

    def list_runs(self, status: str = "", limit: int = 50) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit or 50), 500))
        with self._connect() as conn:
            if status:
                rows = conn.execute(
                    "SELECT * FROM automation_runs WHERE status = ? ORDER BY created_at DESC, id DESC LIMIT ?",
                    (status, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM automation_runs ORDER BY created_at DESC, id DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return [dict(row) for row in rows]

    def list_run_summaries(self, status: str = "", limit: int = 50) -> list[dict[str, Any]]:
        """Return list fields without transferring potentially multi-megabyte result JSON."""
        limit = max(1, min(int(limit or 50), 500))
        columns = ", ".join(RUN_SUMMARY_COLUMNS)
        with self._connect() as conn:
            if status:
                rows = conn.execute(
                    f"SELECT {columns} FROM automation_runs WHERE status = ? "
                    "ORDER BY created_at DESC, id DESC LIMIT ?",
                    (status, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    f"SELECT {columns} FROM automation_runs "
                    "ORDER BY created_at DESC, id DESC LIMIT ?",
                    (limit,),
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
        allowed = set(RUN_COLUMNS) - {"id", "created_at"}
        clean = {key: str(value) for key, value in updates.items() if key in allowed}
        clean["updated_at"] = utc_now_iso()
        assignments = ", ".join(f"{key} = ?" for key in clean)
        with self._connect() as conn:
            conn.execute(
                f"UPDATE automation_runs SET {assignments} WHERE id = ?",
                [*clean.values(), run_id],
            )
        return self.get_run(run_id)

    def append_event(self, run_id: str, stage: str, level: str, message: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        created_at = utc_now_iso()
        payload_json = json.dumps(payload or {}, ensure_ascii=False, separators=(",", ":"))
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO automation_run_events (run_id, stage, level, message, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (run_id, stage, level, message, payload_json, created_at),
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
