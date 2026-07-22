"""Durable owner-scoped state for local suite download/extract tasks."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any


class SuiteTaskStore:
    ACTIVE_STATES = {"queued", "downloading", "extracting", "recovering"}

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self._schema_lock = threading.RLock()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _initialize(self) -> None:
        with self._schema_lock, self._open_connection() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS suite_tasks (
                    task_id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    kind TEXT NOT NULL CHECK(kind IN ('download', 'extract')),
                    status TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_suite_tasks_owner_updated
                    ON suite_tasks(owner_id, updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_suite_tasks_kind_status
                    ON suite_tasks(kind, status, updated_at DESC);
                """
            )
        os.chmod(self.db_path, 0o600)

    def _open_connection(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.execute("PRAGMA busy_timeout=30000")
        connection.row_factory = sqlite3.Row
        return connection

    def _connect(self) -> sqlite3.Connection:
        connection = self._open_connection()
        schema_exists = connection.execute(
            """SELECT 1 FROM sqlite_master
               WHERE type='table' AND name='suite_tasks'"""
        ).fetchone() is not None
        if not schema_exists:
            connection.close()
            self._initialize()
            connection = self._open_connection()
        return connection

    @staticmethod
    def _decode(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return json.loads(str(row["payload_json"])) if row else None

    def create(self, task: dict[str, Any]) -> None:
        owner_id = str(task.get("owner_id") or "").strip()
        task_id = str(task.get("task_id") or "").strip()
        kind = str(task.get("kind") or "").strip()
        if not owner_id or not task_id or kind not in {"download", "extract"}:
            raise ValueError("suite task_id, owner_id and kind are required")
        created_at = float(task.get("created_at") or time.time())
        payload = dict(task)
        payload["created_at"] = created_at
        payload["updated_at"] = float(payload.get("updated_at") or created_at)
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO suite_tasks
                       (task_id, owner_id, kind, status, created_at,
                        updated_at, payload_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    task_id,
                    owner_id,
                    kind,
                    str(payload.get("status") or "queued"),
                    created_at,
                    float(payload["updated_at"]),
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                ),
            )

    def update(self, task_id: str, **updates: Any) -> dict[str, Any] | None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT payload_json FROM suite_tasks WHERE task_id=?", (task_id,)
            ).fetchone()
            task = self._decode(row)
            if task is None:
                connection.rollback()
                return None
            task.update(updates)
            task["updated_at"] = time.time()
            connection.execute(
                """UPDATE suite_tasks
                   SET status=?, updated_at=?, payload_json=?
                   WHERE task_id=?""",
                (
                    str(task.get("status") or "queued"),
                    float(task["updated_at"]),
                    json.dumps(task, ensure_ascii=False, separators=(",", ":")),
                    task_id,
                ),
            )
        return task

    def get(self, task_id: str, owner_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT payload_json FROM suite_tasks
                   WHERE task_id=? AND owner_id=?""",
                (task_id, owner_id),
            ).fetchone()
        return self._decode(row)

    def list_active(self) -> list[dict[str, Any]]:
        placeholders = ",".join("?" for _ in self.ACTIVE_STATES)
        with self._connect() as connection:
            rows = connection.execute(
                f"""SELECT payload_json FROM suite_tasks
                    WHERE status IN ({placeholders}) ORDER BY created_at""",
                tuple(sorted(self.ACTIVE_STATES)),
            ).fetchall()
        return [self._decode(row) or {} for row in rows]

    def find_active_download(self, archive_path: str) -> dict[str, Any] | None:
        for task in self.list_active():
            if task.get("kind") == "download" and task.get(
                "archive_path"
            ) == archive_path:
                return task
        return None

    def delete_finished_before(self, cutoff: float) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                """DELETE FROM suite_tasks
                   WHERE status IN ('completed', 'error', 'interrupted')
                     AND updated_at < ?""",
                (float(cutoff),),
            )
        return int(cursor.rowcount)
