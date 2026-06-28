"""Tests for ticket-centered Redmine knowledge workbench payloads."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from features.redmine.knowledge_repository import RedmineKnowledgeDB
from features.redmine.knowledge_service import RedmineKnowledgeService


class _IssueRepo:
    def __init__(self, issue):
        self.issue = issue

    def get_issue(self, issue_id):
        return self.issue if int(issue_id) == int(self.issue["issue_id"]) else None


class KnowledgeWorkbenchTests(unittest.TestCase):
    def test_issue_workbench_exposes_replies_and_attachment_evidence(self):
        db = RedmineKnowledgeDB(Path(tempfile.mktemp(suffix=".sqlite3")))
        issue = {
            "issue_id": 633454,
            "subject": "[RK3576] BTS VBMeta test key",
            "description": "system signed with VBMeta test key",
            "status_name": "Confirmed",
            "fixed_version": "RK3576_ANDROID16",
            "journals_json": [
                {"id": "1", "user": "FAE", "created_on": "2026-06-01T10:00:00", "notes": "请使用 production key 重新签名。", "details": []}
            ],
            "attachments_json": [
                {
                    "attachment_id": 1,
                    "filename": "bts.png",
                    "status": "done",
                    "analysis_json": {
                        "parsed": True,
                        "details": {"type": "image", "detected_errors": ["VBMeta test key"], "certification_type": "BTS"},
                        "text_excerpt": "publicly known VBMeta test key",
                        "failures": [{"name": "VBMeta test key", "module": "AVB/VBMeta", "reason": "system partition test key"}],
                    },
                }
            ],
            "failures_json": [],
        }

        service = RedmineKnowledgeService(knowledge_db=db, issue_repository=_IssueRepo(issue))
        payload = service.issue_workbench(633454)

        self.assertEqual(payload["fact"]["error_signature"], "VBMeta test key")
        self.assertEqual(payload["evidence"]["reply_summary"][0]["notes"], "请使用 production key 重新签名。")
        self.assertEqual(payload["evidence"]["attachment_summary"][0]["detected_errors"], ["VBMeta test key"])
        self.assertIn("root_cause", payload["gms_like_sections"])
        self.assertEqual(payload["gms_like_sections"]["source_issue_ids"], [633454])


if __name__ == "__main__":
    unittest.main()
