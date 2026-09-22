"""kkagent stream-json 轨迹 + 输出解析测试。"""

from __future__ import annotations

import json
import unittest

from features.redmine.kkagent.output import (
    envelope_result,
    extract_json,
    json_from_message,
    parse_issue_result,
)
from features.redmine.kkagent.trace import KkAgentTrace, consume_line


def _trace_with_events(events: list[dict]) -> KkAgentTrace:
    trace = KkAgentTrace()
    for event in events:
        consume_line(trace, json.dumps(event))
    return trace


class TraceConsumeTests(unittest.TestCase):
    def test_session_and_system_events_are_captured(self):
        trace = _trace_with_events([
            {"type": "system", "version": "0.4.3"},
            {"type": "session", "session_id": "abc"},
        ])
        self.assertEqual(trace.kkagent_version, "0.4.3")
        self.assertEqual(trace.session_id, "abc")

    def test_tool_result_binds_to_call_and_hashes_output(self):
        import hashlib

        trace = _trace_with_events([
            {"type": "tool_call", "tool_call_id": "c1",
             "tool_name": "gms_rt_redmine_issue_fetch", "input": {"issue_id": 1}},
            {"type": "tool_result", "tool_call_id": "c1", "is_error": False,
             "output": "issue body"},
        ])
        self.assertEqual(len(trace.tool_calls), 1)
        call = trace.tool_calls[0]
        self.assertEqual(call.tool_name, "gms_rt_redmine_issue_fetch")
        self.assertEqual(call.status, "succeeded")
        self.assertEqual(call.output_sha256, hashlib.sha256(b"issue body").hexdigest())
        self.assertEqual(call.output_bytes, len(b"issue body"))
        self.assertEqual(call.output_preview, "issue body")

    def test_history_search_count_only_counts_successful_calls(self):
        trace = _trace_with_events([
            {"type": "tool_call", "tool_call_id": "c1",
             "tool_name": "gms_rt_redmine_history_search", "input": {}},
            {"type": "tool_result", "tool_call_id": "c1", "is_error": True, "output": "x"},
            {"type": "tool_call", "tool_call_id": "c2",
             "tool_name": "gms_rt_redmine_history_search", "input": {}},
            {"type": "tool_result", "tool_call_id": "c2", "is_error": False, "output": "y"},
        ])
        self.assertEqual(trace.history_search_count, 1)

    def test_pending_tool_call_is_not_successful(self):
        trace = _trace_with_events([
            {"type": "tool_call", "tool_call_id": "c1",
             "tool_name": "gms_rt_redmine_issue_fetch", "input": {"issue_id": 1}},
        ])
        self.assertEqual(trace.tool_calls[0].status, "pending")
        self.assertEqual(trace.successful_tool_names(), [])

    def test_history_queries_are_normalized_and_deduplicated(self):
        trace = _trace_with_events([
            {"type": "tool_call", "tool_call_id": "c1",
             "tool_name": "gms_rt_redmine_history_search", "input": {"q": " VTS  LTP "}},
            {"type": "tool_result", "tool_call_id": "c1", "is_error": False, "output": "{}"},
            {"type": "tool_call", "tool_call_id": "c2",
             "tool_name": "gms_rt_redmine_history_search", "input": {"query": "vts ltp"}},
            {"type": "tool_result", "tool_call_id": "c2", "is_error": False, "output": "{}"},
            {"type": "tool_call", "tool_call_id": "c3",
             "tool_name": "gms_rt_redmine_history_search", "input": {"q": "Android16 LTP"}},
            {"type": "tool_result", "tool_call_id": "c3", "is_error": False, "output": "{}"},
        ])
        self.assertEqual(trace.history_search_count, 3)
        self.assertEqual(trace.distinct_history_search_count, 2)

    def test_structured_history_result_records_issue_ids(self):
        output = json.dumps({"items": [{"issue_id": 646504}, {"issue_id": "646505"}]})
        trace = _trace_with_events([
            {"type": "tool_call", "tool_call_id": "c1",
             "tool_name": "gms_rt_redmine_history_search", "input": {"q": "widevine"}},
            {"type": "tool_result", "tool_call_id": "c1", "is_error": False, "output": output},
        ])
        self.assertEqual(trace.evidenced_issue_ids(), {646504, 646505})

    def test_truncated_tool_input_keeps_identity_keys(self):
        # 截断不允许吞掉 issue_id/artifact_id 等身份字段。
        big_payload = {"description": "x" * 4000, "issue_id": 123,
                       "artifact_id": "att-9", "path": "frameworks/base"}
        trace = _trace_with_events([
            {"type": "tool_call", "tool_call_id": "c1",
             "tool_name": "gms_rt_redmine_artifact_read", "input": big_payload},
        ])
        tool_input = trace.tool_calls[0].tool_input
        self.assertTrue(tool_input["_truncated"])
        self.assertEqual(tool_input["issue_id"], 123)
        self.assertEqual(tool_input["artifact_id"], "att-9")
        self.assertEqual(tool_input["path"], "frameworks/base")
        self.assertLessEqual(len(tool_input["_truncated_json"]), 500)

    def test_result_usage_overrides_summed_events(self):
        trace = _trace_with_events([
            {"type": "usage", "usage": {"input_tokens": 100, "output_tokens": 10}},
            {"type": "result", "subtype": "success", "exit_code": 0,
             "usage": {"input_tokens": 150, "output_tokens": 20,
                       "cache_read_input_tokens": 5}},
        ])
        self.assertEqual(trace.input_tokens, 150)
        self.assertEqual(trace.output_tokens, 20)
        self.assertEqual(trace.cache_read_tokens, 5)
        self.assertEqual(trace.subtype, "success")

    def test_llm_retry_and_error_counters(self):
        trace = _trace_with_events([
            {"type": "llm_retry"},
            {"type": "llm_retry"},
            {"type": "error", "message": "boom"},
        ])
        self.assertEqual(trace.llm_retries, 2)
        self.assertEqual(trace.errors, ["boom"])

    def test_non_json_line_returns_false(self):
        trace = KkAgentTrace()
        self.assertFalse(consume_line(trace, "not json at all"))
        self.assertTrue(consume_line(trace, "   \n"))


class OutputParserTests(unittest.TestCase):
    def _valid(self) -> dict:
        return {
            "problem_summary": "x", "customer_request": "y",
            "recommended_actions": [], "suggested_solution": "s",
            "detailed_report": "## 一、问题概况", "evidence": [],
            "similar_issues": [], "history_checked": True, "confidence": 0.8,
            "risk": "medium",
            "current_blocker": "", "root_cause": "", "root_cause_type": "unknown",
            "missing_information": [], "suggested_reply_en": "", "suggested_reply_zh": "",
        }

    def test_extract_json_last_line(self):
        raw = 'noise\n{"a": 1}\n{"b": 2}'
        self.assertEqual(extract_json(raw), {"b": 2})

    def test_json_from_message_variants(self):
        valid = json.dumps(self._valid(), ensure_ascii=False)
        self.assertIsNotNone(json_from_message(valid))
        self.assertIsNotNone(json_from_message("```json\n" + valid + "\n```"))
        self.assertIsNotNone(json_from_message("说明。\n\n```json\n" + valid + "\n```"))
        self.assertIsNone(json_from_message("plain prose"))

    def test_envelope_message_json(self):
        envelope = {"type": "result", "subtype": "success",
                    "message": json.dumps(self._valid())}
        result = envelope_result(envelope)
        self.assertEqual(result["problem_summary"], "x")

    def test_parse_issue_result_schema_error_prefix(self):
        parsed, errors = parse_issue_result(
            raw=json.dumps({"message": json.dumps({"problem_summary": "x"})})
        )
        self.assertIsNone(parsed)
        self.assertTrue(errors[0].startswith("schema validation failed"))

    def test_parse_issue_result_invalid_json(self):
        parsed, errors = parse_issue_result(raw="garbage")
        self.assertIsNone(parsed)
        self.assertEqual(errors, ["kkagent output is not valid JSON"])

    def test_parse_issue_result_success(self):
        parsed, errors = parse_issue_result(
            raw=json.dumps({"result": self._valid()})
        )
        self.assertEqual(errors, [])
        self.assertEqual(parsed["problem_summary"], "x")


if __name__ == "__main__":
    unittest.main()
