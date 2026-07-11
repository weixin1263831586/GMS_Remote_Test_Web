from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from features.build.models import utc_now_iso


JOB_COLUMNS = [
    "id", "server_id", "template_id", "source_type", "source_key", "owner",
    "automation_run_id", "status", "remote_session", "remote_workspace",
    "remote_log_path", "command", "parameters_json", "artifact_json", "error",
    "created_at", "updated_at", "started_at", "finished_at",
]


class BuildStore:
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
                CREATE TABLE IF NOT EXISTS build_jobs (
                    id TEXT PRIMARY KEY,
                    server_id TEXT NOT NULL,
                    template_id TEXT NOT NULL,
                    source_type TEXT NOT NULL,
                    source_key TEXT NOT NULL,
                    owner TEXT NOT NULL,
                    automation_run_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    remote_session TEXT NOT NULL,
                    remote_workspace TEXT NOT NULL,
                    remote_log_path TEXT NOT NULL,
                    command TEXT NOT NULL,
                    parameters_json TEXT NOT NULL,
                    artifact_json TEXT NOT NULL,
                    error TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT NOT NULL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_build_jobs_status ON build_jobs(status, updated_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_build_jobs_source ON build_jobs(source_key)")

    @staticmethod
    def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row else None

    def create_job(self, job: dict[str, Any]) -> dict[str, Any]:
        values = {column: str(job.get(column, "")) for column in JOB_COLUMNS}
        columns_sql = ", ".join(JOB_COLUMNS)
        placeholders = ", ".join("?" for _ in JOB_COLUMNS)
        with self._connect() as conn:
            conn.execute(
                f"INSERT INTO build_jobs ({columns_sql}) VALUES ({placeholders})",
                [values[column] for column in JOB_COLUMNS],
            )
        return self.get_job(values["id"])

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM build_jobs WHERE id = ?", (job_id,)).fetchone()
        return self._row_to_dict(row)

    def get_job_by_source_key(self, source_key: str) -> dict[str, Any] | None:
        if not source_key:
            return None
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM build_jobs WHERE source_key = ? ORDER BY created_at DESC LIMIT 1",
                (source_key,),
            ).fetchone()
        return self._row_to_dict(row)

    def list_jobs(self, status: str = "", limit: int = 50) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit or 50), 500))
        with self._connect() as conn:
            if status:
                rows = conn.execute(
                    "SELECT * FROM build_jobs WHERE status = ? ORDER BY created_at DESC LIMIT ?",
                    (status, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM build_jobs ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return [dict(row) for row in rows]

    def update_job(self, job_id: str, **updates: Any) -> dict[str, Any]:
        allowed = set(JOB_COLUMNS) - {"id", "created_at"}
        clean = {key: str(value) for key, value in updates.items() if key in allowed}
        clean["updated_at"] = utc_now_iso()
        assignments = ", ".join(f"{key} = ?" for key in clean)
        with self._connect() as conn:
            conn.execute(
                f"UPDATE build_jobs SET {assignments} WHERE id = ?",
                [*clean.values(), job_id],
            )
        return self.get_job(job_id)

    def claim_queued_job(
        self,
        job_id: str,
        *,
        server_id: str,
        max_concurrent: int,
    ) -> dict[str, Any] | None:
        """Atomically claim a queued job for one worker.

        The conditional update is the cross-process mutex. Returning ``None``
        means another worker already claimed, cancelled, or completed it.
        """
        now = utc_now_iso()
        with self._connect() as conn:
            # Protect capacity calculation and state transition in one
            # cross-process write transaction.
            conn.execute("BEGIN IMMEDIATE")
            if max_concurrent > 0:
                running = conn.execute(
                    """
                    SELECT COUNT(*) FROM build_jobs
                    WHERE server_id = ? AND status = 'running'
                    """,
                    (server_id,),
                ).fetchone()[0]
                if running >= max_concurrent:
                    return None
            cursor = conn.execute(
                """
                UPDATE build_jobs
                SET status = 'running', started_at = ?, updated_at = ?
                WHERE id = ? AND status = 'queued'
                """,
                (now, now, job_id),
            )
            if cursor.rowcount != 1:
                return None
            row = conn.execute(
                "SELECT * FROM build_jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
        return self._row_to_dict(row)

    @staticmethod
    def decode_artifacts(job: dict[str, Any]) -> list[dict[str, Any]]:
        try:
            artifacts = json.loads(job.get("artifact_json") or "[]")
            return artifacts if isinstance(artifacts, list) else []
        except json.JSONDecodeError:
            return []
