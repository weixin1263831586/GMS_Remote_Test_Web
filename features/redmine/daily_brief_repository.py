"""Daily Brief 持久化（per-owner SQLite，独立于 evidence/dashboard 库）。

表结构（见 _init_db）：
- redmine_daily_brief_runs：一次晨报运行的元数据与最终报告；
- redmine_daily_brief_issues：run 内每个 issue 的分析状态与结构化结果。

幂等约束：owner_id + brief_date + mode 唯一——同一天同模式只允许一个
有效 nightly run（重复触发按状态复用/重试，见 service 层）。
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import uuid
from pathlib import Path
from typing import Any

from .daily_brief_jobs import JOB_KINDS as JOB_KINDS
from .daily_brief_jobs import DailyBriefJobStore
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
    "urgent_count", "analysis_backend", "model_name", "device_serial", "analysis_hint", "prompt_version",
    "report_json", "report_markdown", "error",
)
ISSUE_COLUMNS = (
    "run_id", "issue_id", "buckets", "priority", "priority_score",
    "fingerprint", "subject", "status", "started_at", "finished_at",
    "duration_ms", "attempt_count", "error", "error_type", "raw_response",
    "result",
)
TERMINAL_RUN_STATUSES = frozenset({"completed", "partial", "failed", "cancelled"})


def new_run_id() -> str:
    return "db_" + uuid.uuid4().hex


class DailyBriefRepository:
    """一个 owner 一个实例；线程安全由 RLock + SQLite 自身保证。"""

    # 当前 schema 版本（PRAGMA user_version）。每次改 _init_db 的表结构
    # 都必须 +1，让旧库在下一次启动时重放迁移；版本历史见
    # docs/redmine-daily-brief.md 的 schema migration 契约一节。
    _SCHEMA_VERSION = 4

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
            # 跨进程 schema 迁移必须持有 SQLite 写锁：Web 进程、daily_brief
            # worker、systemd、CLI 可能并发初始化同一库，进程内 _lock 覆盖
            # 不了 TOCTOU——两进程同时 PRAGMA table_info 判列缺失、同时
            # ALTER TABLE ADD COLUMN 会以 "duplicate column name" 失败。
            # 先 BEGIN IMMEDIATE 拿写锁，再读 schema、再迁移，迁移天然幂等。
            #
            # Schema versioning uses PRAGMA user_version.
            # 已是当前版本的库直接跳过全部 DDL（快路径）；旧库按迁移步骤
            # 逐版升级，每步自身幂等（列存在检查在写锁内进行，无竞态），
            # 即使 user_version 意外回退/丢失也能安全重放。
            conn.execute("BEGIN IMMEDIATE")
            current_version = conn.execute("PRAGMA user_version").fetchone()[0]
            if current_version == self._SCHEMA_VERSION:
                conn.commit()
                return
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
                    device_serial TEXT NOT NULL DEFAULT '',
                    analysis_hint TEXT NOT NULL DEFAULT '',
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
            if "device_serial" not in run_cols:
                conn.execute(
                    "ALTER TABLE redmine_daily_brief_runs "
                    "ADD COLUMN device_serial TEXT NOT NULL DEFAULT ''"
                )
            if "analysis_hint" not in run_cols:
                conn.execute(
                    "ALTER TABLE redmine_daily_brief_runs "
                    "ADD COLUMN analysis_hint TEXT NOT NULL DEFAULT ''"
                )
            # 历史取消只收敛了 run，遗留 issue 仍为 pending/running，UI 会
            # 优先展示成“排队中”。一次性收敛这些不可能再执行的条目。
            now = _now()
            conn.execute(
                "UPDATE redmine_daily_brief_issues SET status='cancelled', "
                "finished_at=CASE WHEN finished_at='' THEN ? ELSE finished_at END, "
                "error='', error_type='' "
                "WHERE status IN ('pending','running') AND EXISTS ("
                "SELECT 1 FROM redmine_daily_brief_runs AS run "
                "WHERE run.run_id=redmine_daily_brief_issues.run_id "
                "AND run.status='cancelled')",
                (now,),
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
            # 全部迁移完成后盖章：后续启动走快路径，不再重复 DDL。
            # user_version 在同一写事务内设置，崩溃回滚后自然重放迁移。
            conn.execute(f"PRAGMA user_version = {self._SCHEMA_VERSION}")
            conn.commit()

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
        """Latest morning brief; independent issue diagnostics do not replace it."""
        with self._connect() as conn:
            if brief_date:
                row = conn.execute(
                    "SELECT * FROM redmine_daily_brief_runs WHERE owner_id=? AND brief_date=? AND mode NOT LIKE 'issue:%' "
                    "ORDER BY brief_date DESC, started_at DESC, rowid DESC LIMIT 1",
                    (owner_id, brief_date),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM redmine_daily_brief_runs WHERE owner_id=? AND mode NOT LIKE 'issue:%' "
                    "ORDER BY brief_date DESC, started_at DESC, rowid DESC LIMIT 1",
                    (owner_id,),
                ).fetchone()
            return self._row_to_run(row) if row else None

    def latest_active_issue_run(self, owner_id: str) -> DailyBriefRun | None:
        """Return the owner's newest in-flight standalone issue analysis."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM redmine_daily_brief_runs WHERE owner_id=? "
                "AND mode LIKE 'issue:%' "
                "AND status IN ('pending', 'snapshotting', 'analyzing') "
                "AND EXISTS (SELECT 1 FROM redmine_daily_brief_jobs AS job "
                "WHERE job.run_id=redmine_daily_brief_runs.run_id "
                "AND job.status IN ('queued','running')) "
                "ORDER BY started_at DESC, rowid DESC LIMIT 1",
                (owner_id,),
            ).fetchone()
            return self._row_to_run(row) if row else None

    def latest_issue_runs(self, owner_id: str, limit: int = 30) -> list[DailyBriefRun]:
        """Return the newest saved standalone analysis for each Redmine issue.

        Standalone full analyses have a versioned mode (``issue:<id>:full:…``)
        while earlier installations used ``issue:<id>``.  Grouping here keeps
        both formats visible as one history entry per Redmine number.
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM redmine_daily_brief_runs WHERE owner_id=? "
                "AND mode LIKE 'issue:%' "
                "ORDER BY started_at DESC, rowid DESC",
                (owner_id,),
            ).fetchall()
        latest: dict[int, DailyBriefRun] = {}
        for row in rows:
            match = re.match(r"^issue:(\d+)(?::|$)", str(row["mode"] or ""))
            if not match:
                continue
            issue_id = int(match.group(1))
            if issue_id not in latest:
                latest[issue_id] = self._row_to_run(row)
            if len(latest) >= max(1, min(int(limit), 100)):
                break
        return list(latest.values())

    def latest_issue_run(self, owner_id: str, issue_id: int) -> DailyBriefRun | None:
        """Newest standalone analysis for one issue, including legacy runs."""
        # Do not route this through the bounded history view: an older issue
        # must still be found when a user enters its exact Redmine number.
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM redmine_daily_brief_runs WHERE owner_id=? "
                "AND mode LIKE ? ORDER BY started_at DESC, rowid DESC",
                (owner_id, f"issue:{int(issue_id)}%"),
            ).fetchall()
        for row in rows:
            run = self._row_to_run(row)
            match = re.match(r"^issue:(\d+)(?::|$)", run.mode)
            if match and int(match.group(1)) == int(issue_id):
                return run
        return None

    def latest_issue_run_with_report(
        self, owner_id: str, issue_id: int, *, exclude_run_id: str = ""
    ) -> tuple[DailyBriefRun, DailyBriefIssue] | None:
        """Return the newest standalone run that still has a displayable report.

        A cancelled retry may be newer than a prior completed conclusion. Keep
        the retry as the row's execution state, but allow the UI to retain the
        earlier report rather than making the conclusion appear deleted.
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM redmine_daily_brief_runs WHERE owner_id=? "
                "AND mode LIKE ? ORDER BY started_at DESC, rowid DESC",
                (owner_id, f"issue:{int(issue_id)}%"),
            ).fetchall()
            for row in rows:
                run = self._row_to_run(row)
                if run.run_id == exclude_run_id:
                    continue
                match = re.match(r"^issue:(\d+)(?::|$)", run.mode)
                if not match or int(match.group(1)) != int(issue_id):
                    continue
                issue_row = conn.execute(
                    "SELECT * FROM redmine_daily_brief_issues "
                    "WHERE run_id=? AND issue_id=?",
                    (run.run_id, issue_id),
                ).fetchone()
                if issue_row is None:
                    continue
                issue = self._row_to_issue(issue_row)
                if str((issue.result or {}).get("detailed_report") or "").strip():
                    return run, issue
        return None

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

    def cancel_queued_issue_run(self, run_id: str) -> bool:
        """Immediately converge an unclaimed standalone issue run to cancelled.

        A queued job has no Worker process to observe ``cancel_requested`` yet.
        Leaving it pending makes the UI revert to “排队中” after refresh and
        can let a later Worker run work the user already stopped. Running jobs
        remain cooperative: their Worker owns the final transition.
        """
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            run = conn.execute(
                "SELECT mode, status FROM redmine_daily_brief_runs WHERE run_id=?",
                (run_id,),
            ).fetchone()
            if run is None or not str(run["mode"] or "").startswith("issue:"):
                return False
            if run["status"] in TERMINAL_RUN_STATUSES:
                return False
            running = conn.execute(
                "SELECT 1 FROM redmine_daily_brief_jobs "
                "WHERE run_id=? AND status='running' LIMIT 1",
                (run_id,),
            ).fetchone()
            if running is not None:
                return False
            now = _now()
            conn.execute(
                "UPDATE redmine_daily_brief_jobs SET status='cancelled', "
                "finished_at=?, error='cancelled before execution' "
                "WHERE run_id=? AND status='queued'",
                (now, run_id),
            )
            conn.execute(
                "UPDATE redmine_daily_brief_issues SET status='cancelled', "
                "finished_at=?, error='', error_type='' "
                "WHERE run_id=? AND status IN ('pending','running')",
                (now, run_id),
            )
            cursor = conn.execute(
                "UPDATE redmine_daily_brief_runs SET status='cancelled', error='', "
                "finished_at=?, updated_at=? WHERE run_id=?",
                (now, now, run_id),
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
        """Recover orphan runs only; live jobs remain active regardless of age."""
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                "UPDATE redmine_daily_brief_runs SET status='failed', "
                "error='interrupted by process restart', updated_at=? "
                "WHERE status IN ('pending','snapshotting','analyzing') AND started_at < ? "
                "AND NOT EXISTS (SELECT 1 FROM redmine_daily_brief_jobs AS job "
                "WHERE job.run_id=redmine_daily_brief_runs.run_id "
                "AND job.status IN ('queued','running'))",
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

    def create_run_and_enqueue_job(self, run: DailyBriefRun, *, issue: DailyBriefIssue | None = None) -> tuple[bool, dict[str, Any], bool]:
        """Create/reuse a run, optional single issue and job in one write transaction."""
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
            job, job_created = self.jobs.insert_job(conn, run_id, kind="issue" if issue else "run", issue_id=issue.issue_id if issue else 0)
            if job_created:
                if issue:
                    issue.run_id = run_id
                    conn.execute(
                        f"INSERT OR REPLACE INTO redmine_daily_brief_issues ({', '.join(ISSUE_COLUMNS)}) VALUES ({', '.join('?' * len(ISSUE_COLUMNS))})",
                        self._issue_params(issue),
                    )
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

    def list_ai_executions_for_run(self, run_id: str) -> list[dict[str, Any]]:
        """Return sanitized attempt payloads for a run-level aggregate."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT issue_id, payload_json, created_at "
                "FROM redmine_daily_brief_ai_executions "
                "WHERE run_id=? ORDER BY rowid",
                (run_id,),
            ).fetchall()
        executions: list[dict[str, Any]] = []
        for row in rows:
            try:
                payload = json.loads(row["payload_json"] or "{}")
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            payload["issue_id"] = int(row["issue_id"])
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
