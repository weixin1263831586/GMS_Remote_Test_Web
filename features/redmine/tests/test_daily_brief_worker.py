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


def patch_preflight_ok():
    return patch(
        "features.redmine.daily_brief_service.preflight_gms_auth",
        AsyncMock(return_value=(True, "")),
    )


VALID = {
    "problem_summary": "summary",
    "customer_request": "request",
    "recommended_actions": [],
    "suggested_solution": "solution",
    "detailed_report": "## 一、问题概况\n\n| 项目 | 内容 |\n|---|---|\n| Issue | #101 |",
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

    def _patch_preflight(self):
        preflight = patch_preflight_ok()
        preflight.start()
        self.addCleanup(preflight.stop)

    def test_claimed_run_executes_and_completes_job(self):
        self._patch_preflight()
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
        self._patch_preflight()
        run = DailyBriefRun(
            owner_id="u1", brief_date="2026-09-13", mode="manual",
            run_id="db_test", status="completed"
        )
        self.repository.create_run(run)
        self.repository.upsert_issue(DailyBriefIssue(
            run_id=run.run_id, issue_id=101, buckets=[], status="completed"
        ))
        newer = DailyBriefRun(
            owner_id="u1", brief_date=run.brief_date, mode="delta",
            run_id="db_newer", status="completed", started_at="2099-01-01T00:00:00",
        )
        self.repository.create_run(newer)
        self.repository.upsert_issue(DailyBriefIssue(
            run_id=newer.run_id, issue_id=101, buckets=[], status="completed",
            result={"problem_summary": "keep newer run untouched"},
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
        self.assertEqual(self.repository.get_issue("db_newer", 101).result,
                         {"problem_summary": "keep newer run untouched"})
        self.assertEqual(self.repository.get_job(job["job_id"])["status"], "completed")

    def test_issue_job_persisted_cancel_stops_active_analysis(self):
        self._patch_preflight()
        run = DailyBriefRun(
            owner_id="u1", brief_date="2026-09-13", mode="manual",
            run_id="db_test", status="completed",
        )
        self.repository.create_run(run)
        self.repository.upsert_issue(DailyBriefIssue(
            run_id=run.run_id, issue_id=101, buckets=[], status="completed"
        ))
        job = self._claim(kind="issue", issue_id=101)
        entered = asyncio.Event()
        analyzer_cancelled = asyncio.Event()

        async def analyze(_entry):
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                analyzer_cancelled.set()
                raise

        async def scenario():
            with patch.object(self.service, "_build_analyzer") as build:
                build.return_value.analyze = analyze
                task = asyncio.create_task(run_claimed_job(
                    self.repository,
                    job,
                    self.worker_id,
                    asyncio.Event(),
                    lease_seconds=30,
                    service_factory=lambda _owner: self.service,
                ))
                await asyncio.wait_for(entered.wait(), timeout=1)
                self.assertTrue(self.repository.request_cancel(run.run_id))
                return await asyncio.wait_for(task, timeout=2)

        self.assertTrue(asyncio.run(scenario()))
        self.assertTrue(analyzer_cancelled.is_set())
        self.assertEqual(self.repository.get_run(run.run_id).status, "cancelled")
        self.assertEqual(self.repository.get_issue(run.run_id, 101).status, "pending")
        self.assertEqual(self.repository.get_job(job["job_id"])["status"], "completed")

    def test_stale_worker_failure_does_not_overwrite_new_lease_run_state(self):
        """租约被接管后，旧 Worker 的失败不得覆盖新 Worker 的 run。"""
        self.repository.create_run(DailyBriefRun(
            owner_id="u1", brief_date="2026-09-13", mode="manual", run_id="db_test"
        ))
        job = self._claim()

        async def execute_run(_run_id):
            self.assertTrue(self.repository.requeue_job(
                job["job_id"], self.worker_id, job["lease_token"],
                reason="simulated lease handoff",
            ))
            replacement = self.repository.claim_next_job(
                "replacement-worker", lease_seconds=30
            )
            self.assertIsNotNone(replacement)
            run = self.repository.get_run("db_test")
            run.status = "analyzing"
            run.error = ""
            self.repository.update_run(run)
            raise RuntimeError("stale worker failed after handoff")

        fake_service = SimpleNamespace(execute_run=execute_run)
        self.assertTrue(asyncio.run(run_claimed_job(
            self.repository,
            job,
            self.worker_id,
            asyncio.Event(),
            lease_seconds=30,
            service_factory=lambda _owner: fake_service,
        )))

        current_job = self.repository.get_job(job["job_id"])
        self.assertEqual(current_job["status"], "running")
        self.assertEqual(current_job["worker_id"], "replacement-worker")
        self.assertEqual(self.repository.get_run("db_test").status, "analyzing")


class OwnerFairnessTests(unittest.TestCase):
    """owner round-robin 游标:字母序靠前的 owner 不得垄断 claim 起点。"""

    def test_cursor_rotates_start_offset_each_round(self):
        from features.redmine.daily_brief_worker import OwnerFairnessCursor

        owners = ["a", "b", "c"]
        cursor = OwnerFairnessCursor()
        first = cursor.rotate(owners)
        second = cursor.rotate(owners)
        third = cursor.rotate(owners)
        fourth = cursor.rotate(owners)
        self.assertEqual(first[0], "a")
        self.assertEqual(second[0], "b")
        self.assertEqual(third[0], "c")
        self.assertEqual(fourth[0], "a")  # 循环
        # 轮转不丢成员。
        self.assertEqual(sorted(first), sorted(owners))

    def test_cursor_handles_empty_and_single(self):
        from features.redmine.daily_brief_worker import OwnerFairnessCursor

        cursor = OwnerFairnessCursor()
        self.assertEqual(cursor.rotate([]), [])
        self.assertEqual(cursor.rotate(["only"]), ["only"])
        self.assertEqual(cursor.rotate(["only"]), ["only"])


class UnexpectedExitConvergenceTests(unittest.TestCase):
    """run_claimed_job 异常逃逸时,租约归属仍必须收敛。"""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repository = DailyBriefRepository(Path(self._tmp.name))
        self.worker_id = "conv-worker"

    def test_unexpected_exit_requeues_job_not_stuck_running(self):
        from features.redmine.daily_brief_worker import run_claimed_job

        self.repository.create_run(DailyBriefRun(
            owner_id="u1", brief_date="2026-09-13", mode="manual", run_id="db_conv"
        ))
        job, _ = self.repository.enqueue_job("db_conv", kind="run")
        claimed = self.repository.claim_next_job(self.worker_id, lease_seconds=30)
        self.assertEqual(claimed["job_id"], job["job_id"])

        def factory(_owner):
            async def execute_run(_run_id):
                await asyncio.Event().wait()
            return SimpleNamespace(execute_run=execute_run)

        # 为触发 finally 兜底,让 asyncio.wait 自身抛错。
        async def scenario_unexpected():
            with patch(
                "features.redmine.daily_brief_worker.asyncio.wait",
                side_effect=RuntimeError("event loop exploded"),
            ):
                return await run_claimed_job(
                    self.repository,
                    claimed,
                    self.worker_id,
                    asyncio.Event(),
                    lease_seconds=30,
                    service_factory=factory,
                )

        with self.assertRaises(RuntimeError):
            asyncio.run(scenario_unexpected())
        # finally 兜底必须 requeue(而不是 finish_job 标终态 failed):
        # job 回到 queued 供 claim_next_job 重试,run 回到 pending。
        self.assertEqual(
            self.repository.get_job(job["job_id"])["status"], "queued"
        )
        run = self.repository.get_run("db_conv")
        self.assertEqual(run.status, "pending")
        self.assertIn("unexpected exit", run.error)

        # requeue 后能被再次认领,验证重试路径真正可达。
        reclaimed = self.repository.claim_next_job("conv-worker-2", lease_seconds=30)
        self.assertIsNotNone(reclaimed)
        self.assertEqual(reclaimed["job_id"], job["job_id"])


if __name__ == "__main__":
    unittest.main()
