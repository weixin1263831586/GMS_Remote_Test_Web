"""run_payload 执行视图契约测试：审计字段白名单与原始输出脱敏。

从 test_daily_brief_service.py 拆出，保持单文件在 600 行评审预算内。
"""

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


class RunPayloadExecutionViewTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.service = make_service(Path(self._tmp.name))
        preflight = patch_preflight_ok()
        preflight.start()
        self.addCleanup(preflight.stop)

    def _patch_snapshot(self):
        mock = AsyncMock(return_value=dict(SNAPSHOT))
        return patch.object(self.service, "build_triage", mock)

    def _execute_with_analyzer(self, fake_analyze):
        run = self.service.start_run("manual")
        run_id = run["run_id"]
        with self._patch_snapshot(), patch.object(self.service, "_build_analyzer") as builder:
            builder.return_value.analyze = fake_analyze
            asyncio.run(self.service.execute_run(run_id))
        return self.service.run_payload(self.service.repository.get_run(run_id))

    def test_run_payload_hides_raw_response(self):
        async def fake_analyze(entry):
            return KkAgentAnalysisResult(
                ok=True,
                result=dict(VALID),
                raw_output="SECRET-RAW",
                trace={
                    "session_id": "sess-1",
                    "status": "completed",
                    "tool_call_count": 2,
                    "history_search_count": 2,
                    "distinct_history_search_count": 2,
                    "repair_attempts": 1,
                    "tools": [
                        {
                            "tool_name": "gms_rt_redmine_issue_fetch",
                            "status": "succeeded",
                            "output_sha256": "abc",
                            "output_preview": "PRIVATE",
                        }
                    ],
                },
            )

        payload = self._execute_with_analyzer(fake_analyze)
        for issue in payload["issues"]:
            self.assertNotIn("raw_response", issue)
            self.assertEqual(issue["ai_execution"]["session_id"], "sess-1")
            self.assertTrue(issue["ai_execution"]["issue_fetched"])
            self.assertNotIn("tools", issue["ai_execution"])
            self.assertNotIn("PRIVATE", str(issue["ai_execution"]))


if __name__ == "__main__":
    unittest.main()
