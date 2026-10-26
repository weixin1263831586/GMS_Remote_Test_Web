"""Daily Brief 模型、Token 与 GMS 工具统计测试。"""

from __future__ import annotations

import unittest
from typing import Any

from features.redmine.daily_brief_report import (
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
        # 三次调用都没记录参数指纹，无法证明同参重复，不得再按纯次数报警。
        self.assertEqual(recommendations["gms_rt_redmine_journals"], "reliability")
        self.assertNotIn("gms_rt_redmine_issue_fetch", recommendations)

    def test_uses_honest_default_model_label_when_none_was_persisted(self):
        statistics = summarize_execution_statistics([{"issue_id": 101}])

        self.assertEqual(statistics["models"][0]["model_name"], DEFAULT_MODEL_LABEL)

    def test_counts_real_mcp_calls_and_excludes_controller_preflight_probes(self):
        """kkagent 轨迹里真实调用是 mcp__gms__gms_rt_* 命名；Controller 预检
        探针（tool_call_id=preflight:*）不是 agent 调用，不得计入或顶替真实
        用量（否则统计表永远只显示 3 次探针）。"""
        statistics = summarize_execution_statistics(
            [
                {
                    "issue_id": 653167,
                    "tools": [
                        {
                            "tool_name": "gms_rt_redmine_issue_fetch",
                            "tool_call_id": "preflight:gms_rt_redmine_issue_fetch:abc",
                            "status": "succeeded",
                            "output_sha256": "preflight",
                        },
                        {
                            "tool_name": "mcp__gms__gms_rt_redmine_issue_fetch",
                            "tool_call_id": "call_1",
                            "status": "succeeded",
                            "output_sha256": "a",
                        },
                        {
                            "tool_name": "mcp__gms__gms_rt_redmine_journals",
                            "tool_call_id": "call_2",
                            "status": "succeeded",
                            "output_sha256": "b",
                        },
                        {
                            "tool_name": "mcp__gms__gms_rt_redmine_journals",
                            "tool_call_id": "call_3",
                            "status": "failed",
                            "is_error": True,
                        },
                        {"tool_name": "TodoList", "tool_call_id": "call_4", "status": "succeeded"},
                    ],
                },
            ],
            fallback_model="configured-model",
        )

        tools = {row["tool_name"]: row for row in statistics["gms_tools"]}
        self.assertEqual(statistics["gms_tool_call_count"], 3)
        self.assertEqual(tools["gms_rt_redmine_issue_fetch"]["call_count"], 1)
        self.assertEqual(tools["gms_rt_redmine_journals"]["call_count"], 2)
        self.assertEqual(tools["gms_rt_redmine_journals"]["failed_count"], 1)
        self.assertNotIn("TodoList", tools)

    def test_distinct_argument_calls_are_not_flagged_as_duplicates(self):
        """history_search 的初查 + 补充检索、sdk_read 的多个源码窗口都是
        提示词强制的不同参数取证；旧启发式按纯次数报警是常态误报。"""
        statistics = summarize_execution_statistics(
            [
                {
                    "issue_id": 655644,
                    "tools": [
                        {
                            "tool_name": "gms_rt_redmine_history_search",
                            "status": "succeeded",
                            "output_sha256": "a",
                            "tool_input": {"q": "RK3562 Android16 STS"},
                        },
                        {
                            "tool_name": "gms_rt_redmine_history_search",
                            "status": "succeeded",
                            "output_sha256": "b",
                            "tool_input": {"q": "system_server people NPE"},
                        },
                        {
                            "tool_name": "gms_rt_redmine_history_search",
                            "status": "succeeded",
                            "output_sha256": "c",
                            "tool_input": {"q": "IntentFilter getDataType"},
                        },
                        {
                            "tool_name": "gms_rt_sdk_read",
                            "status": "succeeded",
                            "output_sha256": "d",
                            "tool_input": {"result_id": "window-1"},
                        },
                        {
                            "tool_name": "gms_rt_sdk_read",
                            "status": "succeeded",
                            "output_sha256": "e",
                            "tool_input": {"result_id": "window-2"},
                        },
                        {
                            "tool_name": "gms_rt_sdk_read",
                            "status": "succeeded",
                            "output_sha256": "f",
                            "tool_input": {"result_id": "window-3"},
                        },
                    ],
                },
            ],
            fallback_model="configured-model",
        )

        self.assertEqual(statistics["gms_tool_call_count"], 6)
        self.assertEqual(statistics["tool_improvement_recommendations"], [])

    def test_flags_repeated_identical_argument_calls_but_not_polling(self):
        """同参数重复调用才构成浪费；轮询类工具同参反复调用是等终态的
        设计内行为，不得报警。"""
        statistics = summarize_execution_statistics(
            [
                {
                    "issue_id": 655644,
                    "tools": [
                        {
                            "tool_name": "gms_rt_redmine_history_search",
                            "status": "succeeded",
                            "output_sha256": "a",
                            "tool_input": {"q": "same query"},
                        },
                        {
                            "tool_name": "gms_rt_redmine_history_search",
                            "status": "succeeded",
                            "output_sha256": "b",
                            "tool_input": {"q": "same query"},
                        },
                        {
                            "tool_name": "gms_rt_redmine_history_search",
                            "status": "succeeded",
                            "output_sha256": "c",
                            "tool_input": {"q": "same query"},
                        },
                        {
                            "tool_name": "gms_rt_apk_status",
                            "status": "succeeded",
                            "output_sha256": "d",
                            "tool_input": {"task_id": "t1"},
                        },
                        {
                            "tool_name": "gms_rt_apk_status",
                            "status": "succeeded",
                            "output_sha256": "e",
                            "tool_input": {"task_id": "t1"},
                        },
                        {
                            "tool_name": "gms_rt_apk_status",
                            "status": "succeeded",
                            "output_sha256": "f",
                            "tool_input": {"task_id": "t1"},
                        },
                    ],
                },
            ],
            fallback_model="configured-model",
        )

        recommendations = statistics["tool_improvement_recommendations"]
        self.assertEqual(
            [row["tool_name"] for row in recommendations],
            ["gms_rt_redmine_history_search"],
        )
        self.assertEqual(recommendations[0]["kind"], "efficiency")
        self.assertEqual(recommendations[0]["duplicate_call_count"], 2)
        self.assertIn("2 次为同参数重复", recommendations[0]["message"])

    def test_same_arguments_across_issues_are_not_duplicates(self):
        """每个单号的分析是独立会话、各自完成必做取证；跨单号同参不判重复。"""
        def one_call(sha: str) -> dict[str, Any]:
            return {
                "tool_name": "gms_rt_redmine_history_search",
                "status": "succeeded",
                "output_sha256": sha,
                "tool_input": {"q": "shared query"},
            }
        statistics = summarize_execution_statistics(
            [
                {"issue_id": 101, "tools": [one_call("a")]},
                {"issue_id": 102, "tools": [one_call("b")]},
            ],
            fallback_model="configured-model",
        )

        tools_by_name = {
            row["tool_name"]: row for row in statistics["gms_tools"]
        }
        self.assertEqual(tools_by_name["gms_rt_redmine_history_search"]["call_count"], 2)
        self.assertEqual(tools_by_name["gms_rt_redmine_history_search"]["duplicate_call_count"], 0)
        self.assertEqual(statistics["tool_improvement_recommendations"], [])

    def test_tool_input_fingerprint_is_never_returned_to_clients(self):
        """tool_input 只用于重复指纹，任何返回行都不得带参数原文。"""
        statistics = summarize_execution_statistics(
            [
                {
                    "issue_id": 101,
                    "tools": [
                        {
                            "tool_name": "gms_rt_redmine_history_search",
                            "status": "succeeded",
                            "output_sha256": "a",
                            "tool_input": {"q": "same query"},
                        },
                        {
                            "tool_name": "gms_rt_redmine_history_search",
                            "status": "succeeded",
                            "output_sha256": "b",
                            "tool_input": {"q": "same query"},
                        },
                    ],
                },
            ],
            fallback_model="configured-model",
        )

        row = statistics["gms_tools"][0]
        self.assertEqual(row["duplicate_call_count"], 1)
        self.assertNotIn("tool_input", row)
        self.assertNotIn("arg_signatures", row)
        self.assertEqual(row["call_count"], 2)


if __name__ == "__main__":
    unittest.main()
