"""Tests for Redmine issue repository search behavior."""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from features.redmine.api import _enrich_issue_for_display
from features.redmine.knowledge_repository import RedmineKnowledgeDB
from features.redmine.repository import RedmineAgentDB
from features.redmine.service import RedmineService


class RepositorySearchTests(unittest.TestCase):
    def test_redmine_stores_recover_after_runtime_data_directory_deletion(self):
        root = Path(tempfile.mkdtemp())
        try:
            repo = RedmineAgentDB(root / "redmine.sqlite3", root / "docs")
            knowledge = RedmineKnowledgeDB(root / "knowledge.sqlite3")

            shutil.rmtree(root)

            self.assertEqual(repo.list_all_issues(), [])
            self.assertIsNone(knowledge.get_case_fact(123))
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_list_all_issues_search_matches_issue_id(self):
        repo = RedmineAgentDB(Path(tempfile.mktemp(suffix=".sqlite3")), Path(tempfile.mkdtemp()))
        repo.upsert_issue({
            "issue_id": 598972,
            "run_id": "test",
            "subject": "RK3576 Android16 VtsHalPowerTargetTest模块PowerAidl#hasFixedPerformance",
            "description": "PowerAidl failure without numeric id in text",
            "status_name": "Closed",
            "priority_name": "Normal",
            "journals_json": [],
            "attachments_json": [],
            "failures_json": [],
            "references_json": [],
            "ai_json": {},
        })

        rows = repo.list_all_issues(search="598972")

        self.assertEqual([row["issue_id"] for row in rows], [598972])

    def test_display_enrichment_adds_legacy_attachment_links_and_document(self):
        repo = RedmineAgentDB(Path(tempfile.mktemp(suffix=".sqlite3")), Path(tempfile.mkdtemp()))
        repo.upsert_issue({
            "issue_id": 598972,
            "run_id": "test",
            "subject": "RK3576 Android16 VtsHalPowerTargetTest模块PowerAidl#hasFixedPerformance",
            "description": "Power/PowerAidl#hasFixedPerformance FAILURE\nActual: false\nExpected: true",
            "status_name": "Closed",
            "priority_name": "Normal",
            "journals_json": [],
            "attachments_json": [],
            "failures_json": [],
            "references_json": [],
            "ai_json": {},
        })
        service = RedmineService(
            repository=repo,
            knowledge_db=RedmineKnowledgeDB(Path(tempfile.mktemp(suffix=".sqlite3"))),
        )

        enriched = _enrich_issue_for_display(service, repo.get_issue(598972))

        filenames = [item["filename"] for item in enriched["attachment_links"]]
        self.assertIn("VtsHalPowerTargetTest.zip", filenames)
        self.assertIn("0da1ee9.diff", filenames)
        self.assertIn("# Redmine #598972", enriched["doc_content"])
        self.assertIn("附件链接", enriched["doc_content"])


if __name__ == "__main__":
    unittest.main()


class HistorySearchTests(unittest.TestCase):
    """search_history：跨工单历史检索（相似问题 + 可参考修复）。"""

    def _repo_with_issues(self) -> RedmineAgentDB:
        repo = RedmineAgentDB(Path(tempfile.mktemp(suffix=".sqlite3")), Path(tempfile.mkdtemp()))
        repo.upsert_issue({
            "issue_id": 646504, "run_id": "t", "status_name": "Closed",
            "subject": "RK3576 Android16 SSI merge 编译错误",
            "description": "SSI+GRF 合包", "journals_json": [], "attachments_json": [],
            "failures_json": [], "references_json": [], "ai_json": {},
            "is_resolved": 1, "solution": "SSI 包先 apply，再 merge GRF 补丁，最后替换 apex 签名",
        })
        repo.upsert_issue({
            "issue_id": 650761, "run_id": "t", "status_name": "Feedback",
            "subject": "RK3562 Android16 SSI SDK 支持咨询",
            "description": "Android16 SDK 是否支持 RK3562", "journals_json": [],
            "attachments_json": [], "failures_json": [], "references_json": [],
            "ai_json": {}, "is_resolved": 0,
        })
        return repo

    def test_search_history_returns_resolved_first_with_fix(self):
        repo = self._repo_with_issues()
        hits = repo.search_history("Android16 SSI", exclude_issue_id=650761, limit=5)
        self.assertTrue(hits)
        self.assertEqual(hits[0]["issue_id"], 646504)
        self.assertTrue(hits[0]["is_resolved"])
        self.assertIn("merge", hits[0]["solution"])
        self.assertNotIn(650761, [h["issue_id"] for h in hits])

    def test_search_history_trims_and_limits(self):
        repo = self._repo_with_issues()
        repo.upsert_issue({
            "issue_id": 1, "run_id": "t", "status_name": "Closed",
            "subject": "Android16 SSI 其他", "description": "x", "journals_json": [],
            "attachments_json": [], "failures_json": [], "references_json": [],
            "ai_json": {}, "is_resolved": 1, "solution": "长" * 1000,
        })
        hits = repo.search_history("Android16 SSI", limit=2)
        self.assertEqual(len(hits), 2)
        long_fix = next(h for h in hits if h["issue_id"] == 1)
        self.assertLessEqual(len(long_fix["solution"]), 401)  # 400 + 省略号

    def test_search_history_empty_query(self):
        repo = self._repo_with_issues()
        self.assertEqual(repo.search_history("   "), [])

    def test_search_history_flags_background_only_hits(self):
        """只命中 SoC 型号等背景词的条目 distinctive_matches 必须为空。"""
        repo = self._repo_with_issues()
        # 补 3 条 RK3588 工单让 RK3588 df 占比超线，同时保持 power_ext 稀缺。
        for i in (649356, 647558, 643254):
            repo.upsert_issue({
                "issue_id": i, "run_id": "t", "status_name": "Closed",
                "subject": f"RK3588 Android16 EDLA GTS 用例{i}",
                "description": "RK3588 平台通用测试问题", "journals_json": [],
                "attachments_json": [], "failures_json": [], "references_json": [],
                "ai_json": {}, "is_resolved": 1, "solution": "通用适配",
            })
        repo.upsert_issue({
            "issue_id": 598972, "run_id": "t", "status_name": "Closed",
            "subject": "RK3576 Android16 VtsHalPowerTargetTest power_ext 不支持",
            "description": "power_ext FIXED_PERFORMANCE 签名",
            "journals_json": [], "attachments_json": [], "failures_json": [],
            "references_json": [], "ai_json": {}, "is_resolved": 1,
            "solution": "补齐 power_ext fixed performance 配置",
        })
        hits = repo.search_history("RK3588 power_ext", exclude_issue_id=650761, limit=10)
        by_id = {h["issue_id"]: h for h in hits}
        # 仅命中背景词（SoC 型号）→ 无区分词；命中故障签名词 → 有区分词。
        self.assertEqual(by_id[649356]["matched_terms"], ["RK3588"])
        self.assertEqual(by_id[649356]["distinctive_matches"], [])
        self.assertIn("power_ext", by_id[598972]["distinctive_matches"])
        # IDF 加权重排：同为已解决时，命中区分词的排在前。
        self.assertLess(
            hits.index(by_id[598972]),
            hits.index(by_id[649356]),
        )
