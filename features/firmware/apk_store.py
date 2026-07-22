"""Durable owner-scoped metadata for APK/JAR analysis tasks."""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any


class ApkTaskStore:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self._schema_lock = threading.RLock()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _initialize(self) -> None:
        with self._schema_lock, self._open_connection() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS apk_analysis_tasks (
                    task_id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    timestamp REAL NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_apk_tasks_owner_timestamp
                    ON apk_analysis_tasks(owner_id, timestamp DESC);
                """
            )
        self.db_path.chmod(0o600)

    def _open_connection(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.execute("PRAGMA busy_timeout=30000")
        conn.row_factory = sqlite3.Row
        return conn

    def _connect(self) -> sqlite3.Connection:
        conn = self._open_connection()
        schema_exists = conn.execute(
            """SELECT 1 FROM sqlite_master
               WHERE type='table' AND name='apk_analysis_tasks'"""
        ).fetchone() is not None
        if not schema_exists:
            conn.close()
            self._initialize()
            conn = self._open_connection()
        return conn

    @staticmethod
    def _serializable(task: dict[str, Any]) -> dict[str, Any]:
        return {
            key: value
            for key, value in dict(task or {}).items()
            if key != "symbol_index"
        }

    def upsert(self, task_id: str, task: dict[str, Any]) -> None:
        payload = self._serializable(task)
        owner_id = str(payload.get("owner_id") or "").strip()
        if not owner_id:
            raise ValueError("APK task owner_id is required")
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO apk_analysis_tasks
                       (task_id, owner_id, status, timestamp, payload_json)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(task_id) DO UPDATE SET
                       owner_id=excluded.owner_id,
                       status=excluded.status,
                       timestamp=excluded.timestamp,
                       payload_json=excluded.payload_json""",
                (
                    task_id,
                    owner_id,
                    str(payload.get("status") or "uploaded"),
                    float(payload.get("timestamp") or 0),
                    json.dumps(
                        payload,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                ),
            )

    def get(self, task_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload_json FROM apk_analysis_tasks WHERE task_id=?",
                (task_id,),
            ).fetchone()
        return json.loads(str(row["payload_json"])) if row else None

    def list(self) -> dict[str, dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT task_id, payload_json FROM apk_analysis_tasks
                   ORDER BY timestamp DESC"""
            ).fetchall()
        return {
            str(row["task_id"]): json.loads(str(row["payload_json"]))
            for row in rows
        }

    def delete(self, task_id: str) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM apk_analysis_tasks WHERE task_id=?",
                (task_id,),
            )
        return cursor.rowcount == 1
