"""Daily Brief 单元测试：模型校验、快照合并/指纹/增量。"""

from __future__ import annotations

import unittest

from features.redmine.daily_brief_models import (
    DailyBriefIssue,
    DailyBriefRun,
    base_priority_score,
    priority_from_score,
    validate_issue_result,
)
from features.redmine.daily_brief_snapshot import (
    brief_date_today,
    detect_delta,
    issue_fingerprint,
)


class ValidIssueResultTests(unittest.TestCase):
    def _valid(self) -> dict:
        return {
            "problem_summary": "CTS security 失败",
            "customer_request": "客户要求定位失败原因",
            "recommended_actions": [{"step": 1, "action": "复查 logcat"}],
            "suggested_solution": "升级安全补丁后重跑",
            "evidence": [{"source": "journal", "reference": "#12", "fact": "…"}],
            "similar_issues": [],
            "history_checked": True,
            "confidence": 0.85,
            "root_cause_type": "likely",
            "risk": "medium",
        }

    def test_valid_result_passes(self):
        self.assertEqual(validate_issue_result(self._valid()), [])

    def test_missing_required_field_fails(self):
        result = self._valid()
        result.pop("suggested_solution")
        errors = validate_issue_result(result)
        self.assertTrue(any("suggested_solution" in e for e in errors))

    def test_invalid_confidence_and_enums(self):
        result = self._valid()
        result["confidence"] = 1.7
        result["root_cause_type"] = "maybe"
        result["risk"] = "extreme"
        errors = validate_issue_result(result)
        self.assertTrue(any("confidence" in e for e in errors))
        self.assertTrue(any("root_cause_type" in e for e in errors))
        self.assertTrue(any("risk" in e for e in errors))

    def test_low_confidence_forces_human_review(self):
        result = self._valid()
        result["confidence"] = 0.4
        validate_issue_result(result)
        self.assertTrue(result["needs_human_review"])


class PriorityRuleTests(unittest.TestCase):
    def test_stale_urgent_issue_scores_highest(self):
        entry = {
            "buckets": ["waiting_my_reply", "no_reply_3_days"],
            "priority_name": "Urgent",
            "unreplied_days": 4.0,
            "attachment_count": 3,
        }
        score = base_priority_score(entry)
        self.assertGreaterEqual(score, 70)
        self.assertEqual(priority_from_score(score), "P1")

    def test_waiting_only_low_priority(self):
        entry = {
            "buckets": ["waiting_my_reply"],
            "priority_name": "Normal",
            "unreplied_days": 0.0,
            "attachment_count": 0,
        }
        self.assertEqual(priority_from_score(base_priority_score(entry)), "P3")

    def test_score_is_deterministic(self):
        entry = {"buckets": ["no_reply_3_days"], "unreplied_days": 3.2, "attachment_count": 1}
        self.assertEqual(base_priority_score(entry), base_priority_score(dict(entry)))


class FingerprintTests(unittest.TestCase):
    def test_fingerprint_changes_with_content(self):
        base = {"issue_id": 9, "updated_on": "t1", "attachment_count": 1,
                "last_external_reply_at": "", "last_owner_reply_at": ""}
        self.assertNotEqual(issue_fingerprint(base), issue_fingerprint({**base, "updated_on": "t2"}))
        self.assertEqual(issue_fingerprint(base), issue_fingerprint(dict(base)))


class DetectDeltaTests(unittest.TestCase):
    def test_changed_added_removed(self):
        old = [{"issue_id": 1, "fingerprint": "a"}, {"issue_id": 2, "fingerprint": "b"}]
        new = [{"issue_id": 1, "fingerprint": "a2"}, {"issue_id": 3, "fingerprint": "c"}]
        delta = detect_delta(old, new)
        self.assertEqual([e["issue_id"] for e in delta["changed"]], [1, 3])
        self.assertEqual(delta["no_longer_pending"], [2])

    def test_unchanged_issues_are_skipped(self):
        old = [{"issue_id": 1, "fingerprint": "a"}]
        self.assertEqual(detect_delta(old, [dict(old[0])])["changed"], [])

    def test_missing_fingerprint_counts_as_changed(self):
        self.assertEqual(len(detect_delta([{"issue_id": 1}], [{"issue_id": 1}])["changed"]), 1)


class ModelRowTests(unittest.TestCase):
    def test_run_and_issue_rows_roundtrip_fields(self):
        run = DailyBriefRun(owner_id="u1", brief_date="2026-09-13", mode="nightly", run_id="db_x")
        row = run.to_row()
        self.assertEqual(row["owner_id"], "u1")
        self.assertEqual(row["report_json"], {})
        issue = DailyBriefIssue(run_id="db_x", issue_id=42, buckets=["waiting_my_reply"])
        self.assertEqual(issue.to_row()["buckets"], ["waiting_my_reply"])

    def test_brief_date_today_format(self):
        self.assertRegex(brief_date_today(), r"^\d{4}-\d{2}-\d{2}$")


if __name__ == "__main__":
    unittest.main()


class SimilarIssuesValidationTests(unittest.TestCase):
    """v4 schema：similar_issues / history_checked 校验。"""

    def _valid(self) -> dict:
        base = ValidIssueResultTests._valid(self)
        base["similar_issues"] = [
            {"issue_id": 646504, "subject": "RK3576 SSI merge", "similarity": "same",
             "reusable_fix": "同 SSI merge 流程可复用", "reference_fact": "结案 #33"},
        ]
        base["history_checked"] = True
        return base

    def test_valid_v4_result_passes(self):
        self.assertEqual(validate_issue_result(self._valid()), [])

    def test_missing_similar_issues_fails(self):
        result = ValidIssueResultTests._valid(self)
        result.pop("similar_issues")
        errors = validate_issue_result(result)
        self.assertTrue(any("similar_issues" in e for e in errors))
        result2 = ValidIssueResultTests._valid(self)
        result2.pop("history_checked")
        errors2 = validate_issue_result(result2)
        self.assertTrue(any("history_checked" in e for e in errors2))

    def test_invalid_similar_entry_cleaned_and_flagged(self):
        result = self._valid()
        result["similar_issues"] = [
            {"issue_id": "not-a-number", "similarity": "same"},
            {"issue_id": 123, "similarity": "bogus"},
        ]
        errors = validate_issue_result(result)
        self.assertTrue(any("issue_id" in e for e in errors))
        kept = result["similar_issues"]
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["similarity"], "related")  # 非法级别回落

    def test_history_checked_coerced_to_bool(self):
        result = self._valid()
        result["history_checked"] = "yes"
        validate_issue_result(result)
        self.assertIs(result["history_checked"], True)
