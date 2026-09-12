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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .daily_brief_models import DailyBriefIssue, DailyBriefRun
from .users import _now, owner_redmine_root


RUN_COLUMNS = (
    "run_id", "owner_id", "brief_date", "mode", "status",
    "started_at", "finished_at", "snapshot_at", "snapshot_hash",
    "source_sync_status",
    "issue_count", "waiting_my_reply_count", "no_reply_3_days_count",
    "urgent_count", "analysis_backend", "model_name", "prompt_version",
    "report_json", "report_markdown", "error",
)
ISSUE_COLUMNS = (
    "run_id", "issue_id", "buckets", "priority", "priority_score",
    "fingerprint", "subject", "status", "started_at", "finished_at",
    "duration_ms", "attempt_count", "error", "error_type", "raw_response",
    "result",
)
JOB_KINDS = frozenset({"run", "issue"})
TERMINAL_RUN_STATUSES = frozenset({"completed", "partial", "failed"})


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
                    source_sync_status TEXT NOT NULL DEFAULT '',
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
                    subject TEXT NOT NULL DEFAULT '',
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
            # 旧库迁移：subject 列（2026-09 新增，供 UI 单行标题展示）。
            issue_cols = {row[1] for row in conn.execute(
                "PRAGMA table_info(redmine_daily_brief_issues)"
            )}
            if "subject" not in issue_cols:
                conn.execute(
                    "ALTER TABLE redmine_daily_brief_issues "
                    "ADD COLUMN subject TEXT NOT NULL DEFAULT ''"
                )
            # 冻结快照持久化：快照 JSON 与 run 分表存储，进程崩溃后重试
            # 复用同一份输入事实（冻结快照持久化修复）。
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS redmine_daily_brief_snapshots (
                    run_id TEXT PRIMARY KEY,
                    snapshot_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL DEFAULT ''
                )
                """
            )
            # 旧库迁移：source_sync_status 列（pre-sync 失败可见性）。
            run_cols = {row[1] for row in conn.execute(
                "PRAGMA table_info(redmine_daily_brief_runs)"
            )}
            if "source_sync_status" not in run_cols:
                conn.execute(
                    "ALTER TABLE redmine_daily_brief_runs "
                    "ADD COLUMN source_sync_status TEXT NOT NULL DEFAULT ''"
                )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS redmine_daily_brief_jobs (
                    job_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    issue_id INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'queued',
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    worker_id TEXT NOT NULL DEFAULT '',
                    lease_expires_at TEXT NOT NULL DEFAULT '',
                    requested_at TEXT NOT NULL DEFAULT '',
                    started_at TEXT NOT NULL DEFAULT '',
                    finished_at TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT ''
                )
                """
            )
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_brief_jobs_active
                ON redmine_daily_brief_jobs(run_id, kind, issue_id)
                WHERE status IN ('queued', 'running')
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

    def delete_issues(self, run_id: str) -> int:
        """清空一次 run 的旧 issue，供人工强制重跑时替换冻结快照。"""
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM redmine_daily_brief_issues WHERE run_id=?", (run_id,)
            )
            return cursor.rowcount

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

    # ------------------------------------------------------------------ jobs

    @staticmethod
    def _utc_now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

    @classmethod
    def _lease_expiry(cls, seconds: int) -> str:
        return (
            datetime.now(timezone.utc) + timedelta(seconds=max(10, int(seconds)))
        ).isoformat(timespec="seconds").replace("+00:00", "Z")

    @staticmethod
    def _job_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row is not None else None

    def enqueue_job(
        self, run_id: str, *, kind: str = "run", issue_id: int = 0
    ) -> tuple[dict[str, Any], bool]:
        """持久化一个 Web 请求；同一目标已有活动 job 时幂等复用。"""
        if kind not in JOB_KINDS:
            raise ValueError(f"unsupported daily brief job kind: {kind}")
        target_issue = int(issue_id or 0)
        if kind == "run":
            target_issue = 0
        now = self._utc_now()
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            run = conn.execute(
                "SELECT run_id FROM redmine_daily_brief_runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if run is None:
                raise ValueError(f"daily brief run not found: {run_id}")
            if kind == "issue":
                issue = conn.execute(
                    "SELECT issue_id FROM redmine_daily_brief_issues "
                    "WHERE run_id=? AND issue_id=?",
                    (run_id, target_issue),
                ).fetchone()
                if issue is None:
                    raise ValueError(f"issue {target_issue} not found in run {run_id}")
            existing = conn.execute(
                "SELECT * FROM redmine_daily_brief_jobs WHERE run_id=? "
                "AND status IN ('queued','running') ORDER BY requested_at LIMIT 1",
                (run_id,),
            ).fetchone()
            if existing is not None:
                return dict(existing), False
            job_id = "dbj_" + uuid.uuid4().hex
            conn.execute(
                "INSERT INTO redmine_daily_brief_jobs "
                "(job_id,run_id,kind,issue_id,status,requested_at) "
                "VALUES (?,?,?,?, 'queued', ?)",
                (job_id, run_id, kind, target_issue, now),
            )
            conn.execute(
                "UPDATE redmine_daily_brief_runs SET status='pending', finished_at='', "
                "error='', updated_at=? WHERE run_id=?",
                (_now(), run_id),
            )
            if kind == "issue":
                conn.execute(
                    "UPDATE redmine_daily_brief_issues SET status='pending', started_at='', "
                    "finished_at='', error='', error_type='' WHERE run_id=? AND issue_id=?",
                    (run_id, target_issue),
                )
            row = conn.execute(
                "SELECT * FROM redmine_daily_brief_jobs WHERE job_id=?", (job_id,)
            ).fetchone()
            return dict(row), True

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            return self._job_row(conn.execute(
                "SELECT * FROM redmine_daily_brief_jobs WHERE job_id=?", (job_id,)
            ).fetchone())

    def claim_next_job(self, worker_id: str, lease_seconds: int = 90) -> dict[str, Any] | None:
        """领取最早 job，并把租约过期的未完成任务安全放回队列。"""
        now = self._utc_now()
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            expired = conn.execute(
                "SELECT j.job_id,j.run_id,j.kind,j.issue_id,r.status AS run_status "
                "FROM redmine_daily_brief_jobs j JOIN redmine_daily_brief_runs r "
                "ON r.run_id=j.run_id WHERE j.status='running' "
                "AND j.lease_expires_at<>'' AND j.lease_expires_at<?",
                (now,),
            ).fetchall()
            for item in expired:
                if item["run_status"] in TERMINAL_RUN_STATUSES:
                    conn.execute(
                        "UPDATE redmine_daily_brief_jobs SET status='completed',worker_id='',"
                        "lease_expires_at='',finished_at=? WHERE job_id=?",
                        (now, item["job_id"]),
                    )
                    continue
                conn.execute(
                    "UPDATE redmine_daily_brief_jobs SET status='queued',worker_id='',"
                    "lease_expires_at='',error='worker lease expired; queued for retry' "
                    "WHERE job_id=?",
                    (item["job_id"],),
                )
                conn.execute(
                    "UPDATE redmine_daily_brief_runs SET status='pending',"
                    "error='worker interrupted; queued for retry',updated_at=? WHERE run_id=?",
                    (_now(), item["run_id"]),
                )
                if item["kind"] == "issue":
                    conn.execute(
                        "UPDATE redmine_daily_brief_issues SET status='pending' "
                        "WHERE run_id=? AND issue_id=?",
                        (item["run_id"], item["issue_id"]),
                    )
            row = conn.execute(
                "SELECT * FROM redmine_daily_brief_jobs WHERE status='queued' "
                "ORDER BY requested_at,job_id LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            cursor = conn.execute(
                "UPDATE redmine_daily_brief_jobs SET status='running',worker_id=?,"
                "lease_expires_at=?,started_at=?,attempt_count=attempt_count+1,error='' "
                "WHERE job_id=? AND status='queued'",
                (worker_id, self._lease_expiry(lease_seconds), now, row["job_id"]),
            )
            if cursor.rowcount != 1:
                return None
            return self._job_row(conn.execute(
                "SELECT * FROM redmine_daily_brief_jobs WHERE job_id=?", (row["job_id"],)
            ).fetchone())

    def renew_job(self, job_id: str, worker_id: str, lease_seconds: int = 90) -> bool:
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                "UPDATE redmine_daily_brief_jobs SET lease_expires_at=? "
                "WHERE job_id=? AND worker_id=? AND status='running'",
                (self._lease_expiry(lease_seconds), job_id, worker_id),
            )
            return cursor.rowcount == 1

    def finish_job(self, job_id: str, worker_id: str, *, error: str = "") -> bool:
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                "UPDATE redmine_daily_brief_jobs SET status=?,worker_id='',"
                "lease_expires_at='',finished_at=?,error=? "
                "WHERE job_id=? AND worker_id=? AND status='running'",
                ("failed" if error else "completed", self._utc_now(), error[:1000],
                 job_id, worker_id),
            )
            return cursor.rowcount == 1

    def requeue_job(self, job_id: str, worker_id: str, *, reason: str) -> bool:
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                "UPDATE redmine_daily_brief_jobs SET status='queued',worker_id='',"
                "lease_expires_at='',error=? WHERE job_id=? AND worker_id=? "
                "AND status='running'",
                (reason[:1000], job_id, worker_id),
            )
            if cursor.rowcount:
                row = conn.execute(
                    "SELECT run_id,kind,issue_id FROM redmine_daily_brief_jobs WHERE job_id=?",
                    (job_id,),
                ).fetchone()
                conn.execute(
                    "UPDATE redmine_daily_brief_runs SET status='pending',error=?,updated_at=? "
                    "WHERE run_id=?",
                    (reason[:1000], _now(), row["run_id"]),
                )
                if row["kind"] == "issue":
                    conn.execute(
                        "UPDATE redmine_daily_brief_issues SET status='pending' "
                        "WHERE run_id=? AND issue_id=?",
                        (row["run_id"], row["issue_id"]),
                    )
            return cursor.rowcount == 1

    # ------------------------------------------------------------------ snapshots

    def save_snapshot(self, run_id: str, snapshot: dict[str, Any]) -> None:
        """冻结快照落盘（run 级唯一）；重试/重分析均以此为准。"""
        payload = json.dumps(snapshot, ensure_ascii=False, sort_keys=True)
        now = _now()
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO redmine_daily_brief_snapshots "
                "(run_id, snapshot_json, created_at, updated_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(run_id) DO UPDATE SET snapshot_json=excluded.snapshot_json, "
                "updated_at=excluded.updated_at",
                (run_id, payload, now, now),
            )

    def get_snapshot(self, run_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT snapshot_json FROM redmine_daily_brief_snapshots WHERE run_id=?",
                (run_id,),
            ).fetchone()
            if row is None:
                return None
            try:
                data = json.loads(row["snapshot_json"] or "{}")
            except json.JSONDecodeError:
                return None
            return data if isinstance(data, dict) else None

    def delete_snapshot(self, run_id: str) -> int:
        """人工 force 重跑时删除冻结快照，允许重新冻结新事实。"""
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM redmine_daily_brief_snapshots WHERE run_id=?", (run_id,)
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
