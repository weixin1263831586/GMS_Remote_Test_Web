"""Prevent broad same-module Redmine matches from masquerading as same failure."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from features.redmine.analysis.ai import IssueModelAnalysis
from features.redmine.analysis.similarity import IssueSimilarity


class Searcher(IssueSimilarity):
    def __init__(self):
        super().__init__(
            SimpleNamespace(search_similar=lambda *args, **kwargs: []),
            IssueModelAnalysis(None),
        )

    async def _ai_semantic_similarity(self, issue_payload, candidates):
        return {}


class SimilarityRankingTests(unittest.IsolatedAsyncioTestCase):
    async def test_exact_failure_is_prioritized_over_same_module_other_failure(self):
        issue = {
            "issue_id": 101,
            "subject": "CtsSecurityTestCases testFailsAcrossUsers FAILURE",
            "description": "AssertionError",
        }
        failures = [{
            "module": "CtsSecurityTestCases",
            "name": "testFailsAcrossUsers",
            "reason": "AssertionError differs",
        }]
        remote = AsyncMock(return_value=[
            {"issue_id": 201, "subject": "CtsSecurityTestCases unrelated test failure",
             "status_name": "Closed"},
            {"issue_id": 202,
             "subject": "CtsSecurityTestCases testFailsAcrossUsers AssertionError",
             "status_name": "Closed"},
        ])
        refs = await Searcher().find_similar_references(
            SimpleNamespace(search_issues_by_subject=remote), issue, failures
        )
        self.assertTrue(refs)
        self.assertEqual(refs[0]["issue_id"], 202)
        self.assertTrue(refs[0]["match_details"]["same_test_case"])
        self.assertEqual(refs[0]["match_details"]["redmine_direct_match_bonus"], 20)
        self.assertFalse(any(r["issue_id"] == 201 and
                             r["similarity_level"] == "high" for r in refs))
        # The search must include every accessible Redmine project.
        self.assertNotIn("project_id", remote.await_args.kwargs)


if __name__ == "__main__":
    unittest.main()
