"""Daily Brief Agent-token preflight orchestration tests."""

from __future__ import annotations

import asyncio
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

from features.redmine.kkagent_analyzer import KkAgentAnalysisResult

from .test_daily_brief_service import SNAPSHOT, VALID, make_service


class AuthPreflightTests(unittest.TestCase):
    """A revoked token fails fast before a run spends AI turns on MCP errors."""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.service = make_service(Path(self._tmp.name))

    @staticmethod
    def _patch_preflight(ok: bool, reason: str):
        return patch(
            "features.redmine.daily_brief_service.preflight_gms_auth",
            AsyncMock(return_value=(ok, reason)),
        )

    def test_revoked_token_fails_whole_run_before_analysis(self):
        started = self.service.start_run("nightly")
        reason = (
            "GMS agent token 失效（profile 'kkagent-host'）。"
            "请重新注册: gms-rt-agent-enroll <CODE>"
        )
        with self._patch_preflight(False, reason), patch.object(
            self.service, "build_triage"
        ) as triage:
            run = asyncio.run(self.service.execute_run(started["run_id"]))
        self.assertEqual(run.status, "failed")
        self.assertIn("gms-rt-agent-enroll", run.error)
        triage.assert_not_called()

    def test_revoked_token_blocks_reanalyze(self):
        async def ok_analyze(_entry):
            return KkAgentAnalysisResult(ok=True, result=dict(VALID))

        started = self.service.start_run("manual")
        run_id = started["run_id"]
        with patch.object(
            self.service,
            "build_triage",
            AsyncMock(return_value=dict(SNAPSHOT)),
        ), patch.object(self.service, "_build_analyzer") as builder:
            builder.return_value.analyze = ok_analyze
            completed = asyncio.run(self.service.execute_run(run_id))

        reason = "GMS agent token 失效。请重新注册: gms-rt-agent-enroll <CODE>"
        with self._patch_preflight(False, reason), patch.object(
            self.service, "_build_analyzer"
        ) as builder:
            builder.return_value.analyze = ok_analyze
            result = asyncio.run(
                self.service.reanalyze_issue(completed.brief_date, 101)
            )
        self.assertIn("gms-rt-agent-enroll", result.get("error", ""))

        with self._patch_preflight(True, ""), patch.object(
            self.service, "_build_analyzer"
        ) as builder:
            builder.return_value.analyze = ok_analyze
            result = asyncio.run(
                self.service.reanalyze_issue(completed.brief_date, 101)
            )
        self.assertEqual(result.get("status"), "completed")


if __name__ == "__main__":
    unittest.main()
