"""Cross-project and resolved-only Redmine history regression contracts."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from features.redmine.client import RedmineClient
from features.redmine.history_search import search_issue_history


class CrossProjectSubjectSearchTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_search_does_not_restrict_to_fae(self):
        captured = []

        def search(**kwargs):
            captured.append(kwargs)
            return [SimpleNamespace(id=9, subject="Test failure",
                                    status=SimpleNamespace(name="Closed"))]

        client = object.__new__(RedmineClient)
        client._redmine = SimpleNamespace(issue=SimpleNamespace(filter=search))
        result = await client.search_issues_by_subject("Test failure")
        self.assertEqual(result[0]["issue_id"], 9)
        self.assertNotIn("project_id", captured[0])
        self.assertEqual(captured[0]["status_id"], "*")

        await client.search_issues_by_subject("Test failure", project_id="certification")
        self.assertEqual(captured[1]["project_id"], "certification")


class HistoryRemoteFilteringTests(unittest.IsolatedAsyncioTestCase):
    async def test_resolved_but_not_closed_remote_issue_remains_searchable(self):
        rows = [
            {"issue_id": 1, "status_name": "Resolved", "is_closed": False},
            {"issue_id": 2, "status_name": "New", "is_closed": False},
            {"issue_id": 3, "status_name": "Closed", "is_closed": True},
        ]

        async def search(_query, *, status_id, limit):
            return [row for row in rows if status_id == "*" or row["is_closed"]][:limit]

        client = SimpleNamespace(search_issues_by_subject=search, close=AsyncMock())
        repo = SimpleNamespace(search_history=lambda *a, **k: [],
                               corpus_token_stats=lambda tokens: {})
        service = SimpleNamespace(repository=repo, agent=SimpleNamespace(_make_client=lambda: client))
        result = await search_issue_history(service, "failure", limit=10, resolved_only=True)
        self.assertEqual([row["issue_id"] for row in result["items"]], [1, 3])

    async def test_resolved_only_filters_remote_results_and_expands_candidate_pool(self):
        remote = AsyncMock(return_value=[
            {"issue_id": 101, "subject": "duplicate", "status_name": "Closed"},
            {"issue_id": 102, "subject": "open", "status_name": "Feedback"},
            {"issue_id": 103, "subject": "resolved", "status_name": "Resolved"},
            {"issue_id": 104, "subject": "closed", "status_name": "已关闭"},
            {"issue_id": 105, "subject": "open2", "status_name": "New"},
        ])
        client = SimpleNamespace(search_issues_by_subject=remote, close=AsyncMock())
        repo = SimpleNamespace(
            search_history=lambda *args, **kw: [
                {"issue_id": 101, "subject": "local", "is_resolved": True},
            ],
            corpus_token_stats=lambda tokens: {},
        )
        service = SimpleNamespace(repository=repo, agent=SimpleNamespace(_make_client=lambda: client))
        result = await search_issue_history(service, "failure", limit=3, resolved_only=True)
        self.assertEqual([x["issue_id"] for x in result["items"]], [101, 103, 104])
        self.assertTrue(all(x["is_resolved"] for x in result["items"]))
        self.assertEqual(result["remote_search_error"], "")
        self.assertEqual(remote.await_args.kwargs["status_id"], "*")
        self.assertGreater(remote.await_args.kwargs["limit"], 2)
        client.close.assert_awaited_once()

    async def test_remote_failure_preserves_local_results_and_reports_degradation(self):
        remote = AsyncMock(side_effect=TimeoutError("remote site timeout"))
        client = SimpleNamespace(search_issues_by_subject=remote, close=AsyncMock())
        local = [{"issue_id": 8, "subject": "local", "is_resolved": True}]
        repo = SimpleNamespace(
            search_history=lambda *args, **kw: local.copy(),
            corpus_token_stats=lambda x: {},
        )
        result = await search_issue_history(
            SimpleNamespace(repository=repo, agent=SimpleNamespace(_make_client=lambda: client)),
            "failure", limit=5,
        )
        self.assertEqual([x["issue_id"] for x in result["items"]], [8])
        self.assertIn("timeout", result["remote_search_error"])
        client.close.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
