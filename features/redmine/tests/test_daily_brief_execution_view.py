"""run_payload 执行视图契约测试：审计字段白名单与原始输出脱敏。

从 test_daily_brief_service.py 拆出，保持单文件在 600 行评审预算内。
"""

from __future__ import annotations

import asyncio
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

from features.redmine.daily_brief_report import DEFAULT_MODEL_LABEL
from features.redmine.kkagent import KkAgentAnalysisResult
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
                    "input_tokens": 120,
                    "output_tokens": 30,
                    "history_search_count": 2,
                    "distinct_history_search_count": 2,
                    "repair_attempts": 1,
                    "tools": [
                        {
                            "tool_name": "gms_rt_redmine_issue_fetch",
                            "status": "succeeded",
                            "output_sha256": "abc",
                            "output_preview": "PRIVATE",
                        },
                        {
                            "tool_name": "gms_rt_devices_snapshot",
                            "status": "succeeded",
                            "output_sha256": "device",
                        }
                    ],
                },
            )

        payload = self._execute_with_analyzer(fake_analyze)
        for issue in payload["issues"]:
            self.assertNotIn("raw_response", issue)
            self.assertEqual(issue["ai_execution"]["session_id"], "sess-1")
            self.assertTrue(issue["ai_execution"]["issue_fetched"])
            self.assertEqual(issue["ai_execution"]["device_evidence_status"], "succeeded")
            self.assertNotIn("tools", issue["ai_execution"])
            self.assertNotIn("PRIVATE", str(issue["ai_execution"]))
        statistics = payload["issues"][0]["ai_statistics"]
        self.assertEqual(statistics["execution_count"], 1)
        self.assertEqual(statistics["tokens"]["input_tokens"], 120)
        self.assertEqual(statistics["tokens"]["output_tokens"], 30)
        self.assertEqual(
            statistics["timing"]["total_duration_ms"],
            payload["issues"][0]["ai_execution"]["duration_ms"],
        )
        self.assertEqual(statistics["gms_tool_call_count"], 2)
        self.assertEqual(statistics["models"][0]["model_name"], DEFAULT_MODEL_LABEL)
        self.assertNotIn("PRIVATE", str(statistics))

    def test_failed_attempt_exposes_observed_gate_without_publishing_a_report(self):
        from features.redmine.daily_brief_models import DailyBriefIssue
        from features.redmine.daily_brief_report import issue_payload
        from features.redmine.kkagent.trace import KkAgentTrace, ToolTrace

        trace = KkAgentTrace(history_search_count=2, distinct_history_search_count=2)
        trace.tool_calls = [
            ToolTrace(tool_call_id="a", tool_name="gms_rt_redmine_issue_fetch",
                      tool_input={"issue_id": 123}, status="succeeded",
                      evidence_issue_ids=[123], snapshot_ids=["snapshot"]),
            ToolTrace(tool_call_id="b", tool_name="gms_rt_redmine_journals",
                      tool_input={"snapshot_id": "snapshot"}, status="succeeded"),
            ToolTrace(tool_call_id="c", tool_name="gms_rt_redmine_attachments",
                      tool_input={"snapshot_id": "snapshot"}, status="succeeded",
                      attachment_manifest_parsed=True, attachment_count=0),
            ToolTrace(tool_call_id="d", tool_name="gms_rt_sdk_search",
                      status="succeeded", source_evidence_valid=False,
                      source_result_count=0),
        ]
        record = DailyBriefIssue(run_id="db", issue_id=123, subject="CTS failure", buckets=[])
        attempt = {
            **trace.to_summary(), "final_ok": False,
            "failure_stage": "evidence_gate_failed",
            "analysis_review_findings": ["source version mismatch"],
        }
        payload = issue_payload(record, attempt)
        observed = payload["ai_execution"]["failed_attempt_evidence"]
        self.assertTrue(observed["issue_fetched"])
        self.assertTrue(observed["journals_checked"])
        self.assertTrue(observed["attachments_checked"])
        self.assertFalse(payload["ai_execution"]["source_evidence_checked"])
        self.assertEqual(payload["ai_execution"]["source_evidence_tool_count"], 0)
        self.assertEqual(payload["ai_execution"]["review_findings_count"], 1)
        self.assertNotIn("source version mismatch", str(observed))
        self.assertEqual(record.result, {})

    def test_successful_source_search_with_zero_hits_is_not_evidence(self):
        from features.redmine.daily_brief_models import DailyBriefIssue
        from features.redmine.daily_brief_report import issue_payload
        payload = issue_payload(DailyBriefIssue(
            run_id="db", issue_id=1, buckets=[]
        ), {
            "final_ok": False,
            "tools": [{"tool_name": "gms_rt_sdk_search", "status": "succeeded",
                       "source_evidence_valid": False, "source_result_count": 0}],
        })
        self.assertFalse(payload["ai_execution"]["source_evidence_checked"])
        self.assertEqual(payload["ai_execution"]["source_evidence_tool_count"], 0)

    def test_device_service_outage_is_not_presented_as_a_device_failure(self):
        from features.redmine.daily_brief_models import DailyBriefIssue
        from features.redmine.daily_brief_report import issue_payload

        payload = issue_payload(DailyBriefIssue(
            run_id="db", issue_id=1, buckets=[]
        ), {
            "tools": [{
                "tool_name": "gms_rt_devices_snapshot", "status": "failed",
                "failure_kind": "service_unavailable",
            }],
        })
        self.assertEqual(
            payload["ai_execution"]["device_evidence_status"], "service_unavailable"
        )

    def test_pending_device_trace_on_failed_run_is_not_presented_as_collecting(self):
        from features.redmine.daily_brief_models import DailyBriefIssue
        from features.redmine.daily_brief_report import issue_payload

        payload = issue_payload(DailyBriefIssue(
            run_id="db", issue_id=1, buckets=[]
        ), {
            "final_ok": False,
            "failure_stage": "kkagent_error",
            "tools": [{
                "tool_name": "gms_rt_devices_snapshot", "status": "pending",
            }],
        })
        self.assertEqual(
            payload["ai_execution"]["device_evidence_status"], "unavailable"
        )


if __name__ == "__main__":
    unittest.main()
