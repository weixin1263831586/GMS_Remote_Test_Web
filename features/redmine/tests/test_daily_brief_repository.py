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
        self.assertTrue(self.repo.renew_job(first["job_id"], "worker-a", 30))
        self.assertTrue(self.repo.finish_job(first["job_id"], "worker-a"))
        self.assertEqual(self.repo.get_job(first["job_id"])["status"], "completed")

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
        self.repo.claim_next_job("worker-a", lease_seconds=30)
        self.assertTrue(self.repo.requeue_job(
            job["job_id"], "worker-a", reason="worker stopping"
        ))
        self.assertEqual(self.repo.get_job(job["job_id"])["status"], "queued")
        self.assertEqual(self.repo.get_run(run.run_id).status, "pending")


if __name__ == "__main__":
    unittest.main()
