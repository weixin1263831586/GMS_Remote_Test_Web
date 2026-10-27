"""Durable owner-scoped metadata for APK/JAR analysis tasks."""

from __future__ import annotations

import json
import logging
import shutil
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any


logger = logging.getLogger(__name__)

# 上传中断对账冷却期：目录 mtime 距今不足该值时跳过——分块上传可能
# 持续较久，冷却期内绝不删除，宁可让孤儿目录多活一个周期。
ORPHAN_UPLOAD_COOLDOWN_SECONDS = 24 * 60 * 60


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

    def known_task_ids(self) -> set[str]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT task_id FROM apk_analysis_tasks"
            ).fetchall()
        return {str(row["task_id"]) for row in rows}

    def reconcile_orphan_uploads(
        self, *, now: float | None = None,
    ) -> list[str]:
        """删除无任务行且无活跃上传会话的遗留目录，返回已清理的 task_id。

        目录在上传开始时创建、任务行在 finalize 时落库：进程在上传
        中途退出（或 finalize 前异常）会留下无 DB 行的目录，逐个累积
        可到数百 MB（2026-10 审核发现 378MB）。安全规则：
        - 目录名必须是合法 UUID（normalize_apk_task_id 只允许 UUID），
          其它名字一律不碰；
        - 目录里存在 ``upload_metadata.json`` 或 ``*.part*`` 分块视为
          活跃/可续传的上传会话，跳过；
        - 目录 mtime 在冷却期内的跳过（慢速分块上传保护）。
        单目录删除失败只记日志，不中断整体对账。
        """
        if now is None:
            now = time.time()
        root = self.db_path.parent
        if not root.is_dir():
            return []
        known = self.known_task_ids()
        cleaned: list[str] = []
        try:
            entries = list(root.iterdir())
        except OSError:
            return []
        for entry in entries:
            if not entry.is_dir():
                continue
            try:
                uuid.UUID(entry.name)
            except ValueError:
                continue  # 非任务目录（tasks.sqlite3 等）一律不碰
            if entry.name in known:
                continue
            try:
                has_session = (entry / "upload_metadata.json").is_file() or any(
                    entry.glob("*.part*")
                )
                age = now - entry.stat().st_mtime
            except OSError:
                continue
            if has_session or age < ORPHAN_UPLOAD_COOLDOWN_SECONDS:
                continue
            try:
                shutil.rmtree(entry, ignore_errors=True)
                cleaned.append(entry.name)
            except OSError:
                logger.warning("Failed to clean orphan APK upload dir %s", entry.name, exc_info=True)
        if cleaned:
            logger.info(
                "Reconciled %d orphan APK upload dir(s): %s",
                len(cleaned), ", ".join(cleaned),
            )
        return cleaned
