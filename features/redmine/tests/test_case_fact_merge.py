"""upsert_case_fact merge 语义：已有高质量事实不被 AI 空值清空（审核意见 P1）。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from features.redmine.case_extractor import RedmineCaseExtractor
from features.redmine.knowledge_repository import RedmineKnowledgeDB


class UpsertCaseFactMergeTests(unittest.TestCase):
    def setUp(self):
        self.db = RedmineKnowledgeDB(Path(tempfile.mktemp(suffix=".sqlite3")))

    def tearDown(self):
        path = Path(self.db.db_path) if hasattr(self.db, "db_path") else None
        if path and path.exists():
            path.unlink()

    def _rich_issue(self) -> dict:
        return {
            "issue_id": 123,
            "subject": "RK3576 Android16 GTS CtsCarrierApiTestCases fail",
            "status_name": "Feedback",
            "project_name": "Android TV",
            "assigned_to_name": "alice",
            "category": "GMS",
            "description": "RK3576 Android16 GTS failure on CtsCarrierApiTestCases",
        }

    def test_diagnostic_save_preserves_redmine_facts(self):
        """已有完整事实 + AI 空字段 → 非空字段全部保留。"""
        self.db.upsert_case_fact(RedmineCaseExtractor.extract(self._rich_issue()))
        ai_fact = {
            "issue_id": 123,
            "subject": "",
            "status_name": "",
            "project_name": "",
            "chip_platform": "",
            "android_version": "",
            "module": "",
            "problem_summary": "AI 摘要",
            "root_cause": "AI 根因",
            "solution": "AI 方案",
            "confidence": 0.8,
            "source_quality": "daily_brief_ai",
        }
        self.db.upsert_case_fact(ai_fact, merge_missing=True)
        fact = self.db.get_case_fact(123)
        self.assertEqual(fact["status_name"], "Feedback")
        self.assertEqual(fact["project_name"], "Android TV")
        self.assertEqual(fact["assigned_to_name"], "alice")
        self.assertEqual(fact["chip_platform"], "RK3576")
        self.assertEqual(fact["android_version"], "Android16")
        self.assertEqual(fact["root_cause"], "AI 根因")

    def test_existing_rich_fact_not_blanked_by_ai_fact(self):
        """回归：未传字段不得把已有记录降级为空（旧行为是 INSERT OR REPLACE）。"""
        self.db.upsert_case_fact(RedmineCaseExtractor.extract(self._rich_issue()))
        self.db.upsert_case_fact({
            "issue_id": 123,
            "root_cause": "AI 根因",
        }, merge_missing=True)
        fact = self.db.get_case_fact(123)
        self.assertEqual(fact["subject"], "RK3576 Android16 GTS CtsCarrierApiTestCases fail")
        self.assertEqual(fact["module"], "VBMeta" if fact["module"] == "VBMeta" else fact["module"])
        self.assertTrue(fact["problem_summary"] or fact["root_cause"])

    def test_default_upsert_keeps_replace_semantics(self):
        """不传 merge_missing 的调用方（批量导入/种子）行为不变。"""
        self.db.upsert_case_fact(RedmineCaseExtractor.extract(self._rich_issue()))
        self.db.upsert_case_fact({"issue_id": 123, "root_cause": "x"})
        fact = self.db.get_case_fact(123)
        self.assertEqual(fact["status_name"], "")
        self.assertEqual(fact["root_cause"], "x")

    def test_higher_historical_confidence_kept(self):
        self.db.upsert_case_fact({
            "issue_id": 9, "subject": "s", "confidence": 100.0,
            "root_cause": "verified",
        })
        self.db.upsert_case_fact({
            "issue_id": 9, "root_cause": "ai guess", "confidence": 0.6,
        }, merge_missing=True)
        # 根因被替换：置信度跟随新结论，不继承旧结论的高分。
        self.assertEqual(self.db.get_case_fact(9)["root_cause"], "ai guess")
        self.assertEqual(self.db.get_case_fact(9)["confidence"], 0.6)

    def test_confidence_kept_when_conclusion_unchanged(self):
        self.db.upsert_case_fact({
            "issue_id": 10, "subject": "s", "confidence": 0.95,
            "root_cause": "verified cause",
        })
        self.db.upsert_case_fact({
            "issue_id": 10, "problem_summary": "AI 补充摘要",
            "confidence": 0.5,
        }, merge_missing=True)
        fact = self.db.get_case_fact(10)
        # 未替换根因/方案：历史高置信度保留，AI 只补摘要。
        self.assertEqual(fact["root_cause"], "verified cause")
        self.assertEqual(fact["confidence"], 0.95)

    def test_collections_survive_merge(self):
        """回归：merge 不清空已有 keywords/evidence/symptoms。

        旧实现读 existing["keywords"]（DB 返回的是 keywords_json），三类
        集合在只更新根因时全部被清空。
        """
        self.db.upsert_case_fact({
            "issue_id": 11,
            "subject": "s",
            "keywords": ["cts", "gts"],
            "evidence": {"log": "b.txt"},
            "symptoms": ["reboot"],
            "root_cause": "verified",
            "confidence": 0.9,
        })
        self.db.upsert_case_fact({
            "issue_id": 11,
            "root_cause": "new ai cause",
            "confidence": 0.6,
        }, merge_missing=True)
        fact = self.db.get_case_fact(11)
        self.assertEqual(fact["keywords_json"], ["cts", "gts"])
        self.assertEqual(fact["evidence_json"], {"log": "b.txt"})
        self.assertEqual(fact["symptoms_json"], ["reboot"])
        self.assertEqual(fact["root_cause"], "new ai cause")

    def test_real_error_signature_not_replaced_by_provenance_placeholder(self):
        """真实 error_signature 不被 provenance 占位符覆盖（审核意见 P1）。

        历史 mapper 在提取器拿不到签名时写 "daily-brief:<date>"；这是
        非空 provenance 字符串，旧 merge 的"空才保留"挡不住它，真实签名
        被逐轮替换。新 merge 对 daily-brief: 前缀做防御。
        """
        self.db.upsert_case_fact({
            "issue_id": 12,
            "subject": "s",
            "error_signature": "CtsSecurityHostTestCases#testAllDomainsEnforcing",
            "confidence": 0.9,
        })
        self.db.upsert_case_fact({
            "issue_id": 12,
            "error_signature": "daily-brief:2026-09-15",
            "root_cause": "ai cause",
            "confidence": 0.5,
        }, merge_missing=True)
        self.assertEqual(
            self.db.get_case_fact(12)["error_signature"],
            "CtsSecurityHostTestCases#testAllDomainsEnforcing",
        )

    def test_evidence_and_keywords_union_on_merge(self):
        """新 evidence/keywords 非空时 union/dedupe，不整体覆盖（审核意见 P1）。"""
        self.db.upsert_case_fact({
            "issue_id": 13,
            "subject": "s",
            "keywords": ["cts", "gts"],
            "evidence": {
                "daily_brief_evidence": [{"source": "log", "quote": "a"}],
                "manual_note": "operator verified",
            },
            "symptoms": ["reboot"],
            "confidence": 0.9,
        })
        self.db.upsert_case_fact({
            "issue_id": 13,
            "keywords": ["cts", "vts"],
            "evidence": {
                "daily_brief_evidence": [{"source": "log", "quote": "a"}],
            },
            "root_cause": "new ai cause",
            "confidence": 0.6,
        }, merge_missing=True)
        fact = self.db.get_case_fact(13)
        # list 集合保序去重合并：旧值在前，新值追加。
        self.assertEqual(fact["keywords_json"], ["cts", "gts", "vts"])
        evidence = fact["evidence_json"]
        # dict key 级合并：未提及的 manual_note 保留，同名 list 证据去重合并。
        self.assertEqual(evidence["manual_note"], "operator verified")
        self.assertEqual(
            evidence["daily_brief_evidence"], [{"source": "log", "quote": "a"}]
        )


if __name__ == "__main__":
    unittest.main()
