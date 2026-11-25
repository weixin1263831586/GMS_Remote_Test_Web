from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from features.build.models import BuildServerConfig, BuildTemplateConfig, utc_now_iso


JOB_COLUMNS = [
    "id", "server_id", "template_id", "source_type", "source_key", "owner",
    "automation_run_id", "status", "remote_session", "remote_workspace",
    "remote_log_path", "command", "parameters_json", "artifact_json", "error",
    "created_at", "updated_at", "started_at", "finished_at",
]


class BuildStore:
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
        schema_exists = conn.execute(
            """SELECT 1 FROM sqlite_master
               WHERE type='table' AND name='build_jobs'"""
        ).fetchone() is not None
        if not schema_exists:
            conn.close()
            self._init_schema()
            conn = self._open_connection()
        return conn

    def _init_schema(self) -> None:
        with self._schema_lock, self._open_connection() as conn:
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

    def list_jobs(
        self, status: str = "", limit: int = 50, *, owner: str | None = None
    ) -> list[dict[str, Any]]:
        """Apply owner/status filters before LIMIT so other accounts never
        hide a user's builds from their own recent-job listing.

        owner=None is an explicit privileged all-owner query; owner="" is
        an owner filter, not a request to list everything.
        """
        limit = max(1, min(int(limit or 50), 500))
        conditions = []
        params: list[Any] = []
        if owner is not None:
            conditions.append("owner = ?")
            params.append(owner)
        if status:
            conditions.append("status = ?")
            params.append(status)
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM build_jobs" + where
                + " ORDER BY created_at DESC, id DESC LIMIT ?",
                (*params, limit),
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

    def delete_job(self, job_id: str) -> bool:
        with self._connect() as conn:
            cursor = conn.execute("DELETE FROM build_jobs WHERE id = ?", (job_id,))
        return cursor.rowcount == 1

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


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8") or "{}")


class BuildConfigRepository:
    def __init__(self, config_path: str | Path):
        self.config_path = Path(config_path)

    def load(self) -> dict[str, Any]:
        return _read_json(self.config_path)

    def list_servers(self) -> list[dict[str, Any]]:
        data = self.load()
        items = []
        for raw in data.get("servers") or []:
            if not isinstance(raw, dict):
                continue
            try:
                item = BuildServerConfig(**raw).model_dump()
            except Exception:
                continue
            if item["id"]:
                items.append(item)
        return items

    def get_server(self, server_id: str) -> dict[str, Any] | None:
        return next((item for item in self.list_servers() if item["id"] == server_id), None)

    def list_templates(self, enabled_only: bool = False) -> list[dict[str, Any]]:
        data = self.load()
        items = []
        for raw in data.get("templates") or []:
            if not isinstance(raw, dict):
                continue
            try:
                item = BuildTemplateConfig(**raw).model_dump()
            except Exception:
                continue
            if item["id"] and (not enabled_only or item["enabled"]):
                items.append(item)
        return items

    def get_template(self, template_id: str) -> dict[str, Any] | None:
        return next((item for item in self.list_templates() if item["id"] == template_id), None)
