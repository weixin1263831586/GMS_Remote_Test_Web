"""晨报结果 → case_fact 映射测试。"""

from __future__ import annotations

import unittest

from features.redmine.daily_brief_case_fact import build_case_fact_from_brief
from features.redmine.daily_brief_models import DailyBriefIssue, DailyBriefRun


def _run() -> DailyBriefRun:
    return DailyBriefRun(
        owner_id="o", brief_date="2026-09-15", mode="manual",
        run_id="db_test", status="completed",
    )


def _record(result: dict) -> DailyBriefIssue:
    return DailyBriefIssue(
        run_id="db_test", issue_id=646220,
        buckets=["no_reply_3_days"],
        subject="3572S-A16 VTS vts_ltp_test_arm_64 fail",
        status="completed", result=result,
    )


class BuildCaseFactTests(unittest.TestCase):
    def test_maps_core_fields(self):
        result = {
            "problem_summary": "VTS listmount04 fail\n第二行不进入",
            "root_cause": "内核 stable backport 改了错误码",
            "suggested_solution": "① 用 r4 套件补审批",
            "suggested_reply_zh": "您好，已定位……",
            "recommended_actions": [
                {"step": 1, "action": "确认设备"},
                {"step": 2, "action": "复现并对比"},
            ],
            "confidence": 0.85,
            "evidence": [{"source": "attachment", "reference": "x", "fact": "y"}],
            "similar_issues": [
                {"issue_id": 1, "subject": "类似工单A"},
                {"issue_id": 2, "subject": "类似工单B"},
            ],
        }
        fact = build_case_fact_from_brief(646220, _record(result), _run())
        self.assertEqual(fact["issue_id"], 646220)
        self.assertEqual(fact["subject"], "3572S-A16 VTS vts_ltp_test_arm_64 fail")
        self.assertTrue(fact["problem_summary"].startswith("VTS listmount04 fail"))
        self.assertNotIn("\n", fact["problem_summary"])
        self.assertEqual(fact["root_cause"], "内核 stable backport 改了错误码")
        self.assertIn("确认设备", fact["verification"])
        # AI 0-1 统一 ×100 入库（与提取器 0-100 同量纲）。
        self.assertEqual(fact["confidence"], 85.0)
        self.assertEqual(fact["source_quality"], "daily_brief_ai")
        self.assertEqual(len(fact["keywords"]), 2)
        # evidence 列统一包 dict（审核意见 P1：消费方按 dict 解引用）；
        # per-run provenance 落在 evidence 内，不冒充 error_signature。
        self.assertEqual(
            fact["evidence"],
            {
                "daily_brief_evidence": result["evidence"],
                "daily_brief_run": {
                    "brief_date": "2026-09-15",
                    "run_id": "db_test",
                },
            },
        )
        # 无真实签名时留空（merge 保留已有签名）；provenance 占位符
        # 会覆盖已有真实 error_signature 并破坏签名聚合（审核意见 P1）。
        self.assertEqual(fact["error_signature"], "")

    def test_minimal_result_does_not_crash(self):
        fact = build_case_fact_from_brief(
            1, _record({"confidence": 0.3}), _run())
        self.assertEqual(fact["issue_id"], 1)
        self.assertEqual(fact["root_cause"], "")
        self.assertEqual(fact["keywords"], [])
        self.assertEqual(fact["confidence"], 30.0)

    def test_execution_status_never_becomes_redmine_status(self):
        """AI 执行状态（completed）不得写进知识库 status_name。"""
        fact = build_case_fact_from_brief(
            1, _record({"confidence": 0.8, "problem_summary": "x"}), _run())
        self.assertEqual(fact["status_name"], "")
        self.assertNotEqual(fact["status_name"], "completed")

    def test_redmine_facts_come_from_extractor(self):
        """提供扫描库工单行时，Redmine 富字段来自 RedmineCaseExtractor。"""
        issue = {
            "issue_id": 646220,
            "subject": "RK3576 Android16 GTS fail",
            "status_name": "Feedback",
            "project_name": "Android TV",
            "assigned_to_name": "alice",
            "category": "GMS",
            "description": "RK3576 Android16 GTS failure",
        }
        result = {
            "problem_summary": "GTS 用例失败",
            "root_cause": "缺补丁",
            "suggested_solution": "打补丁",
            "confidence": 0.8,
        }
        fact = build_case_fact_from_brief(
            646220, _record(result), _run(), issue=issue)
        self.assertEqual(fact["status_name"], "Feedback")
        self.assertEqual(fact["project_name"], "Android TV")
        self.assertEqual(fact["assigned_to_name"], "alice")
        self.assertEqual(fact["chip_platform"], "RK3576")
        self.assertEqual(fact["android_version"], "Android16")
        # AI 结论覆盖结论性字段。
        self.assertEqual(fact["root_cause"], "缺补丁")


if __name__ == "__main__":
    unittest.main()
