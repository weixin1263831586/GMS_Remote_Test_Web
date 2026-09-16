"""Daily Brief durable job 队列（从 repository 拆出的独立模块）。

租约模型：claim 时生成一次性 lease_token，renew/finish/requeue 全部以
(job_id, worker_id, lease_token) 做 CAS——租约被其他 Worker 接管后旧
Worker 的迟到写入会被拒绝。

审核意见 P1 修复落点：
- ``has_active_job``：already_running 分支必须校验真的有活动 job；
- ``create_run_and_enqueue_job``（repository 内，跨 runs+jobs 同一
  BEGIN IMMEDIATE 事务）：消灭「run 已建 / job 未入队」的孤儿窗口；
- ``reconcile_orphan_runs``：Worker 启动时给 active run 且无活动 job
  的孤儿补一个 queued job。
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from .users import _now


if TYPE_CHECKING:  # pragma: no cover
    from .daily_brief_repository import DailyBriefRepository

JOB_KINDS = frozenset({"run", "issue"})
ACTIVE_JOB_STATUSES = ("queued", "running")
ACTIVE_RUN_STATUSES = ("pending", "snapshotting", "analyzing")


def new_job_id() -> str:
    return "dbj_" + uuid.uuid4().hex


class DailyBriefJobStore:
    """一个 owner 一个实例；SQL 面向 redmine_daily_brief_jobs。"""

    def __init__(self, repository: DailyBriefRepository):
        self.repo = repository

    # ---------------------------------------------------------------- time

    @staticmethod
    def _utc_now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

    @classmethod
    def _lease_expiry(cls, seconds: int) -> str:
        return (
            datetime.now(timezone.utc) + timedelta(seconds=max(10, int(seconds)))
        ).isoformat(timespec="seconds").replace("+00:00", "Z")

    # ---------------------------------------------------------------- read

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self.repo._connect() as conn:
            row = conn.execute(
                "SELECT * FROM redmine_daily_brief_jobs WHERE job_id=?", (job_id,)
            ).fetchone()
            return dict(row) if row is not None else None

    def has_active_job(self, run_id: str, kind: str | None = None) -> bool:
        """run 是否仍有 queued/running 的 job（already_running 兜底校验）。"""
        sql = (
            "SELECT 1 FROM redmine_daily_brief_jobs WHERE run_id=? "
            "AND status IN ('queued','running')"
        )
        params: list[Any] = [run_id]
        if kind is not None:
            sql += " AND kind=?"
            params.append(kind)
        with self.repo._connect() as conn:
            return conn.execute(sql + " LIMIT 1", params).fetchone() is not None

    def cancel_queued_issue_jobs(self, run_id: str) -> int:
        """作废 run 的全部排队 issue-job（force 重跑前调用）。

        审核意见 P1：force 重跑会删除 issue 行（refreeze），排队的
        issue-job 若不清除，会在 run-job 之后被领取并因 issue 行不存在
        而失败。claim 门控是兜底，这里在重置时源头清除。
        """
        with self.repo._lock, self.repo._connect() as conn:
            cursor = conn.execute(
                "UPDATE redmine_daily_brief_jobs SET status='cancelled',"
                "worker_id='',lease_token='',lease_expires_at='',"
                "finished_at=?,error='superseded by full run rerun' "
                "WHERE run_id=? AND kind='issue' AND status='queued'",
                (self._utc_now(), run_id),
            )
            return cursor.rowcount

    # -------------------------------------------------------------- mutate

    def _active_job(
        self, conn: sqlite3.Connection, run_id: str, kind: str, issue_id: int
    ) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM redmine_daily_brief_jobs WHERE run_id=? AND kind=? "
            "AND issue_id=? AND status IN ('queued','running') "
            "ORDER BY requested_at LIMIT 1",
            (run_id, kind, issue_id),
        ).fetchone()

    def _reset_run_for_enqueue(self, conn: sqlite3.Connection, run_id: str) -> None:
        # 重排队时刷新 started_at：否则历史 completed run 的旧时间戳会让
        # reset_stale_running() 把刚排队的任务当成超时僵尸标成 failed。
        conn.execute(
            "UPDATE redmine_daily_brief_runs SET status='pending', started_at=?, "
            "finished_at='', error='', cancel_requested=0, updated_at=? WHERE run_id=?",
            (_now(), _now(), run_id),
        )

    def insert_job(
        self, conn: sqlite3.Connection, run_id: str, *, kind: str, issue_id: int
    ) -> tuple[dict[str, Any], bool]:
        """在既有事务/连接里插入 job；(run_id, kind, issue_id) 幂等合流。

        合流键与部分唯一索引 idx_brief_jobs_active 一致：不同 issue 的
        reanalyze 绝不能被同 run job 静默吞掉。
        """
        existing = self._active_job(conn, run_id, kind, issue_id)
        if existing is not None:
            return dict(existing), False
        try:
            job_id = new_job_id()
            conn.execute(
                "INSERT INTO redmine_daily_brief_jobs "
                "(job_id,run_id,kind,issue_id,status,requested_at) "
                "VALUES (?,?,?,?, 'queued', ?)",
                (job_id, run_id, kind, issue_id, self._utc_now()),
            )
        except sqlite3.IntegrityError:
            # 跨进程并发：另一进程刚为同一 (run,kind,issue) 插入。
            existing = self._active_job(conn, run_id, kind, issue_id)
            if existing is None:
                raise
            return dict(existing), False
        row = conn.execute(
            "SELECT * FROM redmine_daily_brief_jobs WHERE job_id=?", (job_id,)
        ).fetchone()
        return dict(row), True

    def enqueue_job(
        self, run_id: str, *, kind: str = "run", issue_id: int = 0
    ) -> tuple[dict[str, Any], bool]:
        """持久化一个 Web/CLI 请求；同一目标已有活动 job 时幂等复用。"""
        if kind not in JOB_KINDS:
            raise ValueError(f"unsupported daily brief job kind: {kind}")
        target_issue = int(issue_id or 0)
        if kind == "run":
            target_issue = 0
        with self.repo._lock, self.repo._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            run = conn.execute(
                "SELECT run_id, status FROM redmine_daily_brief_runs WHERE run_id=?",
                (run_id,),
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
            job, created = self.insert_job(conn, run_id, kind=kind, issue_id=target_issue)
            if created:
                self._reset_run_for_enqueue(conn, run_id)
                if kind == "issue":
                    conn.execute(
                        "UPDATE redmine_daily_brief_issues SET status='pending', "
                        "started_at='', finished_at='', error='', error_type='' "
                        "WHERE run_id=? AND issue_id=?",
                        (run_id, target_issue),
                    )
            return job, created

    def claim_next_job(
        self, worker_id: str, lease_seconds: int = 90
    ) -> dict[str, Any] | None:
        """领取最早 job，并把租约过期的未完成任务安全放回队列。"""
        now = self._utc_now()
        with self.repo._lock, self.repo._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            expired = conn.execute(
                "SELECT j.job_id,j.run_id,j.kind,j.issue_id,r.status AS run_status "
                "FROM redmine_daily_brief_jobs j JOIN redmine_daily_brief_runs r "
                "ON r.run_id=j.run_id WHERE j.status='running' "
                "AND j.lease_expires_at<>'' AND j.lease_expires_at<?",
                (now,),
            ).fetchall()
            for item in expired:
                if item["run_status"] in TERMINAL_RUN_STATUSES_SQL:
                    conn.execute(
                        "UPDATE redmine_daily_brief_jobs SET status='completed',worker_id='',"
                        "lease_token='',lease_expires_at='',finished_at=? WHERE job_id=?",
                        (now, item["job_id"]),
                    )
                    continue
                conn.execute(
                    "UPDATE redmine_daily_brief_jobs SET status='queued',worker_id='',"
                    "lease_token='',lease_expires_at='',"
                    "error='worker lease expired; queued for retry' WHERE job_id=?",
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
            # 先清理所有已被 full-run 覆盖的 issue-job，再从全局队列选取。
            # 不能只在当前候选恰好是 issue-job 时清理：requested_at 相同
            # 时 job_id 可能让 run-job 先被领取，遗留的 issue-job 会在
            # full-run 完成后错误执行。
            conn.execute(
                "UPDATE redmine_daily_brief_jobs SET status='cancelled',"
                "worker_id='',lease_token='',lease_expires_at='',"
                "finished_at=?,error='superseded by full run job' "
                "WHERE kind='issue' AND status='queued' AND EXISTS ("
                "SELECT 1 FROM redmine_daily_brief_jobs AS run_job "
                "WHERE run_job.run_id=redmine_daily_brief_jobs.run_id "
                "AND run_job.kind='run' "
                "AND run_job.status IN ('queued','running')"
                ")",
                (now,),
            )
            while True:
                row = conn.execute(
                    "SELECT * FROM redmine_daily_brief_jobs WHERE status='queued' "
                    "ORDER BY requested_at,job_id LIMIT 1"
                ).fetchone()
                if row is None:
                    return None
                # 跨 kind 互斥（审核意见 P1）：同一 run 的活跃 run-job 是
                # 全量执行域（force 重跑会删掉 issue 行），排队的
                # issue-job 此时不允许被领取——否则 reanalyze_issue 读到
                # 被删除的 issue 行 → RuntimeError → 失败收敛把刚重置的
                # run 打成 failed。作废（cancelled）而不是等待：run-job
                # 完成后用户可重新发起单项分析。
                if row["kind"] == "issue":
                    active_run_job = conn.execute(
                        "SELECT 1 FROM redmine_daily_brief_jobs "
                        "WHERE run_id=? AND kind='run' "
                        "AND status IN ('queued','running') LIMIT 1",
                        (row["run_id"],),
                    ).fetchone()
                    if active_run_job is not None:
                        conn.execute(
                            "UPDATE redmine_daily_brief_jobs SET status='cancelled',"
                            "worker_id='',lease_token='',lease_expires_at='',"
                            "finished_at=?,error='superseded by full run job' "
                            "WHERE job_id=?",
                            (now, row["job_id"]),
                        )
                        continue
                lease_token = uuid.uuid4().hex
                cursor = conn.execute(
                    "UPDATE redmine_daily_brief_jobs SET status='running',worker_id=?,"
                    "lease_token=?,lease_expires_at=?,started_at=?,attempt_count=attempt_count+1,"
                    "error='' WHERE job_id=? AND status='queued'",
                    (worker_id, lease_token, self._lease_expiry(lease_seconds), now, row["job_id"]),
                )
                if cursor.rowcount != 1:
                    return None
                return dict(conn.execute(
                    "SELECT * FROM redmine_daily_brief_jobs WHERE job_id=?", (row["job_id"],)
                ).fetchone())

    def renew_job(
        self, job_id: str, worker_id: str, lease_token: str, lease_seconds: int = 90
    ) -> bool:
        with self.repo._lock, self.repo._connect() as conn:
            cursor = conn.execute(
                "UPDATE redmine_daily_brief_jobs SET lease_expires_at=? "
                "WHERE job_id=? AND worker_id=? AND lease_token=? AND status='running'",
                (self._lease_expiry(lease_seconds), job_id, worker_id, lease_token),
            )
            return cursor.rowcount == 1

    def finish_job(
        self, job_id: str, worker_id: str, lease_token: str, *, error: str = ""
    ) -> bool:
        with self.repo._lock, self.repo._connect() as conn:
            cursor = conn.execute(
                "UPDATE redmine_daily_brief_jobs SET status=?,worker_id='',"
                "lease_token='',lease_expires_at='',finished_at=?,error=? "
                "WHERE job_id=? AND worker_id=? AND lease_token=? AND status='running'",
                ("failed" if error else "completed", self._utc_now(), error[:1000],
                 job_id, worker_id, lease_token),
            )
            return cursor.rowcount == 1

    def requeue_job(
        self, job_id: str, worker_id: str, lease_token: str, *, reason: str
    ) -> bool:
        with self.repo._lock, self.repo._connect() as conn:
            cursor = conn.execute(
                "UPDATE redmine_daily_brief_jobs SET status='queued',worker_id='',"
                "lease_token='',lease_expires_at='',error=? WHERE job_id=? AND worker_id=? "
                "AND lease_token=? AND status='running'",
                (reason[:1000], job_id, worker_id, lease_token),
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

    # ------------------------------------------------------- orphan repair

    def reconcile_orphan_runs(self, started_after_iso: str) -> int:
        """给「active run 但无任何活动 job」的孤儿补一个 queued run job。

        典型成因：旧版本两步创建（start_run 与 enqueue_job 非同一事务）
        在进程崩溃时留下的孤儿。started_after_iso 之前的更早孤儿由
        reset_stale_running 统一标 failed，不在此复活。
        """
        with self.repo._lock, self.repo._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            orphans = conn.execute(
                "SELECT r.run_id FROM redmine_daily_brief_runs r "
                "WHERE r.status IN ('pending','snapshotting','analyzing') "
                "AND r.started_at >= ? AND NOT EXISTS ("
                "  SELECT 1 FROM redmine_daily_brief_jobs j "
                "  WHERE j.run_id = r.run_id "
                "  AND j.status IN ('queued','running'))",
                (started_after_iso,),
            ).fetchall()
            for item in orphans:
                self.insert_job(conn, str(item["run_id"]), kind="run", issue_id=0)
            return len(orphans)


# claim_next_job 的过期恢复需要终态集合；从 repository 常量会形成循环
# import（repository 又要 import JobStore），这里以同一字面量保持一致，
# 由架构测试约束两处不得漂移。
TERMINAL_RUN_STATUSES_SQL = frozenset({"completed", "partial", "failed", "cancelled"})


__all__ = [
    "ACTIVE_JOB_STATUSES",
    "ACTIVE_RUN_STATUSES",
    "JOB_KINDS",
    "DailyBriefJobStore",
    "new_job_id",
]
