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

from .daily_brief_jobs import JOB_KINDS, DailyBriefJobStore
from .daily_brief_models import (
    DailyBriefIssue,
    DailyBriefRun,
    derive_data_quality,  # noqa: F401 - re-exported for service/report use
)
from .users import _now, owner_redmine_root


RUN_COLUMNS = (
    "run_id", "owner_id", "brief_date", "mode", "status",
    "started_at", "finished_at", "snapshot_at", "snapshot_hash",
    "source_sync_status", "data_quality", "last_sync_at",
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
JOB_KINDS = frozenset({"run", "issue"})  # noqa: F811 - re-exported legacy name
TERMINAL_RUN_STATUSES = frozenset({"completed", "partial", "failed", "cancelled"})


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
        self.jobs = DailyBriefJobStore(self)

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
            # 协作式取消：API 写标志位，执行循环轮询后收敛为 cancelled。
            # 幂等迁移——旧库补列，新库建表语句本身也不含此列（控制面
            # 状态不属于 RUN_COLUMNS 数据面，避免经 update_run 被覆写）。
            try:
                conn.execute(
                    "ALTER TABLE redmine_daily_brief_runs "
                    "ADD COLUMN cancel_requested INTEGER NOT NULL DEFAULT 0"
                )
            except sqlite3.OperationalError:
                pass  # column already exists
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
            # 旧库迁移：数据新鲜度拆分（execution status ≠ data quality）。
            for column in ("data_quality", "last_sync_at"):
                if column not in run_cols:
                    conn.execute(
                        f"ALTER TABLE redmine_daily_brief_runs "
                        f"ADD COLUMN {column} TEXT NOT NULL DEFAULT ''"
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
                    lease_token TEXT NOT NULL DEFAULT '',
                    lease_expires_at TEXT NOT NULL DEFAULT '',
                    requested_at TEXT NOT NULL DEFAULT '',
                    started_at TEXT NOT NULL DEFAULT '',
                    finished_at TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT ''
                )
                """
            )
            # 旧库迁移：lease_token 列（2026-09 加入，防旧 Worker 在租约
            # 被新 Worker 重新获取后覆盖新状态）。
            job_cols = {row[1] for row in conn.execute(
                "PRAGMA table_info(redmine_daily_brief_jobs)"
            )}
            if "lease_token" not in job_cols:
                conn.execute(
                    "ALTER TABLE redmine_daily_brief_jobs "
                    "ADD COLUMN lease_token TEXT NOT NULL DEFAULT ''"
                )
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_brief_jobs_active
                ON redmine_daily_brief_jobs(run_id, kind, issue_id)
                WHERE status IN ('queued', 'running')
                """
            )
            # AI 执行轨迹（每次 attempt 一行）：session_id / tool trace 摘要
            # / token usage。完整输出留在 kkagent session，本表只存证据索引
            # （Evidence Provenance 的落库起点）。
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS redmine_daily_brief_ai_executions (
                    execution_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    issue_id INTEGER NOT NULL,
                    attempt_no INTEGER NOT NULL DEFAULT 1,
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL DEFAULT '',
                    finished_at TEXT NOT NULL DEFAULT ''
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_brief_ai_exec_run_issue
                ON redmine_daily_brief_ai_executions(run_id, issue_id)
                """
            )

    # ------------------------------------------------------------------ runs

    def create_run(self, run: DailyBriefRun) -> DailyBriefRun | None:
        """插入 run；同 owner+date+mode 已存在时返回既有记录（幂等）。

        跨进程一致性由 SQLite 承担：ON CONFLICT DO NOTHING 的原子插入
        代替 SELECT→判断→INSERT（Web 进程、Worker、nightly/delta CLI 可能
        并发创建同一 owner+date+mode 的 run，Python 进程内锁无法覆盖）。
        """
        now = _now()
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                f"INSERT INTO redmine_daily_brief_runs ({', '.join(RUN_COLUMNS)}, created_at, updated_at) "
                f"VALUES ({', '.join('?' * len(RUN_COLUMNS))}, ?, ?) "
                "ON CONFLICT(owner_id, brief_date, mode) DO NOTHING",
                [*self._run_params(run), now, now],
            )
            if cursor.rowcount == 1:
                return run
            row = conn.execute(
                "SELECT * FROM redmine_daily_brief_runs WHERE owner_id=? AND brief_date=? AND mode=?",
                (run.owner_id, run.brief_date, run.mode),
            ).fetchone()
            return self._row_to_run(row) if row else None

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

    # ------------------------------------------------------------------ cancel

    def request_cancel(self, run_id: str) -> bool:
        """Request cooperative cancellation; no-op for terminal runs.

        独立于 update_run 走专用语句：cancel_requested 是控制面标志，
        不进 RUN_COLUMNS，避免执行循环每次 upsert run 时把它写回 0。
        """
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                "UPDATE redmine_daily_brief_runs SET cancel_requested=1, "
                f"updated_at=? WHERE run_id=? AND status NOT IN "
                f"({', '.join('?' * len(TERMINAL_RUN_STATUSES))})",
                (_now(), run_id, *TERMINAL_RUN_STATUSES),
            )
            return bool(cursor.rowcount)

    def is_cancel_requested(self, run_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT cancel_requested FROM redmine_daily_brief_runs WHERE run_id=?",
                (run_id,),
            ).fetchone()
            return bool(row and row["cancel_requested"])

    def clear_cancel(self, run_id: str) -> None:
        """复用 run 重新执行前清除取消标志（见 _reset_run_for_retry）。"""
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE redmine_daily_brief_runs SET cancel_requested=0, "
                "updated_at=? WHERE run_id=?",
                (_now(), run_id),
            )

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

    # durable job 队列已拆分至 daily_brief_jobs.DailyBriefJobStore；这里
    # 保留薄委托，既有调用方（dispatch/worker/service/测试）无需改动。
    # 原子化入口（run+job 同一事务）见 create_run_and_enqueue_job。

    def enqueue_job(
        self, run_id: str, *, kind: str = "run", issue_id: int = 0
    ) -> tuple[dict[str, Any], bool]:
        return self.jobs.enqueue_job(run_id, kind=kind, issue_id=issue_id)

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        return self.jobs.get_job(job_id)

    def has_active_job(self, run_id: str, kind: str | None = None) -> bool:
        return self.jobs.has_active_job(run_id, kind)

    def claim_next_job(self, worker_id: str, lease_seconds: int = 90):
        return self.jobs.claim_next_job(worker_id, lease_seconds)

    def renew_job(
        self, job_id: str, worker_id: str, lease_token: str, lease_seconds: int = 90
    ) -> bool:
        return self.jobs.renew_job(job_id, worker_id, lease_token, lease_seconds)

    def finish_job(
        self, job_id: str, worker_id: str, lease_token: str, *, error: str = ""
    ) -> bool:
        return self.jobs.finish_job(job_id, worker_id, lease_token, error=error)

    def requeue_job(
        self, job_id: str, worker_id: str, lease_token: str, *, reason: str
    ) -> bool:
        return self.jobs.requeue_job(job_id, worker_id, lease_token, reason=reason)

    def reconcile_orphan_runs(self, started_after_iso: str) -> int:
        return self.jobs.reconcile_orphan_runs(started_after_iso)

    def create_run_and_enqueue_job(self, run: DailyBriefRun) -> tuple[bool, dict[str, Any], bool]:
        """run 创建 + job 入队 = **同一个** BEGIN IMMEDIATE 事务。

        审核意见 P1：旧的两步路径（create_run 提交后再 enqueue_job）在
        进程崩溃时会留下「run=pending / 无 job」的孤儿——下一次触发看到
        already_running 直接复用，却永远没有 Worker 会执行它。

        返回 (run_created, job, job_created)。run 已存在（同 owner+date+mode）
        时复用既有 run：仍确保它有活动 job（幂等补队），调用方据
        run_created/job_created 区分新建与合流。
        """
        now = _now()
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                f"INSERT INTO redmine_daily_brief_runs ({', '.join(RUN_COLUMNS)}, created_at, updated_at) "
                f"VALUES ({', '.join('?' * len(RUN_COLUMNS))}, ?, ?) "
                "ON CONFLICT(owner_id, brief_date, mode) DO NOTHING",
                [*self._run_params(run), now, now],
            )
            run_created = cursor.rowcount == 1
            if not run_created:
                row = conn.execute(
                    "SELECT run_id FROM redmine_daily_brief_runs "
                    "WHERE owner_id=? AND brief_date=? AND mode=?",
                    (run.owner_id, run.brief_date, run.mode),
                ).fetchone()
                if row is None:  # pragma: no cover - conflict implies row
                    raise RuntimeError("run conflict but no existing row")
                run_id = str(row["run_id"])
            else:
                run_id = run.run_id
            job, job_created = self.jobs.insert_job(conn, run_id, kind="run", issue_id=0)
            if job_created:
                if not run_created:
                    self.jobs._reset_run_for_enqueue(conn, run_id)
                else:
                    conn.execute(
                        "UPDATE redmine_daily_brief_runs SET started_at=? WHERE run_id=?",
                        (now, run_id),
                    )
            return run_created, job, job_created

    def get_active_run_job(self, run_id: str) -> dict[str, Any] | None:
        """run 级活动 job（queued/running），供 dispatch 响应整形。"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM redmine_daily_brief_jobs WHERE run_id=? AND kind='run' "
                "AND status IN ('queued','running') ORDER BY requested_at LIMIT 1",
                (run_id,),
            ).fetchone()
            return dict(row) if row is not None else None

    # ----------------------------------------------------------- ai executions

    def record_ai_execution(self, run_id: str, issue_id: int, payload: dict[str, Any]) -> None:
        """持久化一次 AI attempt（session/trace/usage），供审计与 UI。"""
        now = _now()
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO redmine_daily_brief_ai_executions "
                "(execution_id, run_id, issue_id, attempt_no, payload_json, "
                " created_at, finished_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    "dbe_" + uuid.uuid4().hex,
                    run_id,
                    int(issue_id),
                    max(1, int(payload.get("attempt_no") or 1)),
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    now,
                    now,
                ),
            )

    def list_ai_executions(self, run_id: str, issue_id: int) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT payload_json, created_at FROM redmine_daily_brief_ai_executions "
                "WHERE run_id=? AND issue_id=? ORDER BY created_at, rowid",
                (run_id, int(issue_id)),
            ).fetchall()
            executions: list[dict[str, Any]] = []
            for row in rows:
                try:
                    payload = json.loads(row["payload_json"] or "{}")
                except json.JSONDecodeError:
                    continue
                if isinstance(payload, dict):
                    payload["recorded_at"] = row["created_at"]
                    executions.append(payload)
            return executions

    def latest_ai_executions_by_issue(self, run_id: str) -> dict[int, dict[str, Any]]:
        """一次查询返回 run 内每个 issue 最新的脱敏执行 payload。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT issue_id, payload_json, created_at "
                "FROM redmine_daily_brief_ai_executions "
                "WHERE run_id=? ORDER BY rowid",
                (run_id,),
            ).fetchall()
        latest: dict[int, dict[str, Any]] = {}
        for row in rows:
            try:
                payload = json.loads(row["payload_json"] or "{}")
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            payload["recorded_at"] = row["created_at"]
            latest[int(row["issue_id"])] = payload
        return latest

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
