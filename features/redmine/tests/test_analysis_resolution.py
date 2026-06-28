"""Tests for evidence-based Redmine display fallbacks."""

from __future__ import annotations

import unittest

from features.redmine.agent import RedmineAgent


class ResolutionFallbackTests(unittest.TestCase):
    def test_enrich_issue_uses_attachment_and_journal_evidence_without_guessing(self):
        issue = {
            "issue_id": 7001,
            "subject": "RK3576 Android16 VTS fail",
            "description": "客户反馈日志见附件，麻烦分析。",
            "error_info": "",
            "error_analysis": "暂无分析结果",
            "solution": "3. 待进一步分析确认解决方案。",
            "patch_direction": "",
            "failures_json": [],
            "references_json": [],
            "journals_json": [
                {"user": "FAE", "created_on": "2026-06-01T10:00:00", "notes": "请先确认附件 log 中的失败项。"}
            ],
            "attachments_json": [
                {
                    "filename": "run_log.txt",
                    "analysis_json": {
                        "parsed": True,
                        "details": {"type": "text", "detected_errors": ["Actual: false Expected: true"]},
                        "text_excerpt": "VtsHalPowerTargetTest FAILURE\nValue of: supported\n  Actual: false\nExpected: true",
                        "failures": [
                            {
                                "module": "VtsHalPowerTargetTest",
                                "name": "Power/PowerAidl#hasFixedPerformance",
                                "reason": "Actual: false Expected: true",
                            }
                        ],
                    },
                }
            ],
        }

        enriched = RedmineAgent.enrich_issue_display_fields(issue)

        self.assertIn("run_log.txt", enriched["error_info"])
        self.assertIn("Actual: false", enriched["error_info"])
        self.assertIn("附件证据: run_log.txt", enriched["error_analysis"])
        self.assertIn("历史回复: 1 条有内容回复可参考", enriched["error_analysis"])
        self.assertIn("未找到明确已验证解决方案", enriched["solution"])
        self.assertNotIn("待进一步分析确认解决方案", enriched["solution"])

    def test_enrich_issue_uses_verified_journal_resolution(self):
        issue = {
            "issue_id": 7002,
            "subject": "配置问题",
            "description": "报错见描述: Error: invalid config",
            "error_analysis": "",
            "solution": "",
            "patch_direction": "",
            "failures_json": [],
            "references_json": [],
            "journals_json": [
                {"user": "FAE", "created_on": "2026-06-01T10:00:00", "notes": "解决方案：修改 config.xml 中的配置。"},
                {"user": "客户", "created_on": "2026-06-01T11:00:00", "notes": "验证通过，可以关闭。"},
            ],
            "attachments_json": [],
        }

        enriched = RedmineAgent.enrich_issue_display_fields(issue)

        self.assertIn("已验证", enriched["solution"])
        self.assertIn("修改 config.xml", enriched["solution"])


if __name__ == "__main__":
    unittest.main()
