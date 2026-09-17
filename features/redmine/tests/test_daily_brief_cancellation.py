"""Daily Brief 跨进程持久取消测试。"""

from __future__ import annotations

import asyncio
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

from features.redmine.kkagent_analyzer import KkAgentAnalysisResult
from features.redmine.tests.test_daily_brief_service import (
    SNAPSHOT,
    VALID,
    make_service,
    patch_preflight_ok,
)


class DailyBriefCancellationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.service = make_service(Path(self._tmp.name))
        preflight = patch_preflight_ok()
        preflight.start()
        self.addCleanup(preflight.stop)

    def _patch_snapshot(self):
        return patch.object(
            self.service,
            "build_triage",
            AsyncMock(return_value=dict(SNAPSHOT)),
        )

    def test_persisted_cancel_stops_active_analyzer_without_shared_task(self):
        """独立 Worker 轮询 DB 标志并终止当前 KkAgent。"""
        started = self.service.start_run("nightly")
        run_id = started["run_id"]
        entered = asyncio.Event()
        analyzer_cancelled = asyncio.Event()

        async def slow_analyze(_entry):
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                analyzer_cancelled.set()
                raise

        async def scenario():
            with self._patch_snapshot(), patch.object(
                self.service, "_build_analyzer"
            ) as builder:
                builder.return_value.analyze = slow_analyze
                task = asyncio.create_task(self.service.execute_run(run_id))
                # The full repository suite can start several subprocesses at
                # once; allow scheduler contention without changing the
                # cancellation behavior being tested.
                await asyncio.wait_for(entered.wait(), timeout=5)
                # 模拟 Web 与 Worker 分属两个进程：只写持久标志。
                self.service.repository.request_cancel(run_id)
                return await asyncio.wait_for(task, timeout=2)

        run = asyncio.run(scenario())
        self.assertEqual(run.status, "cancelled")
        self.assertTrue(analyzer_cancelled.is_set())
        issue = self.service.repository.get_issue(run_id, 101)
        self.assertEqual(issue.status, "pending")
        self.assertEqual(issue.started_at, "")

    def test_persisted_cancel_stops_single_issue_reanalysis(self):
        started = self.service.start_run("manual")
        run_id = started["run_id"]

        async def initial_analyze(_entry):
            return KkAgentAnalysisResult(ok=True, result=dict(VALID))

        with self._patch_snapshot(), patch.object(
            self.service, "_build_analyzer"
        ) as builder:
            # 假 analyzer 必须带空 env_extra：否则 Mock 的 env_extra 会被
            # 深度分析取证预采集当成已绑定 profile，真实拉起 gms-rt CLI。
            builder.return_value.env_extra = {}
            builder.return_value.analyze = initial_analyze
            completed = asyncio.run(self.service.execute_run(run_id))

        # Worker 领取 issue job 后会先把 run 转为 analyzing。
        active = self.service.repository.get_run(run_id)
        active.status = "analyzing"
        active.error = ""
        active.finished_at = ""
        self.service.repository.update_run(active)

        entered = asyncio.Event()
        analyzer_cancelled = asyncio.Event()

        async def slow_analyze(_entry):
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                analyzer_cancelled.set()
                raise

        async def scenario():
            with patch.object(self.service, "_build_analyzer") as builder:
                builder.return_value.env_extra = {}
                builder.return_value.analyze = slow_analyze
                task = asyncio.create_task(
                    self.service.reanalyze_issue(completed.brief_date, 101)
                )
                await asyncio.wait_for(entered.wait(), timeout=1)
                self.service.repository.request_cancel(run_id)
                return await asyncio.wait_for(task, timeout=2)

        result = asyncio.run(scenario())
        self.assertEqual(result["status"], "cancelled")
        self.assertTrue(analyzer_cancelled.is_set())
        self.assertEqual(self.service.repository.get_run(run_id).status, "cancelled")
        # 2850cff 定版语义：被停止的单条分析收敛为 cancelled 终态
        #（与 worker issue-job 路径一致）；重新分析会覆盖该状态。
        self.assertEqual(
            self.service.repository.get_issue(run_id, 101).status, "cancelled"
        )


if __name__ == "__main__":
    unittest.main()
