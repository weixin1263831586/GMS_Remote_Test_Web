"""Daily Brief 单元测试：模型校验、快照合并/指纹/增量。"""

from __future__ import annotations

import unittest

from features.redmine.daily_brief_models import (
    RUN_STATUSES,
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
            "detailed_report": "## 一、问题概况\n\n| 项目 | 内容 |\n|---|---|\n| Issue | #1 |",
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

    def test_detailed_report_type_is_checked(self):
        result = self._valid()
        result["detailed_report"] = {"not": "a string"}
        errors = validate_issue_result(result)
        self.assertTrue(any("detailed_report" in e for e in errors))

    def test_invalid_confidence_and_enums(self):
        result = self._valid()
        result["confidence"] = 1.7
        result["root_cause_type"] = "maybe"
        result["risk"] = "extreme"
        errors = validate_issue_result(result)
        self.assertTrue(any("confidence" in e for e in errors))
        self.assertTrue(any("root_cause_type" in e for e in errors))
        self.assertTrue(any("risk" in e for e in errors))

    def test_missing_confidence_is_derived_from_root_cause_type(self):
        result = self._valid()
        result.pop("confidence")
        errors = [error for error in validate_issue_result(result) if "confidence" in error]
        self.assertEqual(errors, [])
        self.assertEqual(result["confidence"], 0.75)

    def test_missing_confidence_without_root_cause_type_still_fails(self):
        result = self._valid()
        result.pop("confidence")
        result.pop("root_cause_type")
        errors = [error for error in validate_issue_result(result) if "confidence" in error]
        self.assertEqual(errors, ["missing field: confidence"])

    def test_enum_word_confidence_is_normalized_not_rejected(self):
        """生产实证（issue 646220）：模型把 root_cause_type 的枚举词
        复制进 confidence（"confidence":"likely"），修复轮仍可能再错。
        枚举词是确定性可映射的，受控规范化后入库而不是烧尽修复预算。
        """
        result = self._valid()
        result["confidence"] = "likely"
        errors = validate_issue_result(result)
        self.assertEqual(errors, [])
        self.assertEqual(result["confidence"], 0.75)
        self.assertFalse(result.get("needs_human_review"))

    def test_confidence_word_map_boundaries(self):
        result = self._valid()
        for word, expected in (
            ("confirmed", 0.9), ("HIGH", 0.85), ("0.8", 0.8),
            ("possible", 0.55), ("unknown", 0.3),
        ):
            result["confidence"] = word
            self.assertEqual(validate_issue_result(result), [], word)
            self.assertEqual(result["confidence"], expected, word)

    def test_low_enum_confidence_triggers_human_review(self):
        result = self._valid()
        result["confidence"] = "unknown"  # 0.3 < 0.6 阈值
        self.assertEqual(validate_issue_result(result), [])
        self.assertTrue(result.get("needs_human_review"))

    def test_uninterpretable_confidence_still_rejected(self):
        result = self._valid()
        for bogus in ("very likely", ["likely"], {"v": 1}, True, 1.7, "-0.2"):
            result["confidence"] = bogus
            errors = validate_issue_result(dict(result))
            self.assertTrue(
                any("confidence" in e for e in errors), f"bogus={bogus!r}"
            )

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
    def test_cancelled_is_a_supported_terminal_status(self):
        self.assertIn("cancelled", RUN_STATUSES)

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
    """similar_issues schema 与 runtime-owned history_checked 边界。"""

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

    def test_history_checked_is_ignored_by_model_schema(self):
        """该字段由 runtime tool trace 覆写，模型缺失或乱填都不采信。"""
        for bogus in ("yes", "false", "no", 1, 0):
            result = self._valid()
            result["history_checked"] = bogus
            errors = validate_issue_result(result)
            self.assertFalse(any("history_checked" in e for e in errors))
        result = self._valid()
        result.pop("history_checked")
        self.assertEqual(validate_issue_result(result), [])
