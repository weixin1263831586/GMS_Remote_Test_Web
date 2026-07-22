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
