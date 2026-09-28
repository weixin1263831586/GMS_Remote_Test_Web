"""Daily Brief job claim concurrency tests."""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from features.redmine.daily_brief_models import DailyBriefIssue, DailyBriefRun
from features.redmine.daily_brief_repository import DailyBriefRepository, new_run_id


class DailyBriefJobClaimTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = DailyBriefRepository(Path(self._tmp.name))

    def _create_run(self, date: str) -> DailyBriefRun:
        run = DailyBriefRun(
            owner_id="u1",
            brief_date=date,
            mode="nightly",
            run_id=new_run_id(),
            status="completed",
        )
        self.repo.create_run(run)
        return run

    def _enqueue_issue(self, run: DailyBriefRun, issue_id: int) -> None:
        self.repo.upsert_issue(DailyBriefIssue(
            run_id=run.run_id,
            issue_id=issue_id,
            buckets=[],
            status="completed",
        ))
        self.repo.enqueue_job(run.run_id, kind="issue", issue_id=issue_id)

    def test_workers_serialize_jobs_per_run_without_blocking_other_runs(self):
        first_run = self._create_run("2026-09-13")
        second_run = self._create_run("2026-09-14")
        self._enqueue_issue(first_run, 100)
        self._enqueue_issue(first_run, 200)
        self._enqueue_issue(second_run, 300)

        first = self.repo.claim_next_job("worker-a", lease_seconds=30)
        second = self.repo.claim_next_job("worker-b", lease_seconds=30)

        self.assertEqual(
            {first["run_id"], second["run_id"]},
            {first_run.run_id, second_run.run_id},
        )
        self.assertIsNone(self.repo.claim_next_job("worker-c", lease_seconds=30))
