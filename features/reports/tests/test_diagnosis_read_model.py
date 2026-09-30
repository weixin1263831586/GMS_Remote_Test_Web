"""报告诊断 → DiagnosisReadModel 适配器测试（全局审查第二十节）。

覆盖：五路召回到 canonical 章节的投影、evidence/background 分层
（ADR 0014）、AI 降级时的 conclusion 语义、失败指纹派生与降级、
analysis_api 编排结果的 serve-time 接入。
"""

from __future__ import annotations

import unittest
import unittest.mock

from features.reports.diagnosis_read_model import (
    read_model_from_report_diagnosis,
)
from foundation.diagnosis_read_model import validate_diagnosis_read_model


def _diagnosis() -> dict:
    return {
        "test_name": "android.systemui.cts.DynamicColorsTest#testColorOverlay",
        "error_message": "junit.framework.AssertionError: expected overlay color",
        "stack_trace": "at android.systemui.cts.DynamicColorsTest.testColorOverlay(DynamicColorsTest.java:88)",
        "module": "CtsSystemUITestCases",
        "report_name": "2026-09-30-run",
        "failure_index": 2,
        "android_version": "17",
        "summary": "Dynamic colors overlay 覆盖冲突",
        "ai_result": {
            "root_cause": "RRO overlay 覆盖了 dynamic color 资源",
            "analysis": "…",
            "suggestions": ["检查 vendor overlay", "比对基线资源"],
            "ai_enabled": True,
            "ai_model": "LocalQwen",
            "ai_provider": "local",
            "ai_fallback_used": False,
            "ai_providers_attempted": ["local"],
            "root_cause_evidence": [
                {"source": "logcat", "reference": "log.txt:10", "fact": "FATAL EXCEPTION"},
            ],
        },
        "source_search_results": [
            {"path": "frameworks/base/libs/WindowManager.java", "line": 120, "snippet": "mOverlay"},
        ],
        "knowledge_base_results": [
            {"issue_id": 648526, "subject": "历史同因", "similarity": "similar"},
        ],
        "system_background_results": [
            {"title": "OverlayManager", "source": "android_internals",
             "source_path": "overlay.md", "evidence_level": "background",
             "source_revision": "1f47669c", "license": "CC BY-NC-SA 4.0",
             "anchor_verifications": [{"evidence_level": "path_matched"}]},
        ],
        "suite_target": {"suite": "android-cts-17_r1", "match_notes": []},
        "mainline_exemptions": [],
        "mainline_exempt": False,
    }


class ReadModelFromReportDiagnosisTests(unittest.TestCase):
    def test_full_projection_is_canonical(self):
        model = read_model_from_report_diagnosis(_diagnosis())
        self.assertEqual(validate_diagnosis_read_model(model), [])
        self.assertEqual(model["producer"], "reports.diagnose")
        self.assertEqual(model["subject"]["module"], "CtsSystemUITestCases")
        self.assertEqual(model["subject"]["failure_index"], 2)

    def test_evidence_layering(self):
        model = read_model_from_report_diagnosis(_diagnosis())
        kinds = [e["kind"] for e in model["evidence"]]
        self.assertIn("ai_evidence", kinds)
        self.assertIn("source_anchor", kinds)
        anchor = next(e for e in model["evidence"] if e["kind"] == "source_anchor")
        self.assertEqual(anchor["reference"], "frameworks/base/libs/WindowManager.java:120")
        # 内部案例是 similar_cases，不是 evidence。
        self.assertEqual(model["similar_cases"][0]["id"], 648526)

    def test_background_stays_background(self):
        model = read_model_from_report_diagnosis(_diagnosis())
        self.assertEqual(len(model["background"]), 1)
        self.assertEqual(model["background"][0]["title"], "OverlayManager")
        # 顶层盖章 = background；核验状态在 anchor_status（封闭三态）。
        self.assertEqual(model["background"][0]["evidence_level"], "background")
        self.assertEqual(model["background"][0]["anchor_status"], "path_matched")
        self.assertEqual(model["background"][0]["license"], "CC BY-NC-SA 4.0")

    def test_ai_success_marks_likely(self):
        model = read_model_from_report_diagnosis(_diagnosis())
        self.assertEqual(model["conclusion"]["status"], "likely")
        self.assertEqual(model["conclusion"]["provider"]["provider"], "local")

    def test_rule_fallback_marks_possible_not_likely(self):
        diagnosis = _diagnosis()
        diagnosis["ai_result"] = {
            "root_cause": "超时模式匹配（规则兜底）",
            "ai_enabled": False,
            "ai_error": "provider unavailable",
        }
        model = read_model_from_report_diagnosis(diagnosis)
        self.assertEqual(model["conclusion"]["status"], "possible")
        self.assertEqual(
            model["conclusion"]["missing_information"], ["provider unavailable"],
        )

    def test_no_root_cause_marks_unknown_with_missing_info(self):
        diagnosis = _diagnosis()
        diagnosis["ai_result"] = {"ai_enabled": False}
        diagnosis["summary"] = "Diagnosis orchestration complete"
        model = read_model_from_report_diagnosis(diagnosis)
        self.assertEqual(model["conclusion"]["status"], "unknown")
        self.assertTrue(model["conclusion"]["missing_information"])

    def test_mainline_exempt_raises_risk(self):
        diagnosis = _diagnosis()
        diagnosis["mainline_exempt"] = True
        model = read_model_from_report_diagnosis(diagnosis)
        self.assertEqual(model["conclusion"]["risk"], "high")

    def test_none_and_empty_input_return_none(self):
        self.assertIsNone(read_model_from_report_diagnosis(None))
        self.assertIsNone(read_model_from_report_diagnosis({}))

    def test_projection_does_not_mutate_input(self):
        diagnosis = _diagnosis()
        read_model_from_report_diagnosis(diagnosis)
        self.assertNotIn("read_model", diagnosis)
        self.assertEqual(len(diagnosis["source_search_results"]), 1)


class FailureIdentityDerivationTests(unittest.TestCase):
    def test_fingerprint_derived_from_failure_context(self):
        model = read_model_from_report_diagnosis(_diagnosis())
        identity = model["failure_identity"]
        self.assertIsNotNone(identity)
        self.assertEqual(identity["module"], "CtsSystemUITestCases")
        self.assertTrue(identity["fingerprint"])
        self.assertEqual(model["failure_cluster"]["fingerprint"], identity["fingerprint"])

    def test_identity_import_failure_degrades_to_none(self):
        with unittest.mock.patch.dict(
            "sys.modules", {"features.redmine": None, "features": None},
        ):
            model = read_model_from_report_diagnosis(_diagnosis())
        self.assertIsNone(model["failure_identity"])


if __name__ == "__main__":
    unittest.main()
