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


if __name__ == "__main__":
    unittest.main()
