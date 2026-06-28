"""Tests for ReplyDrafter."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from features.redmine.case_extractor import RedmineCaseExtractor
from features.redmine.knowledge_repository import RedmineKnowledgeDB
from features.redmine.mature_cases import MatureCaseBuilder
from features.redmine.reply_drafter import ReplyDrafter


def _seed(db, issue_id, subject, status="Confirmed"):
    fact = RedmineCaseExtractor.extract({
        "issue_id": issue_id,
        "subject": subject,
        "description": "system signed with VBMeta test key",
        "status_name": status,
        "fixed_version": "RK3576_ANDROID16",
    })
    db.upsert_case_fact(fact)


class ReplyDrafterTests(unittest.TestCase):
    def setUp(self):
        self.db = RedmineKnowledgeDB(Path(tempfile.mktemp(suffix=".sqlite3")))
        _seed(self.db, 633454, "[RK3576] BTS VBMeta test key")
        _seed(self.db, 635211, "RK3576 Android16 BTS VBMeta test key")
        case = MatureCaseBuilder(self.db).build_from_issues([633454, 635211])
        self.case_id = case["case_id"]

    def test_new_vbmeta_issue_matches_mature_case(self):
        drafter = ReplyDrafter(self.db)
        issue = {
            "issue_id": 637372,
            "subject": "RK3576 Android16 EDLA vbmeta signed with VBMeta test key",
            "description": "EDLA failed, vbmeta test key",
            "status_name": "New",
            "fixed_version": "ANDROID16",
        }
        result = drafter.draft_reply(issue)
        self.assertEqual(result["source"], "mature_case")
        self.assertEqual(result["mature_case_id"], self.case_id)
        self.assertIn("production", result["reply_draft"])
        # By default internal refs are hidden from the customer-facing body.
        self.assertIn("不对客户展示", result["reply_draft"])

    def test_no_mature_case_falls_back_to_similar(self):
        empty_db = RedmineKnowledgeDB(Path(tempfile.mktemp(suffix=".sqlite3")))
        _seed(empty_db, 633454, "RK3576 BTS VBMeta test key")
        drafter = ReplyDrafter(empty_db)
        issue = {
            "issue_id": 999,
            "subject": "RK3576 Android16 VBMeta test key again",
            "description": "vbmeta test key",
            "fixed_version": "ANDROID16",
        }
        result = drafter.draft_reply(issue)
        self.assertEqual(result["source"], "similar_issues")
        self.assertTrue(result["similar_issues"])

    def test_show_internal_refs_exposes_redmine_ids(self):
        drafter = ReplyDrafter(self.db, show_internal_refs=True)
        issue = {
            "issue_id": 637372,
            "subject": "RK3576 Android16 VBMeta test key",
            "description": "vbmeta test key",
            "fixed_version": "ANDROID16",
        }
        result = drafter.draft_reply(issue)
        self.assertIn("#633454", result["reply_draft"])

    def test_reply_does_not_depend_on_reference_outputs(self):
        # Seed a reference output; the reply must not use it.
        self.db.insert_reference_output(633454, "gms_assistant", {"markdown": "SECRET_REFERENCE_ANSWER_xyz"})
        drafter = ReplyDrafter(self.db)
        issue = {
            "issue_id": 637372,
            "subject": "RK3576 Android16 VBMeta test key",
            "description": "vbmeta test key",
            "fixed_version": "ANDROID16",
        }
        result = drafter.draft_reply(issue)
        self.assertNotIn("SECRET_REFERENCE_ANSWER_xyz", result["reply_draft"])


if __name__ == "__main__":
    unittest.main()
