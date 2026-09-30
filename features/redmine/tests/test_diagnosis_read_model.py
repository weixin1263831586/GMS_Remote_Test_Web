"""Redmine IssueResult → DiagnosisReadModel 适配器测试（全局审查第二十节）。

覆盖：完整投影、execution 指纹接线、无结果/无指纹的空值语义、
issue_payload（daily_brief_report）里的 serve-time 接入。
"""

from __future__ import annotations

import unittest
import unittest.mock

from features.redmine.daily_brief_models import DailyBriefIssue
from features.redmine.daily_brief_report import issue_payload
from features.redmine.diagnosis_read_model import read_model_from_issue_result
from foundation.diagnosis_read_model import validate_diagnosis_read_model


def _result() -> dict:
    return {
        "problem_summary": "SystemUI 崩溃循环",
        "customer_request": "修复",
        "current_blocker": "待复现",
        "root_cause": "overlay 覆盖了 color 资源",
        "root_cause_type": "likely",
        "recommended_actions": [
            {"action": "比对 overlay", "step": 1, "reason": "先验证"},
            {"action": "抓取 dump", "step": 2},
        ],
        "suggested_solution": "回退 overlay",
        "detailed_report": "…",
        "evidence": [
            {"source": "logcat", "reference": "buffer.txt:120", "fact": "AndroidRuntime FATAL"},
        ],
        "similar_issues": [
            {"issue_id": 648526, "subject": "旧单", "similarity": "same",
             "reusable_fix": "fix", "reference_fact": "fact"},
        ],
        "confidence": 0.72,
        "risk": "medium",
        "missing_information": ["缺少复现步骤"],
        "suggested_reply_en": "…",
        "suggested_reply_zh": "…",
    }


def _execution() -> dict:
    return {
        "session_id": "s1",
        "status": "completed",
        "final_ok": True,
        "tools": [],
        "failure_identity": {
            "fingerprint": "fp-123",
            "module": "", "testcase": "", "assertion_class": "",
            "android_version": "17", "device_class": "",
        },
    }


class ReadModelFromIssueResultTests(unittest.TestCase):
    def test_full_projection_is_canonical(self):
        model = read_model_from_issue_result(
            _result(), issue_id=648900, subject="SystemUI crash",
        )
        self.assertEqual(validate_diagnosis_read_model(model), [])
        self.assertEqual(model["producer"], "redmine.issue_analysis")
        self.assertEqual(model["subject"], {"issue_id": 648900, "title": "SystemUI crash"})
        self.assertEqual(model["failure_identity"], None)
        self.assertEqual(model["conclusion"]["status"], "likely")
        self.assertEqual(model["conclusion"]["root_cause"], "overlay 覆盖了 color 资源")
        self.assertEqual(len(model["recommended_actions"]), 2)
        self.assertEqual(model["similar_cases"][0]["id"], 648526)
        self.assertEqual(model["similar_cases"][0]["kind"], "internal_case")
        self.assertEqual(model["similar_cases"][0]["fact"], "fact")

    def test_execution_fingerprint_feeds_identity_and_cluster_stub(self):
        model = read_model_from_issue_result(
            _result(), issue_id=1, execution=_execution(),
        )
        self.assertEqual(model["failure_identity"]["fingerprint"], "fp-123")
        self.assertEqual(
            model["failure_cluster"]["fingerprint"], "fp-123",
        )
        self.assertFalse(model["failure_cluster"]["aggregated"])

    def test_execution_without_fingerprint_degrades_to_none(self):
        model = read_model_from_issue_result(
            _result(), issue_id=1, execution={"session_id": "s"},
        )
        self.assertIsNone(model["failure_identity"])
        self.assertIsNone(model["failure_cluster"])

    def test_none_and_empty_results_return_none(self):
        self.assertIsNone(read_model_from_issue_result(None, issue_id=1))
        self.assertIsNone(read_model_from_issue_result({}, issue_id=1))

    def test_projection_does_not_mutate_inputs(self):
        result = _result()
        execution = _execution()
        read_model_from_issue_result(result, issue_id=1, execution=execution)
        self.assertNotIn("schema_version", result)
        self.assertEqual(len(result["recommended_actions"]), 2)
        self.assertIn("fingerprint", execution["failure_identity"])


class IssuePayloadIntegrationTests(unittest.TestCase):
    def test_issue_payload_carries_read_model(self):
        payload = issue_payload(DailyBriefIssue(
            run_id="r", issue_id=9, buckets=[], result=_result(),
        ))
        self.assertEqual(validate_diagnosis_read_model(payload["read_model"]), [])
        self.assertEqual(payload["read_model"]["subject"]["issue_id"], 9)

    def test_issue_payload_without_result_has_no_read_model(self):
        payload = issue_payload(DailyBriefIssue(run_id="r", issue_id=9, buckets=[]))
        self.assertNotIn("read_model", payload)

    def test_issue_payload_read_model_failure_never_breaks_payload(self):
        issue = DailyBriefIssue(run_id="r", issue_id=9, buckets=[], result=_result())
        with unittest.mock.patch(
            "features.redmine.diagnosis_read_model.read_model_from_issue_result",
            side_effect=RuntimeError("boom"),
        ):
            payload = issue_payload(issue)
        self.assertNotIn("read_model", payload)
        self.assertEqual(payload["issue_id"], 9)


if __name__ == "__main__":
    unittest.main()
