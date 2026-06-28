"""Tests for InternalIssueCreator."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from features.redmine.case_extractor import RedmineCaseExtractor
from features.redmine.internal_issue_creator import InternalIssueCreator
from features.redmine.knowledge_repository import RedmineKnowledgeDB


class _FakeIssue:
    def __init__(self, issue_id):
        self.id = issue_id


class _FakeClient:
    """Async fake — mirrors the real RedmineClient.create_issue signature."""

    def __init__(self, issue_id=8001):
        self.issue_id = issue_id
        self.last_kwargs = None
        self.closed = False

    async def create_issue(self, project_id, subject, **kwargs):
        self.last_kwargs = {"project_id": project_id, "subject": subject, **kwargs}
        return _FakeIssue(self.issue_id)

    async def close(self):
        self.closed = True


class InternalIssueCreatorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.db = RedmineKnowledgeDB(Path(tempfile.mktemp(suffix=".sqlite3")))
        fact = RedmineCaseExtractor.extract({
            "issue_id": 633454,
            "subject": "[RK3576] BTS VBMeta test key",
            "description": "vbmeta test key",
            "status_name": "Confirmed",
            "fixed_version": "RK3576_ANDROID16",
        })
        self.db.upsert_case_fact(fact)

    async def test_requires_confirmation(self):
        creator = InternalIssueCreator(self.db, client=_FakeClient())
        result = await creator.from_issue(633454, payload={"project_id": "fae"}, confirmed=False)
        self.assertFalse(result["success"])
        self.assertIn("confirmation required", result["error"])
        self.assertIn("description", result)
        self.assertIn("h1. 问题摘要", result["description"])

    async def test_disabled_when_not_allowed(self):
        creator = InternalIssueCreator(self.db, client=_FakeClient(), allow_create=False)
        result = await creator.from_issue(633454, payload={"project_id": "fae"}, confirmed=True)
        self.assertFalse(result["success"])
        self.assertIn("disabled", result["error"])

    async def test_payload_correct_and_link_written(self):
        client = _FakeClient(issue_id=8002)
        creator = InternalIssueCreator(self.db, client=client)
        result = await creator.from_issue(633454, payload={
            "project_id": "fae",
            "tracker_id": 1,
            "priority_id": 2,
            "assigned_to_id": 123,
            "created_by": "黄超群",
        }, confirmed=True)
        self.assertTrue(result["success"])
        # The awaited coroutine must resolve to a real issue id, not 0.
        self.assertEqual(result["internal_issue_id"], 8002)
        self.assertEqual(client.last_kwargs["project_id"], "fae")
        self.assertEqual(client.last_kwargs["assigned_to_id"], 123)
        rows = self.db.connect().execute("SELECT * FROM redmine_internal_issue_links").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(dict(rows[0])["internal_issue_id"], 8002)
        self.assertEqual(dict(rows[0])["source_issue_id"], 633454)

    async def test_no_client_returns_payload_only(self):
        creator = InternalIssueCreator(self.db, client=None)
        result = await creator.from_issue(633454, payload={"project_id": "fae"}, confirmed=True)
        self.assertFalse(result["success"])
        self.assertIn("client not configured", result["error"])
        self.assertEqual(result["payload"]["project_id"], "fae")


if __name__ == "__main__":
    unittest.main()
