"""Durable suite download/extraction tasks and owner-scoped state."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from . import runtime
from .suite_archives import extract_archive_local_with_progress
from .suite_download_security import (
    curl_resolve_arguments as _curl_resolve_arguments,
)
from .suite_download_security import (
    resolve_suite_download_target as _resolve_suite_download_target,
)


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


logger = logging.getLogger(__name__)

_SUITE_TASK_TTL = 3600
MAX_SUITE_ARCHIVE_BYTES = 50 * 1024 * 1024 * 1024
_last_suite_cleanup = 0.0


def update_suite_task(task_id: str, **updates):
    runtime.suite_task_store.update(task_id, **updates)


def cleanup_old_suite_tasks():
    global _last_suite_cleanup
    now = time.time()
    if now - _last_suite_cleanup < 60:
        return
    _last_suite_cleanup = now
    runtime.suite_task_store.delete_finished_before(now - _SUITE_TASK_TTL)


def schedule_suite_task(coroutine) -> asyncio.Task:
    task = asyncio.create_task(coroutine)
    runtime.global_state.background_tasks.add(task)
    task.add_done_callback(runtime.global_state.background_tasks.discard)
    return task


def _parse_curl_size(s: str) -> float:
    s = s.rstrip(",").lower()
    if s.endswith("g"):
        return float(s[:-1]) * 1024 ** 3
    if s.endswith("m"):
        return float(s[:-1]) * 1024 ** 2
    if s.endswith("k"):
        return float(s[:-1]) * 1024
    return float(s)


async def run_suite_download_task(task_id: str, url: str, archive_path: str):
    part_path = archive_path + ".part"
    filename = os.path.basename(archive_path)
    # 禁止重定向，防止 Location 绕过 SSRF 地址校验。
    try:
        target = await asyncio.to_thread(_resolve_suite_download_target, url)
    except ValueError as exc:
        update_suite_task(
            task_id,
            status="error",
            error=f"Download target rejected: {exc}",
            retryable=False,
        )
        return
    # --fail：HTTP 4xx/5xx 时 curl 以 22 退出而不是把错误页 body 存成
    # “下载完成”的归档；--max-redirs 0 防止 Location 绕过 SSRF 校验。
    cmd = ["curl", "--proto", "=http,https", "--fail", "--max-redirs", "0", *_curl_resolve_arguments(target), "-C", "-", "--max-filesize", str(MAX_SUITE_ARCHIVE_BYTES), "--connect-timeout", "30", "--max-time", "7200", "--retry", "3", "--retry-delay", "5", "-o", part_path, target.url]
    update_suite_task(task_id, status="downloading", progress=0, message=f"Downloading: {filename}")

    process = None
    try:
        process = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        buffer = ""
        while True:
            chunk = await process.stderr.read(4096)
            if not chunk:
                break
            buffer += chunk.decode("utf-8", errors="ignore")
            while "\r" in buffer:
                line, buffer = buffer.split("\r", 1)
                line = line.strip()
                if not line:
                    continue
                parts = line.split()
                if len(parts) >= 6:
                    try:
                        pct = float(parts[0])
                        total = _parse_curl_size(parts[1])
                        speed = _parse_curl_size(parts[-1])
                        downloaded = total * pct / 100
                        progress = pct if pct > 0 else 0
                        update_suite_task(task_id, progress=min(progress, 99.0), downloaded_size=int(downloaded), total_size=int(total), speed_bps=int(speed))
                    except (ValueError, ZeroDivisionError):
                        pass

        await process.wait()
        if process.returncode != 0:
            # curl exit 63 means --max-filesize was exceeded; discard that
            # oversized partial. Network failures retain .part so the next
            # task can actually resume via -C -.
            if process.returncode == 63 and os.path.exists(part_path):
                with contextlib.suppress(OSError):
                    os.remove(part_path)
            update_suite_task(
                task_id,
                status="error",
                progress=0,
                error=f"Download failed (exit code {process.returncode})",
                resumable=os.path.exists(part_path),
            )
            return

        os.replace(part_path, archive_path)
        file_size = os.path.getsize(archive_path)
        update_suite_task(task_id, status="completed", progress=100, downloaded_size=file_size, archive_path=archive_path, speed_bps=0, message=f"Download complete: {filename}")
    except asyncio.CancelledError:
        if process and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        update_suite_task(
            task_id,
            status="recovering",
            message="Download paused during Controller shutdown",
            resumable=os.path.exists(part_path),
        )
        raise
    except Exception as e:
        if process and process.returncode is None:
            process.kill()
            await process.wait()
        # 任务 error 会随任务状态接口回传给所有者：原文只进日志。
        logger.error(f"[Suite Download] Failed: {e}", exc_info=True)
        update_suite_task(
            task_id, status="error", error="套件下载失败（详见服务端日志）"
        )


def _extract_archive_with_progress(archive_path: str, extract_dir: str, target_dir_name: str, task_id: str | None = None) -> dict:
    return extract_archive_local_with_progress(
        archive_path,
        extract_dir,
        target_dir_name,
        task_id,
        update_suite_task,
    )


async def run_suite_extract_task(task_id: str, archive_path: str, extract_dir: str, target_dir_name: str):
    try:
        update_suite_task(task_id, status="extracting", progress=0, message="Extracting...")
        result = await asyncio.to_thread(
            _extract_archive_with_progress,
            archive_path, extract_dir, target_dir_name, task_id)
        update_suite_task(task_id, status="completed", progress=100, **result)
    except Exception as e:
        logger.error(f"[Suite Extract] Failed: {e}")
        update_suite_task(task_id, status="error", error=f"Extraction failed: {e!s}")
