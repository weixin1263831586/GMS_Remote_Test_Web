"""Daily Brief 模型、Token 与 GMS 工具统计测试。"""

from __future__ import annotations

import unittest

from features.redmine.daily_brief_execution_statistics import (
    DEFAULT_MODEL_LABEL,
    summarize_execution_statistics,
)


class DailyBriefExecutionStatisticsTests(unittest.TestCase):
    def test_summarizes_models_tokens_and_gms_tools_without_tool_payloads(self):
        statistics = summarize_execution_statistics(
            [
                {
                    "issue_id": 101,
                    "model_name": "glm-4.7",
                    "input_tokens": 120,
                    "output_tokens": 30,
                    "wall_duration_ms": 1_200,
                    "cache_read_tokens": 40,
                    "cache_creation_tokens": 5,
                    "tools": [
                        {
                            "tool_name": "gms_rt_redmine_issue_fetch",
                            "status": "succeeded",
                            "output_sha256": "a",
                            "input": {"would": "not be returned"},
                        },
                        {
                            "tool_name": "unrelated_tool",
                            "status": "succeeded",
                        },
                    ],
                },
                {
                    "issue_id": 101,
                    "model_name": "glm-4.7",
                    "input_tokens": 80,
                    "output_tokens": 20,
                    "wall_duration_ms": 800,
                    "tools": [
                        {
                            "tool_name": "gms_rt_redmine_issue_fetch",
                            "status": "succeeded",
                            "output_sha256": "b",
                        },
                        {
                            "tool_name": "gms_rt_redmine_issue_fetch",
                            "status": "succeeded",
                            "output_sha256": "c",
                        },
                    ],
                },
                {
                    "issue_id": 102,
                    "input_tokens": 10,
                    "output_tokens": 5,
                    "duration_ms": 2_000,
                    "tools": [
                        {
                            "tool_name": "gms_rt_redmine_journals",
                            "status": "failed",
                            "is_error": True,
                        },
                    ],
                },
            ],
            fallback_model="configured-model",
        )

        self.assertEqual(statistics["execution_count"], 3)
        self.assertEqual(statistics["issue_count"], 2)
        self.assertEqual(statistics["tokens"], {
            "input_tokens": 210,
            "output_tokens": 55,
            "cache_read_tokens": 40,
            "cache_creation_tokens": 5,
            "total_tokens": 265,
        })
        self.assertEqual(statistics["gms_tool_call_count"], 4)
        self.assertEqual(statistics["timing"], {
            "total_duration_ms": 4_000,
            "average_duration_ms": 1_333,
            "measured_execution_count": 3,
        })
        self.assertEqual(statistics["models"], [
            {
                "model_name": "glm-4.7",
                "execution_count": 2,
                "input_tokens": 200,
                "output_tokens": 50,
                "cache_read_tokens": 40,
                "cache_creation_tokens": 5,
                "issue_count": 1,
                "total_tokens": 250,
            },
            {
                "model_name": "configured-model",
                "execution_count": 1,
                "input_tokens": 10,
                "output_tokens": 5,
                "cache_read_tokens": 0,
                "cache_creation_tokens": 0,
                "issue_count": 1,
                "total_tokens": 15,
            },
        ])
        tools = {row["tool_name"]: row for row in statistics["gms_tools"]}
        self.assertEqual(tools["gms_rt_redmine_issue_fetch"]["call_count"], 3)
        self.assertEqual(tools["gms_rt_redmine_issue_fetch"]["succeeded_count"], 3)
        self.assertEqual(tools["gms_rt_redmine_journals"]["failed_count"], 1)
        self.assertNotIn("input", tools["gms_rt_redmine_issue_fetch"])
        recommendations = {
            row["tool_name"]: row["kind"]
            for row in statistics["tool_improvement_recommendations"]
        }
        self.assertEqual(recommendations["gms_rt_redmine_issue_fetch"], "efficiency")
        self.assertEqual(recommendations["gms_rt_redmine_journals"], "reliability")

    def test_uses_honest_default_model_label_when_none_was_persisted(self):
        statistics = summarize_execution_statistics([{"issue_id": 101}])

        self.assertEqual(statistics["models"][0]["model_name"], DEFAULT_MODEL_LABEL)


if __name__ == "__main__":
    unittest.main()
