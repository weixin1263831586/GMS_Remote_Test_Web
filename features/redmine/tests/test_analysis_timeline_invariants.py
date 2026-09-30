"""分析时间线不变量：每个 ``analysis_started`` 必须恰好收敛到一条终态。

duplicate AI 调用（execution ledger 防重复）早退路径曾只写
``analysis_started`` 就直接 return，时间线永远悬在 in-progress，污染
时长统计与卡死检测（全局审查 P1）。这里用不变量断言兜底该早退路径：
事件流必须 ended as started。
"""

from __future__ import annotations

import asyncio
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from features.redmine.daily_brief_analysis_events import (
    event_store_for_repository,
)
from features.redmine.daily_brief_models import DailyBriefIssue, DailyBriefRun
from features.redmine.tests.test_daily_brief_service import make_service


TERMINAL_EVENTS = {"analysis_completed", "analysis_failed"}


class AnalysisTimelineInvariantTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.service = make_service(Path(self._tmp.name))

    def _make_run(self, run_id: str) -> DailyBriefRun:
        run = DailyBriefRun(
            owner_id="u1", brief_date="2026-09-30", mode="issue:652654",
            run_id=run_id, status="analyzing",
        )
        self.service.repository.create_run(run)
        self.service.repository.upsert_issue(DailyBriefIssue(
            run_id=run.run_id, issue_id=652654, buckets=[],
            subject="CTS failure",
        ))
        return run

    def test_duplicate_ai_call_leaves_a_terminal_timeline(self):
        run = self._make_run("db_dup")
        duplicate = {"duplicate": True, "receipt_id": "rcp_seed"}
        with patch.object(self.service.ai_ledger, "begin", return_value=duplicate):
            asyncio.run(self.service._analyze_one(
                run, 652654,
                {"issue_id": 652654, "analysis_mode": "diagnostic"},
                object(), {},
            ))

        record = self.service.repository.get_issue(run.run_id, 652654)
        self.assertEqual(record.status, "failed")
        self.assertEqual(record.error_type, "ai_call_in_flight")

        store = event_store_for_repository(self.service.repository)
        events = store.list_after(run.run_id, 652654, after_sequence=0)
        started = [e for e in events if e["event_type"] == "analysis_started"]
        terminal = [e for e in events if e["event_type"] in TERMINAL_EVENTS]
        # 不变量：started 数 == 终态数（本路径应为 1 == 1）。
        self.assertEqual(len(started), 1)
        self.assertEqual(len(terminal), 1)
        self.assertEqual(terminal[0]["event_type"], "analysis_failed")
        self.assertEqual(terminal[0]["status"], "failed")
        self.assertIn("ai_call_in_flight", terminal[0]["summary"])


if __name__ == "__main__":
    unittest.main()
