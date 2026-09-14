"""Runtime Evidence Gate 测试：真实 tool trace 判定，不信模型自报。"""

from __future__ import annotations

import json
import unittest

from features.redmine.kkagent.evidence_gate import (
    MIN_HISTORY_SEARCHES,
    evaluate_evidence_gate,
    gate_and_errors,
    gate_errors,
)
from features.redmine.kkagent.trace import KkAgentTrace, consume_line


def _trace(*events: dict) -> KkAgentTrace:
    trace = KkAgentTrace()
    for event in events:
        consume_line(trace, json.dumps(event))
    return trace


def _full_trace(history_searches: int = 2, with_attachments: bool = True) -> KkAgentTrace:
    events: list[dict] = [
        {"type": "session", "session_id": "s1"},
        {"type": "tool_call", "tool_call_id": "c1",
         "tool_name": "gms_rt_redmine_issue_fetch", "input": {}},
        {"type": "tool_result", "tool_call_id": "c1", "is_error": False, "output": "i"},
        {"type": "tool_call", "tool_call_id": "c2",
         "tool_name": "gms_rt_redmine_journals", "input": {}},
        {"type": "tool_result", "tool_call_id": "c2", "is_error": False, "output": "j"},
    ]
    call_id = 10
    if with_attachments:
        events += [
            {"type": "tool_call", "tool_call_id": "c3",
             "tool_name": "gms_rt_redmine_attachments", "input": {}},
            {"type": "tool_result", "tool_call_id": "c3", "is_error": False,
             "output": json.dumps({"data": {"artifacts": [
                 {"artifact_id": "a1", "kind": "image", "status": "ready"},
                 {"artifact_id": "a2", "kind": "image", "status": "ready"},
                 {"artifact_id": "a3", "kind": "image", "status": "ready"},
             ]}})},
        ]
    for index in range(history_searches):
        events += [
            {"type": "tool_call", "tool_call_id": f"c{call_id}",
             "tool_name": "gms_rt_redmine_history_search", "input": {"q": f"query {index}"}},
            {"type": "tool_result", "tool_call_id": f"c{call_id}",
             "is_error": False,
             "output": json.dumps({"items": [{"issue_id": 646504 + index}]})},
        ]
        call_id += 1
    return _trace(*events)


class EvidenceGateTests(unittest.TestCase):
    def test_full_trace_passes_gate(self):
        trace = _full_trace()
        gate = evaluate_evidence_gate(trace, {"attachment_count": 2})
        self.assertEqual(gate_errors(gate), [])
        self.assertTrue(gate["history_checked"])
        self.assertEqual(gate["history_search_count"], 2)
        self.assertEqual(gate["distinct_history_search_count"], 2)
        self.assertEqual(gate["session_id"], "s1")

    def test_missing_history_search_fails(self):
        trace = _full_trace(history_searches=1)
        gate = evaluate_evidence_gate(trace, {"attachment_count": 0})
        errors = gate_errors(gate)
        self.assertTrue(any("only used 1 distinct" in e for e in errors))
        self.assertFalse(gate["history_checked"])

    def test_attachments_required_when_snapshot_has_attachments(self):
        trace = _full_trace(with_attachments=False)
        gate = evaluate_evidence_gate(trace, {"attachment_count": 3})
        self.assertTrue(any("attachments" in e for e in gate_errors(gate)))
        # 快照无附件时不得强求。
        gate2 = evaluate_evidence_gate(trace, {"attachment_count": 0})
        self.assertFalse(any("attachments" in e for e in gate_errors(gate2)))

    def test_gate_overwrites_model_claimed_history_checked(self):
        trace = _full_trace(history_searches=0)
        result = {"history_checked": True}  # 模型自报
        gate, errors = gate_and_errors(trace, {"attachment_count": 0}, result)
        self.assertFalse(result["history_checked"])  # 运行时覆写
        self.assertEqual(result["evidence_gate"], gate)
        self.assertTrue(errors)

    def test_each_ready_text_attachment_must_be_read(self):
        trace = _full_trace()
        attachment_call = next(
            call for call in trace.tool_calls if "redmine_attachments" in call.tool_name
        )
        attachment_call.text_artifact_ids = ["text-1"]
        gate = evaluate_evidence_gate(trace, {"attachment_count": 2})
        self.assertFalse(gate["attachments_checked"])
        self.assertTrue(any("text-1" in error for error in gate_errors(gate)))

        consume_line(trace, json.dumps({
            "type": "tool_call", "tool_call_id": "read-1",
            "tool_name": "gms_rt_redmine_artifact_read",
            "input": {"artifact_id": "text-1"},
        }))
        consume_line(trace, json.dumps({
            "type": "tool_result", "tool_call_id": "read-1",
            "is_error": False, "output": "contents",
        }))
        gate = evaluate_evidence_gate(trace, {"attachment_count": 2})
        self.assertTrue(gate["attachments_checked"])

    def test_min_history_searches_is_two(self):
        self.assertEqual(MIN_HISTORY_SEARCHES, 2)

    def test_duplicate_history_queries_do_not_pass(self):
        trace = _full_trace(history_searches=2)
        for call in trace.tool_calls:
            if "history_search" in call.tool_name:
                call.tool_input = {"q": "same query"}
        gate = evaluate_evidence_gate(trace, {"attachment_count": 0})
        self.assertEqual(gate["history_search_count"], 2)
        self.assertEqual(gate["distinct_history_search_count"], 1)
        self.assertTrue(gate_errors(gate))

    def test_similar_issue_must_come_from_successful_tool_evidence(self):
        trace = _full_trace()
        supported = {"similar_issues": [{"issue_id": 646504}]}
        _gate, errors = gate_and_errors(
            trace, {"issue_id": 1, "attachment_count": 0}, supported
        )
        self.assertEqual(errors, [])

        unsupported = {"similar_issues": [{"issue_id": 999999}]}
        _gate, errors = gate_and_errors(
            trace, {"issue_id": 1, "attachment_count": 0}, unsupported
        )
        self.assertTrue(any("999999" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
