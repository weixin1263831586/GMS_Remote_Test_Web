"""Daily Brief 持久化（per-owner SQLite，独立于 evidence/dashboard 库）。

表结构（见 _init_db）：
- redmine_daily_brief_runs：一次晨报运行的元数据与最终报告；
- redmine_daily_brief_issues：run 内每个 issue 的分析状态与结构化结果。

幂等约束：owner_id + brief_date + mode 唯一——同一天同模式只允许一个
有效 nightly run（重复触发按状态复用/重试，见 service 层）。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from pathlib import Path
from typing import Any

from .daily_brief_models import DailyBriefIssue, DailyBriefRun
from .users import _now, owner_redmine_root


RUN_COLUMNS = (
    "run_id", "owner_id", "brief_date", "mode", "status",
    "started_at", "finished_at", "snapshot_at", "snapshot_hash",
    "issue_count", "waiting_my_reply_count", "no_reply_3_days_count",
    "urgent_count", "analysis_backend", "model_name", "prompt_version",
    "report_json", "report_markdown", "error",
)
ISSUE_COLUMNS = (
    "run_id", "issue_id", "buckets", "priority", "priority_score",
    "fingerprint", "status", "started_at", "finished_at", "duration_ms",
    "attempt_count", "error", "error_type", "raw_response", "result",
)


def new_run_id() -> str:
    return "db_" + uuid.uuid4().hex


class DailyBriefRepository:
    """一个 owner 一个实例；线程安全由 RLock + SQLite 自身保证。"""

    def __init__(self, owner_root: Path):
        self.owner_root = Path(owner_root)
        self.db_path = self.owner_root / "daily_brief.sqlite3"
        self._lock = threading.RLock()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    # ------------------------------------------------------------------ schema

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS redmine_daily_brief_runs (
                    run_id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    brief_date TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    started_at TEXT NOT NULL DEFAULT '',
                    finished_at TEXT NOT NULL DEFAULT '',
                    snapshot_at TEXT NOT NULL DEFAULT '',
                    snapshot_hash TEXT NOT NULL DEFAULT '',
                    issue_count INTEGER NOT NULL DEFAULT 0,
                    waiting_my_reply_count INTEGER NOT NULL DEFAULT 0,
                    no_reply_3_days_count INTEGER NOT NULL DEFAULT 0,
                    urgent_count INTEGER NOT NULL DEFAULT 0,
                    analysis_backend TEXT NOT NULL DEFAULT '',
                    model_name TEXT NOT NULL DEFAULT '',
                    prompt_version TEXT NOT NULL DEFAULT '',
                    report_json TEXT NOT NULL DEFAULT '{}',
                    report_markdown TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL DEFAULT ''
                )
                """
            )
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_brief_runs_identity
                ON redmine_daily_brief_runs(owner_id, brief_date, mode)
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS redmine_daily_brief_issues (
                    run_id TEXT NOT NULL,
                    issue_id INTEGER NOT NULL,
                    buckets TEXT NOT NULL DEFAULT '[]',
                    priority TEXT NOT NULL DEFAULT 'P3',
                    priority_score INTEGER NOT NULL DEFAULT 0,
                    fingerprint TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'pending',
                    started_at TEXT NOT NULL DEFAULT '',
                    finished_at TEXT NOT NULL DEFAULT '',
                    duration_ms INTEGER NOT NULL DEFAULT 0,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    error TEXT NOT NULL DEFAULT '',
                    error_type TEXT NOT NULL DEFAULT '',
                    raw_response TEXT NOT NULL DEFAULT '',
                    result TEXT NOT NULL DEFAULT '{}',
                    PRIMARY KEY (run_id, issue_id)
                )
                """
            )

    # ------------------------------------------------------------------ runs

    def create_run(self, run: DailyBriefRun) -> DailyBriefRun | None:
        """插入 run；同 owner+date+mode 已存在时返回既有记录（幂等）。"""
        now = _now()
        with self._lock, self._connect() as conn:
            existing = conn.execute(
                "SELECT * FROM redmine_daily_brief_runs WHERE owner_id=? AND brief_date=? AND mode=?",
                (run.owner_id, run.brief_date, run.mode),
            ).fetchone()
            if existing is not None:
                return self._row_to_run(existing)
            conn.execute(
                f"INSERT INTO redmine_daily_brief_runs ({', '.join(RUN_COLUMNS)}, created_at, updated_at) "
                f"VALUES ({', '.join('?' * len(RUN_COLUMNS))}, ?, ?)",
                [*self._run_params(run), now, now],
            )
        return run

    def get_run(self, run_id: str) -> DailyBriefRun | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM redmine_daily_brief_runs WHERE run_id=?", (run_id,)
            ).fetchone()
            return self._row_to_run(row) if row else None

    def find_run(self, owner_id: str, brief_date: str, mode: str) -> DailyBriefRun | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM redmine_daily_brief_runs WHERE owner_id=? AND brief_date=? AND mode=?",
                (owner_id, brief_date, mode),
            ).fetchone()
            return self._row_to_run(row) if row else None

    def latest_run(self, owner_id: str, brief_date: str | None = None) -> DailyBriefRun | None:
        """最新一次任意模式的 run（brief_date 缺省为最新日期）。"""
        with self._connect() as conn:
            if brief_date:
                row = conn.execute(
                    "SELECT * FROM redmine_daily_brief_runs WHERE owner_id=? AND brief_date=? "
                    "ORDER BY brief_date DESC, started_at DESC, rowid DESC LIMIT 1",
                    (owner_id, brief_date),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM redmine_daily_brief_runs WHERE owner_id=? "
                    "ORDER BY brief_date DESC, started_at DESC, rowid DESC LIMIT 1",
                    (owner_id,),
                ).fetchone()
            return self._row_to_run(row) if row else None

    def update_run(self, run: DailyBriefRun) -> bool:
        now = _now()
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                f"UPDATE redmine_daily_brief_runs SET {', '.join(f'{c}=?' for c in RUN_COLUMNS)}, "
                "updated_at=? WHERE run_id=?",
                [*self._run_params(run), now, run.run_id],
            )
            return bool(cursor.rowcount)

    # ------------------------------------------------------------------ issues

    def upsert_issue(self, issue: DailyBriefIssue) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                f"INSERT OR REPLACE INTO redmine_daily_brief_issues ({', '.join(ISSUE_COLUMNS)}) "
                f"VALUES ({', '.join('?' * len(ISSUE_COLUMNS))})",
                self._issue_params(issue),
            )

    def list_issues(self, run_id: str) -> list[DailyBriefIssue]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM redmine_daily_brief_issues WHERE run_id=? "
                "ORDER BY priority_score DESC, issue_id ASC",
                (run_id,),
            ).fetchall()
            return [self._row_to_issue(row) for row in rows]

    def get_issue(self, run_id: str, issue_id: int) -> DailyBriefIssue | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM redmine_daily_brief_issues WHERE run_id=? AND issue_id=?",
                (run_id, issue_id),
            ).fetchone()
            return self._row_to_issue(row) if row else None

    def reset_stale_running(self, older_than_iso: str) -> int:
        """进程崩溃恢复：把长时间 running/pending 的 run 标记为 failed。"""
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                "UPDATE redmine_daily_brief_runs SET status='failed', "
                "error='interrupted by process restart', updated_at=? "
                "WHERE status IN ('pending','snapshotting','analyzing') AND started_at < ?",
                (_now(), older_than_iso),
            )
            return cursor.rowcount

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _run_params(run: DailyBriefRun) -> list[Any]:
        values = run.to_row()
        values["report_json"] = json.dumps(values["report_json"], ensure_ascii=False, sort_keys=True)
        return [values[col] for col in RUN_COLUMNS]

    @staticmethod
    def _row_to_run(row: sqlite3.Row) -> DailyBriefRun:
        data = {col: row[col] for col in RUN_COLUMNS}
        try:
            data["report_json"] = json.loads(data["report_json"] or "{}")
        except json.JSONDecodeError:
            data["report_json"] = {}
        return DailyBriefRun(**data)

    @staticmethod
    def _issue_params(issue: DailyBriefIssue) -> list[Any]:
        values = issue.to_row()
        values["buckets"] = json.dumps(values["buckets"], ensure_ascii=False)
        values["result"] = json.dumps(values["result"], ensure_ascii=False, sort_keys=True)
        return [values[col] for col in ISSUE_COLUMNS]

    @staticmethod
    def _row_to_issue(row: sqlite3.Row) -> DailyBriefIssue:
        data = {col: row[col] for col in ISSUE_COLUMNS}
        try:
            data["buckets"] = json.loads(data["buckets"] or "[]")
        except json.JSONDecodeError:
            data["buckets"] = []
        try:
            data["result"] = json.loads(data["result"] or "{}")
        except json.JSONDecodeError:
            data["result"] = {}
        return DailyBriefIssue(**data)


_REPO_LOCK = threading.Lock()
_REPO_CACHE: dict[str, DailyBriefRepository] = {}


def owner_daily_brief_repository(owner_id: str) -> DailyBriefRepository:
    """按 owner 缓存 repository（数据落在 owner 隔离目录下）。"""
    key = str(owner_id or "anonymous")
    with _REPO_LOCK:
        repo = _REPO_CACHE.get(key)
        if repo is None:
            repo = DailyBriefRepository(owner_redmine_root(key))
            _REPO_CACHE[key] = repo
        return repo
