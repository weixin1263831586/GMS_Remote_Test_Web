"""Tests for RedmineCaseSearch similarity retrieval."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from features.redmine.case_search import RedmineCaseSearch
from features.redmine.case_extractor import RedmineCaseExtractor
from features.redmine.knowledge_repository import RedmineKnowledgeDB


def _seed_vbmeta(db: RedmineKnowledgeDB, issue_id: int, subject: str, status: str = "Confirmed") -> None:
    issue = {
        "issue_id": issue_id,
        "subject": subject,
        "description": "The partition 'system' is signed with a publicly known VBMeta test key",
        "status_name": status,
        "fixed_version": "RK3576_ANDROID16",
    }
    fact = RedmineCaseExtractor.extract(issue)
    db.upsert_case_fact(fact)


class CaseSearchTests(unittest.TestCase):
    def setUp(self):
        self.db = RedmineKnowledgeDB(Path(tempfile.mktemp(suffix=".sqlite3")))
        for issue_id, subject, status in [
            (633454, "[RK3576] BTS issue system signed with VBMeta test key", "Confirmed"),
            (635211, "RK3576 Android16 BTS扫描system分区VBMeta test key", "Confirmed"),
            (636612, "RK3576 Android16 BTS扫描system分区VBMeta test key", "Confirmed"),
            (635224, "RK3576 Android16 VtsHalPowerTargetTest PowerAidl#hasFixedPerformance", "Closed"),
        ]:
            _seed_vbmeta(self.db, issue_id, subject, status)
        self.search = RedmineCaseSearch(self.db)

    def test_vbmeta_text_hits_vbmeta_facts(self):
        results = self.search.search_similar("system signed with VBMeta test key", limit=5)
        issue_ids = {r["issue_id"] for r in results}
        self.assertIn(633454, issue_ids)
        self.assertIn(635211, issue_ids)
        self.assertIn(636612, issue_ids)
        # The non-VBMeta power issue must not outrank them.
        vbmeta_results = [r for r in results if r["error_signature"] == "VBMeta test key"]
        self.assertTrue(vbmeta_results)
        top = results[0]
        self.assertEqual(top["error_signature"], "VBMeta test key")
        self.assertGreater(top["score"], 60)

    def test_signature_match_scores_high(self):
        results = self.search.search_similar("VBMeta test key", limit=5)
        for r in results:
            if r["error_signature"] == "VBMeta test key":
                self.assertEqual(r["similarity_level"], "high")
                return
        self.fail("no VBMeta candidate")

    def test_exclude_issue_id(self):
        results = self.search.search_similar("VBMeta test key", limit=10, exclude_issue_id=633454)
        self.assertNotIn(633454, {r["issue_id"] for r in results})

    def test_issue_row_probe_matches_own_signature(self):
        issue = {
            "issue_id": 637372,
            "subject": "RK3576 Android16 EDLA vbmeta signed with VBMeta test key",
            "description": "EDLA failed: vbmeta test key",
            "status_name": "New",
            "fixed_version": "ANDROID16",
        }
        results = self.search.search_similar(issue, limit=5)
        self.assertTrue(any(r["issue_id"] == 633454 for r in results))

    def test_empty_query_returns_nothing(self):
        self.assertEqual(self.search.search_similar("", limit=5), [])


if __name__ == "__main__":
    unittest.main()
