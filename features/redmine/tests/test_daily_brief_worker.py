"""Durable Daily Brief worker tests (no real kkagent or Redmine)."""

from __future__ import annotations

import asyncio
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from features.redmine.daily_brief_models import DailyBriefIssue, DailyBriefRun
from features.redmine.daily_brief_repository import DailyBriefRepository
from features.redmine.daily_brief_service import DailyBriefService
from features.redmine.daily_brief_worker import run_claimed_job
from features.redmine.kkagent_analyzer import KkAgentAnalysisResult


VALID = {
    "problem_summary": "summary",
    "customer_request": "request",
    "recommended_actions": [],
    "suggested_solution": "solution",
    "evidence": [],
    "confidence": 0.9,
    "root_cause_type": "likely",
}
SNAPSHOT = {
    "brief_date": "2026-09-13",
    "generated_at": "2026-09-13T00:00:03",
    "snapshot_hash": "hash-1",
    "counts": {"waiting_my_reply": 1, "no_reply_3_days": 0, "total": 1},
    "issues": [{
        "issue_id": 101,
        "subject": "A",
        "buckets": ["waiting_my_reply"],
        "priority": "P1",
        "priority_score": 90,
        "fingerprint": "fp-a",
    }],
}


def make_service(repository: DailyBriefRepository) -> DailyBriefService:
    service = DailyBriefService.__new__(DailyBriefService)
    service.owner_id = "u1"
    service.config_manager = None
    service.repository = repository
    return service


class DailyBriefWorkerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repository = DailyBriefRepository(Path(self._tmp.name))
        self.service = make_service(self.repository)
        self.worker_id = "test-worker"

    def _claim(self, *, kind: str = "run", issue_id: int = 0):
        job, _ = self.repository.enqueue_job(
            "db_test", kind=kind, issue_id=issue_id
        )
        claimed = self.repository.claim_next_job(self.worker_id, lease_seconds=30)
        self.assertEqual(claimed["job_id"], job["job_id"])
        return claimed

    def test_claimed_run_executes_and_completes_job(self):
        self.repository.create_run(DailyBriefRun(
            owner_id="u1", brief_date="2026-09-13", mode="manual", run_id="db_test"
        ))
        job = self._claim()

        async def analyze(_entry):
            return KkAgentAnalysisResult(ok=True, result=dict(VALID))

        async def scenario():
            with patch.object(
                self.service, "build_triage", AsyncMock(return_value=dict(SNAPSHOT))
            ), patch.object(self.service, "_build_analyzer") as build:
                build.return_value.analyze = analyze
                return await run_claimed_job(
                    self.repository,
                    job,
                    self.worker_id,
                    asyncio.Event(),
                    lease_seconds=30,
                    service_factory=lambda _owner: self.service,
                )

        self.assertTrue(asyncio.run(scenario()))
        self.assertEqual(self.repository.get_job(job["job_id"])["status"], "completed")
        self.assertEqual(self.repository.get_run("db_test").status, "completed")

    def test_worker_shutdown_requeues_active_job(self):
        self.repository.create_run(DailyBriefRun(
            owner_id="u1", brief_date="2026-09-13", mode="manual", run_id="db_test"
        ))
        job = self._claim()
        entered = asyncio.Event()

        async def execute_run(_run_id):
            entered.set()
            await asyncio.Event().wait()

        fake_service = SimpleNamespace(execute_run=execute_run)

        async def scenario():
            stop = asyncio.Event()
            task = asyncio.create_task(run_claimed_job(
                self.repository,
                job,
                self.worker_id,
                stop,
                lease_seconds=30,
                service_factory=lambda _owner: fake_service,
            ))
            await entered.wait()
            stop.set()
            return await task

        self.assertFalse(asyncio.run(scenario()))
        self.assertEqual(self.repository.get_job(job["job_id"])["status"], "queued")
        self.assertEqual(self.repository.get_run("db_test").status, "pending")

    def test_issue_job_uses_single_issue_reanalysis_path(self):
        run = DailyBriefRun(
            owner_id="u1", brief_date="2026-09-13", mode="manual",
            run_id="db_test", status="completed"
        )
        self.repository.create_run(run)
        self.repository.upsert_issue(DailyBriefIssue(
            run_id=run.run_id, issue_id=101, buckets=[], status="completed"
        ))
        job = self._claim(kind="issue", issue_id=101)

        async def analyze(_entry):
            return KkAgentAnalysisResult(ok=True, result=dict(VALID))

        async def scenario():
            with patch.object(self.service, "_build_analyzer") as build:
                build.return_value.analyze = analyze
                return await run_claimed_job(
                    self.repository,
                    job,
                    self.worker_id,
                    asyncio.Event(),
                    lease_seconds=30,
                    service_factory=lambda _owner: self.service,
                )

        self.assertTrue(asyncio.run(scenario()))
        self.assertEqual(self.repository.get_issue("db_test", 101).status, "completed")
        self.assertEqual(self.repository.get_job(job["job_id"])["status"], "completed")


if __name__ == "__main__":
    unittest.main()
