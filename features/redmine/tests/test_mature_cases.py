"""Tests for MatureCaseBuilder aggregation."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from features.redmine.case_extractor import RedmineCaseExtractor
from features.redmine.knowledge_repository import RedmineKnowledgeDB
from features.redmine.mature_cases import MatureCaseBuilder


def _seed(db, issue_id, subject, status, solution=""):
    fact = RedmineCaseExtractor.extract({
        "issue_id": issue_id,
        "subject": subject,
        "description": "system signed with VBMeta test key",
        "status_name": status,
        "fixed_version": "RK3576_ANDROID16",
        "solution": solution,
    })
    db.upsert_case_fact(fact)


def _seed_power(db, issue_id=598972):
    fact = RedmineCaseExtractor.extract({
        "issue_id": issue_id,
        "subject": "RK3576 Android16 VtsHalPowerTargetTest模块PowerAidl#hasFixedPerformance",
        "description": (
            "Power/PowerAidl#hasFixedPerformance/0_android_hardware_power_IPower_default FAILURE\n"
            "Value of: supported\n  Actual: false\nExpected: true"
        ),
        "status_name": "Closed",
        "fixed_version": "RK3576_ANDROID16",
    })
    db.upsert_case_fact(fact)


class MatureCaseBuilderTests(unittest.TestCase):
    def setUp(self):
        self.db = RedmineKnowledgeDB(Path(tempfile.mktemp(suffix=".sqlite3")))
        _seed(self.db, 633454, "[RK3576] BTS VBMeta test key", "Confirmed")
        _seed(self.db, 635211, "RK3576 Android16 BTS VBMeta test key", "Confirmed")
        _seed(self.db, 635211 + 1, "RK3576 Android16 BTS VBMeta test key (closed)", "Closed", solution="用production key重新签名")
        self.builder = MatureCaseBuilder(self.db)

    def test_build_aggregates_source_issues(self):
        case = self.builder.build_from_issues([633454, 635211, 635212])
        self.assertEqual(case["module"], "AVB/VBMeta")
        self.assertEqual(case["canonical_error_signature"], "VBMeta test key")
        self.assertEqual(case["chip_platform"], "RK3576")
        self.assertEqual(set(case["source_issue_ids_json"]), {633454, 635211, 635212})
        # Links written.
        links = self.db.list_links_for_case(case["case_id"])
        self.assertEqual({link["issue_id"] for link in links}, {633454, 635211, 635212})

    def test_closed_solution_preferred(self):
        case = self.builder.build_from_issues([633454, 635211, 635212])
        solution = case["solution_json"]
        self.assertIn("production", solution.get("overview", ""))
        # Steps parsed from numbered lines.
        self.assertTrue(solution.get("steps"))

    def test_approve_flow(self):
        case = self.builder.build_from_issues([633454, 635211])
        ok = self.db.approve_mature_case(case["case_id"], "黄超群")
        self.assertTrue(ok)
        refreshed = self.db.get_mature_case(case["case_id"])
        self.assertEqual(refreshed["status"], "approved")
        self.assertEqual(refreshed["approved_by"], "黄超群")

    def test_reply_template_marked_mature(self):
        case = self.builder.build_from_issues([633454, 635211, 635212])
        self.assertIn("成熟案例", case["reply_template"])
        self.assertIn("AVB/VBMeta", case["reply_template"])

    def test_power_hal_case_uses_hal_migration_rule(self):
        db = RedmineKnowledgeDB(Path(tempfile.mktemp(suffix=".sqlite3")))
        _seed_power(db)
        case = MatureCaseBuilder(db).build_from_issues([598972])
        self.assertEqual(case["canonical_error_signature"], "PowerAidl hasFixedPerformance unsupported")
        self.assertIn("Mode::FIXED_PERFORMANCE", case["root_cause"])
        self.assertIn("isModeSupported", case["solution_json"].get("overview", ""))
        self.assertIn("vendor HAL", case["rules_json"][0]["title"])


if __name__ == "__main__":
    unittest.main()
