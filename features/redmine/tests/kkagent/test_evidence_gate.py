"""Runtime Evidence Gate 测试：真实 tool trace 判定，不信模型自报。"""

from __future__ import annotations

import json
import unittest

from features.redmine.kkagent.evidence_gate import (
    MIN_HISTORY_SEARCHES,
    evaluate_evidence_gate,
    gate_and_errors,
    gate_errors,
    is_test_failure_subject,
)
from features.redmine.kkagent.trace import KkAgentTrace, ToolTrace, consume_line


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


class TestFailureSourceEvidenceGateTests(unittest.TestCase):
    """测试类失败必须做源码级取证（区分缺补丁 vs 上游行为变更）。"""

    @staticmethod
    def _entry() -> dict:
        return {
            "issue_id": 646220,
            "subject": "3572S-A16-normal版VTS的vts_ltp_test_arm_64",
            "attachment_count": 0,
        }

    def test_ltp_subject_is_detected_as_test_failure(self):
        self.assertTrue(is_test_failure_subject(self._entry()))
        self.assertFalse(is_test_failure_subject(
            {"subject": "RK3576 自动亮度失效"}
        ))
        self.assertFalse(is_test_failure_subject({}))

    def test_test_failure_without_source_evidence_fails_gate(self):
        trace = _full_trace()
        gate = evaluate_evidence_gate(trace, self._entry())
        self.assertTrue(gate["test_failure_subject"])
        self.assertEqual(gate["source_evidence_tool_count"], 0)
        self.assertFalse(gate["source_evidence_checked"])
        errors = gate_errors(gate)
        self.assertTrue(any("source-level evidence" in e for e in errors))

    def test_sdk_search_call_satisfies_source_gate(self):
        trace = _full_trace()
        trace.tool_calls.append(ToolTrace(
            tool_call_id="sdk1",
            tool_name="gms_rt_sdk_search",
            status="succeeded",
        ))
        gate = evaluate_evidence_gate(trace, self._entry())
        self.assertEqual(gate["source_evidence_tool_count"], 1)
        self.assertTrue(gate["source_evidence_checked"])
        self.assertFalse(any("source-level" in e for e in gate_errors(gate)))

    def test_failed_source_call_does_not_count(self):
        trace = _full_trace()
        trace.tool_calls.append(ToolTrace(
            tool_call_id="sdk1",
            tool_name="gms_rt_sdk_search",
            status="failed",
        ))
        gate = evaluate_evidence_gate(trace, self._entry())
        self.assertEqual(gate["source_evidence_tool_count"], 0)
        self.assertTrue(any("source-level" in e for e in gate_errors(gate)))

    def test_non_test_subject_ignores_source_gate(self):
        trace = _full_trace()
        gate = evaluate_evidence_gate(
            trace, {"issue_id": 1, "subject": "RK3576 亮度", "attachment_count": 0}
        )
        self.assertFalse(gate["test_failure_subject"])
        self.assertTrue(gate["source_evidence_checked"])
        self.assertFalse(any("source-level" in e for e in gate_errors(gate)))

    def test_gate_records_source_count_for_ui(self):
        trace = _full_trace()
        result: dict = {}
        gate, _errors = gate_and_errors(trace, self._entry(), result)
        self.assertEqual(gate["source_evidence_tool_count"], 0)
        self.assertIn("source_evidence_tool_count", result["evidence_gate"])

    def test_gate_degrades_when_no_sdk_sources(self):
        """部署无 SDK 源时降级放行，但保留观测字段。"""
        trace = _full_trace()
        entry = dict(self._entry(), sdk_sources_available=False)
        gate = evaluate_evidence_gate(trace, entry)
        self.assertTrue(gate["test_failure_subject"])
        self.assertFalse(gate["source_evidence_required"])
        self.assertTrue(gate["source_evidence_checked"])
        self.assertEqual(gate_errors(gate), [])

    def test_gate_stays_strict_when_sdk_sources_available(self):
        trace = _full_trace()
        entry = dict(self._entry(), sdk_sources_available=True)
        gate = evaluate_evidence_gate(trace, entry)
        self.assertTrue(gate["source_evidence_required"])
        self.assertFalse(gate["source_evidence_checked"])
        self.assertTrue(any("source-level" in e for e in gate_errors(gate)))

    def test_missing_hint_defaults_to_strict(self):
        """entry 缺 hint 时按强制处理（fail-safe，不静默放水）。"""
        trace = _full_trace()
        gate = evaluate_evidence_gate(trace, self._entry())
        self.assertTrue(gate["source_evidence_required"])
        self.assertFalse(gate["source_evidence_checked"])


if __name__ == "__main__":
    unittest.main()
