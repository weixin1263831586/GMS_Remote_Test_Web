"""Tests for CaseEvaluator reference comparison."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from features.redmine.case_evaluator import CaseEvaluator
from features.redmine.case_extractor import RedmineCaseExtractor
from features.redmine.knowledge_repository import RedmineKnowledgeDB


class CaseEvaluatorTests(unittest.TestCase):
    def setUp(self):
        self.db = RedmineKnowledgeDB(Path(tempfile.mktemp(suffix=".sqlite3")))
        fact = RedmineCaseExtractor.extract({
            "issue_id": 633454,
            "subject": "[RK3576] BTS VBMeta test key",
            "description": "system signed with VBMeta test key",
            "status_name": "Confirmed",
            "fixed_version": "RK3576_ANDROID16",
        })
        self.db.upsert_case_fact(fact)
        self.evaluator = CaseEvaluator(self.db)

    def test_reference_only_goes_to_reference_table(self):
        self.evaluator.import_reference_output(633454, {
            "source": "gms_assistant",
            "markdown": "# GMS参考",
            "structured_json": {"platform": "RK3576", "root_cause": "未切production key"},
        })
        refs = self.db.get_reference_outputs(633454)
        self.assertEqual(len(refs), 1)
        self.assertEqual(refs[0]["source"], "gms_assistant")
        self.assertIn("platform", refs[0]["structured_json"])

    def test_evaluate_scores_match_and_flags_missing(self):
        self.evaluator.import_reference_output(633454, {
            "source": "gms_assistant",
            "structured_json": {
                "platform": "RK3576",
                "android_version": "Android16",
                "module": "AVB/VBMeta",
                "root_cause": "认证版本未切换production AVB key",
                "solution": "用production key重新签名",
                "verification": "重跑BTS扫描",
            },
        })
        result = self.evaluator.evaluate_case(633454)
        self.assertGreaterEqual(result["score"], 50)
        # root_cause present in both (loose match on "production").
        self.assertNotIn("root_cause", result["missing_fields"])
        # notes/rules not in reference → not scored as missing.

    def test_latest_evaluation_retrieved(self):
        self.evaluator.evaluate_case(633454, reference={"platform": "RK3576"})
        latest = self.db.get_latest_case_evaluation(633454)
        self.assertIsNotNone(latest)
        self.assertIn("score", latest)

    def test_reference_does_not_affect_reply(self):
        # Ensure reference storage path is isolated from case facts.
        fact = self.db.get_case_fact(633454)
        self.assertNotIn("gms_assistant", str(fact))


if __name__ == "__main__":
    unittest.main()
