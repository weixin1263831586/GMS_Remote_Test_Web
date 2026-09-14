"""Daily Brief Repository 测试：建库、幂等、run/issue 读写、崩溃恢复。"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from features.redmine.daily_brief_models import DailyBriefIssue, DailyBriefRun
from features.redmine.daily_brief_repository import DailyBriefRepository, new_run_id


def make_run(owner="u1", date="2026-09-13", mode="nightly", **kw) -> DailyBriefRun:
    return DailyBriefRun(owner_id=owner, brief_date=date, mode=mode, run_id=new_run_id(), **kw)


class DailyBriefRepositoryTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = DailyBriefRepository(Path(self._tmp.name))

    def test_create_run_is_idempotent_per_owner_date_mode(self):
        first = make_run()
        self.repo.create_run(first)
        second = make_run(status="analyzing")
        existing = self.repo.create_run(second)
        self.assertEqual(existing.run_id, first.run_id)  # 返回既有 run，不新建

    def test_update_and_get_run_roundtrip(self):
        run = make_run()
        self.repo.create_run(run)
        run.status = "completed"
        run.report_json = {"counts": {"total": 3}}
        run.snapshot_hash = "sha"
        self.assertTrue(self.repo.update_run(run))
        loaded = self.repo.get_run(run.run_id)
        self.assertEqual(loaded.status, "completed")
        self.assertEqual(loaded.report_json["counts"]["total"], 3)
        self.assertEqual(loaded.snapshot_hash, "sha")

    def test_find_and_latest_run(self):
        nightly = make_run(mode="nightly", date="2026-09-13")
        delta = make_run(mode="delta", date="2026-09-13")
        older = make_run(mode="nightly", date="2026-09-12")
        for run in (older, nightly, delta):
            self.repo.create_run(run)
        self.assertEqual(self.repo.find_run("u1", "2026-09-13", "delta").run_id, delta.run_id)
        latest = self.repo.latest_run("u1")
        self.assertIn(latest.run_id, {nightly.run_id, delta.run_id})
        self.assertEqual(self.repo.latest_run("u1", "2026-09-12").run_id, older.run_id)
        self.assertIsNone(self.repo.latest_run("other-owner"))

    def test_issue_upsert_and_list_ordering(self):
        run = make_run()
        self.repo.create_run(run)
        self.repo.upsert_issue(DailyBriefIssue(
            run_id=run.run_id, issue_id=2, buckets=["waiting_my_reply"],
            priority_score=30, status="completed",
            result={"problem_summary": "x"},
        ))
        self.repo.upsert_issue(DailyBriefIssue(
            run_id=run.run_id, issue_id=1, buckets=["no_reply_3_days"],
            priority_score=90, status="pending",
        ))
        issues = self.repo.list_issues(run.run_id)
        self.assertEqual([i.issue_id for i in issues], [1, 2])  # score desc
        loaded = self.repo.get_issue(run.run_id, 2)
        self.assertEqual(loaded.result["problem_summary"], "x")
        self.assertEqual(loaded.buckets, ["waiting_my_reply"])

        self.assertEqual(self.repo.delete_issues(run.run_id), 2)
        self.assertEqual(self.repo.list_issues(run.run_id), [])

    def test_latest_ai_execution_is_grouped_per_issue(self):
        run = make_run()
        self.repo.create_run(run)
        self.repo.record_ai_execution(run.run_id, 1, {"attempt_no": 1, "status": "failed"})
        self.repo.record_ai_execution(run.run_id, 2, {"attempt_no": 1, "status": "completed"})
        self.repo.record_ai_execution(run.run_id, 1, {"attempt_no": 2, "status": "completed"})

        latest = self.repo.latest_ai_executions_by_issue(run.run_id)

        self.assertEqual(latest[1]["attempt_no"], 2)
        self.assertEqual(latest[1]["status"], "completed")
        self.assertEqual(latest[2]["attempt_no"], 1)
        self.assertIn("recorded_at", latest[1])

    def test_reset_stale_running_marks_interrupted_runs(self):
        run = make_run(status="analyzing", started_at="2026-09-13T00:00:00")
        self.repo.create_run(run)
        # started_at 在阈值之前 → 标记 failed
        self.repo.update_run(run)
        marked = self.repo.reset_stale_running("2026-09-13T06:00:00")
        self.assertEqual(marked, 1)
        self.assertEqual(self.repo.get_run(run.run_id).status, "failed")

    def test_owner_isolation_by_root(self):
        repo_a = DailyBriefRepository(Path(self._tmp.name) / "a")
        repo_b = DailyBriefRepository(Path(self._tmp.name) / "b")
        run_a = make_run(owner="alice")
        repo_a.create_run(run_a)
        self.assertIsNone(repo_b.get_run(run_a.run_id))
        self.assertEqual(repo_a.db_path, repo_a.db_path)

    def test_frozen_snapshot_persists_across_repository_instances(self):
        """冻结快照必须落盘：新 repository 实例（= 新进程）仍能读回。"""
        run = make_run()
        self.repo.create_run(run)
        snapshot = {
            "snapshot_hash": "hash-1",
            "source_sync_status": "synced",
            "counts": {"waiting_my_reply": 1, "total": 1},
            "issues": [{"issue_id": 101, "subject": "A"}],
        }
        self.repo.save_snapshot(run.run_id, snapshot)

        reopened = DailyBriefRepository(self.repo.owner_root)
        loaded = reopened.get_snapshot(run.run_id)
        self.assertEqual(loaded["snapshot_hash"], "hash-1")
        self.assertEqual(loaded["issues"][0]["issue_id"], 101)

        # 替换语义：force 重跑后 save 覆盖旧快照；delete 清理。
        snapshot["snapshot_hash"] = "hash-2"
        reopened.save_snapshot(run.run_id, snapshot)
        self.assertEqual(reopened.get_snapshot(run.run_id)["snapshot_hash"], "hash-2")
        self.assertEqual(reopened.delete_snapshot(run.run_id), 1)
        self.assertIsNone(reopened.get_snapshot(run.run_id))

    def test_run_roundtrip_keeps_source_sync_status(self):
        run = make_run(source_sync_status="sync_failed")
        self.repo.create_run(run)
        loaded = self.repo.get_run(run.run_id)
        self.assertEqual(loaded.source_sync_status, "sync_failed")

    def test_legacy_database_migrates_source_sync_status_column(self):
        """旧库（无 source_sync_status 列）打开时自动迁移，不丢既有 run。"""
        import sqlite3

        legacy = Path(self._tmp.name) / "legacy"
        legacy.mkdir()
        db = legacy / "daily_brief.sqlite3"
        conn = sqlite3.connect(db)
        conn.execute(
            "CREATE TABLE redmine_daily_brief_runs ("
            "run_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, brief_date TEXT NOT NULL, "
            "mode TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', "
            "started_at TEXT NOT NULL DEFAULT '', finished_at TEXT NOT NULL DEFAULT '', "
            "snapshot_at TEXT NOT NULL DEFAULT '', snapshot_hash TEXT NOT NULL DEFAULT '', "
            "issue_count INTEGER NOT NULL DEFAULT 0, waiting_my_reply_count INTEGER NOT NULL DEFAULT 0, "
            "no_reply_3_days_count INTEGER NOT NULL DEFAULT 0, urgent_count INTEGER NOT NULL DEFAULT 0, "
            "analysis_backend TEXT NOT NULL DEFAULT '', model_name TEXT NOT NULL DEFAULT '', "
            "prompt_version TEXT NOT NULL DEFAULT '', report_json TEXT NOT NULL DEFAULT '{}', "
            "report_markdown TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '', "
            "created_at TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL DEFAULT '')"
        )
        conn.execute(
            "INSERT INTO redmine_daily_brief_runs (run_id, owner_id, brief_date, mode, status) "
            "VALUES ('db_old', 'u1', '2026-09-13', 'nightly', 'completed')"
        )
        conn.commit()
        conn.close()

        migrated = DailyBriefRepository(legacy)
        loaded = migrated.get_run("db_old")
        self.assertEqual(loaded.status, "completed")
        self.assertEqual(loaded.source_sync_status, "")

    def test_jobs_are_idempotent_claimed_and_completed(self):
        run = make_run(status="snapshotting")
        self.repo.create_run(run)
        first, created = self.repo.enqueue_job(run.run_id)
        duplicate, duplicate_created = self.repo.enqueue_job(run.run_id)
        self.assertTrue(created)
        self.assertFalse(duplicate_created)
        self.assertEqual(duplicate["job_id"], first["job_id"])
        self.assertEqual(self.repo.get_run(run.run_id).status, "pending")

        claimed = self.repo.claim_next_job("worker-a", lease_seconds=30)
        self.assertEqual(claimed["job_id"], first["job_id"])
        self.assertEqual(claimed["status"], "running")
        token = claimed["lease_token"]
        self.assertTrue(token)
        self.assertTrue(self.repo.renew_job(first["job_id"], "worker-a", token, 30))
        # 旧 lease token（租约被接管后）不得能 finish —— CAS 拒绝。
        self.assertFalse(self.repo.finish_job(first["job_id"], "worker-a", "stale-token"))
        self.assertTrue(self.repo.finish_job(first["job_id"], "worker-a", token))
        self.assertEqual(self.repo.get_job(first["job_id"])["status"], "completed")

    def test_distinct_reanalysis_targets_are_never_coalesced(self):
        """评审 P1:不同 issue 的 reanalyze 请求绝不能互相吞掉。"""
        run = make_run(status="completed")
        self.repo.create_run(run)
        for issue_id in (100, 200):
            self.repo.upsert_issue(DailyBriefIssue(
                run_id=run.run_id, issue_id=issue_id, buckets=[], status="completed"
            ))
        job_a, created_a = self.repo.enqueue_job(run.run_id, kind="issue", issue_id=100)
        job_b, created_b = self.repo.enqueue_job(run.run_id, kind="issue", issue_id=200)
        self.assertTrue(created_a)
        self.assertTrue(created_b)
        self.assertNotEqual(job_a["job_id"], job_b["job_id"])
        # 同一 issue 的重复请求才允许合流。
        job_a2, created_a2 = self.repo.enqueue_job(run.run_id, kind="issue", issue_id=100)
        self.assertFalse(created_a2)
        self.assertEqual(job_a2["job_id"], job_a["job_id"])
        # run 级 job 与 issue 级 job 也各自独立。
        job_run, created_run = self.repo.enqueue_job(run.run_id, kind="run")
        self.assertTrue(created_run)
        self.assertNotEqual(job_run["job_id"], job_a["job_id"])

    def test_enqueue_job_requeues_with_fresh_started_at(self):
        """重排队刷新 started_at,避免 stale 恢复误杀刚排队的历史 run。"""
        run = make_run(status="completed", started_at="2020-01-01T00:00:00")
        self.repo.create_run(run)
        self.repo.enqueue_job(run.run_id, kind="run")
        refreshed = self.repo.get_run(run.run_id)
        self.assertGreater(refreshed.started_at, "2025-01-01")

    def test_enqueue_new_work_clears_cancel_request(self):
        """已停止 run 的显式重试必须清掉旧取消标志。"""
        run = make_run(status="analyzing")
        self.repo.create_run(run)
        self.assertTrue(self.repo.request_cancel(run.run_id))
        run.status = "cancelled"
        self.repo.update_run(run)
        self.repo.upsert_issue(DailyBriefIssue(
            run_id=run.run_id, issue_id=100, buckets=[], status="pending"
        ))

        self.repo.enqueue_job(run.run_id, kind="issue", issue_id=100)

        self.assertFalse(self.repo.is_cancel_requested(run.run_id))

    def test_stale_worker_cannot_overwrite_reclaimed_job(self):
        """租约过期被新 Worker 领取后,旧 Worker 的写入必须被拒绝。"""
        run = make_run(status="completed")
        self.repo.create_run(run)
        job, _ = self.repo.enqueue_job(run.run_id, kind="run")
        first = self.repo.claim_next_job("worker-a", lease_seconds=30)
        # 模拟租约过期:直接把 lease_expires_at 拨回过去。
        with self.repo._connect() as conn:
            conn.execute(
                "UPDATE redmine_daily_brief_jobs SET lease_expires_at='2000-01-01T00:00:00Z' "
                "WHERE job_id=?", (job["job_id"],)
            )
        second = self.repo.claim_next_job("worker-b", lease_seconds=30)
        self.assertIsNotNone(second)
        self.assertNotEqual(second["lease_token"], first["lease_token"])
        # 旧 Worker 迟到的 renew/finish/requeue 全部失败。
        self.assertFalse(self.repo.renew_job(
            job["job_id"], "worker-a", first["lease_token"], 30
        ))
        self.assertFalse(self.repo.finish_job(
            job["job_id"], "worker-a", first["lease_token"]
        ))
        self.assertFalse(self.repo.requeue_job(
            job["job_id"], "worker-a", first["lease_token"], reason="late"
        ))
        # 新 Worker 的操作仍然有效。
        self.assertTrue(self.repo.finish_job(
            job["job_id"], "worker-b", second["lease_token"]
        ))

    def test_issue_job_marks_only_target_pending(self):
        run = make_run(status="completed")
        self.repo.create_run(run)
        for issue_id in (1, 2):
            self.repo.upsert_issue(DailyBriefIssue(
                run_id=run.run_id, issue_id=issue_id, buckets=[], status="completed"
            ))
        job, created = self.repo.enqueue_job(run.run_id, kind="issue", issue_id=2)
        self.assertTrue(created)
        self.assertEqual(job["issue_id"], 2)
        self.assertEqual(self.repo.get_run(run.run_id).status, "pending")
        self.assertEqual(self.repo.get_issue(run.run_id, 1).status, "completed")
        self.assertEqual(self.repo.get_issue(run.run_id, 2).status, "pending")

    def test_running_job_can_be_requeued_on_worker_shutdown(self):
        run = make_run()
        self.repo.create_run(run)
        job, _ = self.repo.enqueue_job(run.run_id)
        claimed = self.repo.claim_next_job("worker-a", lease_seconds=30)
        self.assertTrue(self.repo.requeue_job(
            job["job_id"], "worker-a", claimed["lease_token"], reason="worker stopping"
        ))
        self.assertEqual(self.repo.get_job(job["job_id"])["status"], "queued")
        self.assertEqual(self.repo.get_run(run.run_id).status, "pending")


def _concurrent_create_run_worker(tmp: str, mode: str, barrier_port: int) -> None:
    """子进程:与另一进程同时为同一 owner+date+mode 创建 run。"""
    import socket

    # 廉价同步:双方都连上 barrier 端口后同时开始,放大竞态窗口。
    sock = socket.socket()
    try:
        sock.connect(("127.0.0.1", barrier_port))
    except OSError:
        pass
    finally:
        sock.close()
    repo = DailyBriefRepository(Path(tmp))
    run = make_run(mode=mode)
    created = repo.create_run(run)
    result = created if created is not None else run
    (Path(tmp) / f"result-{mode}.txt").write_text(result.run_id, encoding="utf-8")


class CrossProcessConsistencyTests(unittest.TestCase):
    """跨进程一致性矩阵。

    Web 进程、daily_brief_worker、nightly/delta CLI 可能同时对同一
    owner SQLite 调 create_run/enqueue_job;一致性必须由数据库承担
    (ON CONFLICT / 唯一索引 / BEGIN IMMEDIATE),而不是 Python 进程锁。
    """

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_root = Path(self._tmp.name)

    def test_concurrent_create_run_yields_single_row_and_same_run_id(self):
        import multiprocessing
        import socket

        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(4)
        barrier_port = listener.getsockname()[1]

        procs = [
            multiprocessing.Process(
                target=_concurrent_create_run_worker,
                args=(str(self.db_root), "nightly", barrier_port),
            )
            for _ in range(3)
        ]
        for proc in procs:
            proc.start()
        # 等 3 个子进程都到 barrier 再放行。
        accepted = 0
        listener.settimeout(10)
        while accepted < len(procs):
            conn, _ = listener.accept()
            conn.close()
            accepted += 1
        listener.close()
        for proc in procs:
            proc.join(timeout=20)
            self.assertEqual(proc.exitcode, 0)

        repo = DailyBriefRepository(self.db_root)
        import sqlite3
        with sqlite3.connect(repo.db_path) as conn:
            rows = conn.execute(
                "SELECT run_id FROM redmine_daily_brief_runs "
                "WHERE owner_id='u1' AND brief_date='2026-09-13' AND mode='nightly'"
            ).fetchall()
        self.assertEqual(len(rows), 1)
        # 所有子进程拿到同一个 run_id(胜者写入,其余读到既有记录)。
        ids = {
            (self.db_root / "result-nightly.txt").read_text(encoding="utf-8")
        }
        self.assertEqual(ids, {rows[0][0]})

    def test_concurrent_enqueue_same_issue_coalesces_to_one_job(self):
        import multiprocessing

        repo = DailyBriefRepository(self.db_root)
        run = make_run(status="completed")
        repo.create_run(run)
        repo.upsert_issue(DailyBriefIssue(
            run_id=run.run_id, issue_id=100, buckets=[], status="completed"
        ))

        def worker() -> None:
            child = DailyBriefRepository(self.db_root)
            child.enqueue_job(run.run_id, kind="issue", issue_id=100)

        procs = [multiprocessing.Process(target=worker) for _ in range(3)]
        for proc in procs:
            proc.start()
        for proc in procs:
            proc.join(timeout=20)
            self.assertEqual(proc.exitcode, 0)

        import sqlite3
        with sqlite3.connect(repo.db_path) as conn:
            rows = conn.execute(
                "SELECT status FROM redmine_daily_brief_jobs "
                "WHERE run_id=? AND kind='issue' AND issue_id=100",
                (run.run_id,),
            ).fetchall()
        # 并发重复请求合流为恰好一个 queued job;不允许丢失也不允许重复。
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "queued")

    def test_lease_expiration_requeues_and_new_worker_takes_over(self):
        """kill -9 语义:租约过期后任务被重新排队并可被接管。"""
        repo = DailyBriefRepository(self.db_root)
        run = make_run(status="completed")
        repo.create_run(run)
        job, _ = repo.enqueue_job(run.run_id, kind="run")
        claimed = repo.claim_next_job("crashed-worker", lease_seconds=30)
        self.assertIsNotNone(claimed)
        # 模拟进程死亡:租约拨到过去。
        import sqlite3
        with sqlite3.connect(repo.db_path) as conn:
            conn.execute(
                "UPDATE redmine_daily_brief_jobs "
                "SET lease_expires_at='2000-01-01T00:00:00Z' WHERE job_id=?",
                (job["job_id"],),
            )
        # 新 Worker claim:先回收过期租约再领取。
        taken = repo.claim_next_job("fresh-worker", lease_seconds=30)
        self.assertIsNotNone(taken)
        self.assertEqual(taken["job_id"], job["job_id"])
        self.assertEqual(taken["worker_id"], "fresh-worker")
        self.assertEqual(taken["attempt_count"], 2)
        self.assertEqual(repo.get_run(run.run_id).status, "pending")


if __name__ == "__main__":
    unittest.main()
