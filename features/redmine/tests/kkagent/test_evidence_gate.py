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


def _full_trace(history_searches: int = 2, with_attachments: bool = True, issue_id: int = 1) -> KkAgentTrace:
    """构建一次针对 *issue_id* 的取证轨迹（带 snapshot 归属，贴近真实调用）。"""
    events: list[dict] = [
        {"type": "session", "session_id": "s1"},
        {"type": "tool_call", "tool_call_id": "c1",
         "tool_name": "gms_rt_redmine_issue_fetch", "input": {"issue": issue_id}},
        {"type": "tool_result", "tool_call_id": "c1", "is_error": False,
         "output": json.dumps({"issue_id": issue_id, "snapshot_id": f"snap-{issue_id}"})},
        {"type": "tool_call", "tool_call_id": "c2",
         "tool_name": "gms_rt_redmine_journals", "input": {"snapshot_id": f"snap-{issue_id}"}},
        {"type": "tool_result", "tool_call_id": "c2", "is_error": False, "output": "j"},
    ]
    call_id = 10
    if with_attachments:
        events += [
            {"type": "tool_call", "tool_call_id": "c3",
             "tool_name": "gms_rt_redmine_attachments",
             "input": {"snapshot_id": f"snap-{issue_id}"}},
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

    def test_unrecoverable_read_failure_satisfies_attachment_gate(self):
        # 「没有可用文本」是基础设施事实（PDF 无文字层等），重试永远失败；
        # 门禁必须把这次失败读取视为已核验，否则模型被锁死在重试循环
        # （2026-09-24 晨报批次真实发生）。
        trace = _full_trace()
        attachment_call = next(
            call for call in trace.tool_calls if "redmine_attachments" in call.tool_name
        )
        attachment_call.text_artifact_ids = ["pdf-no-text"]
        gate = evaluate_evidence_gate(trace, {"attachment_count": 2})
        self.assertFalse(gate["attachments_checked"])

        consume_line(trace, json.dumps({
            "type": "tool_call", "tool_call_id": "read-p",
            "tool_name": "gms_rt_redmine_artifact_read",
            "input": {"artifact_id": "pdf-no-text"},
        }))
        consume_line(trace, json.dumps({
            "type": "tool_result", "tool_call_id": "read-p",
            "is_error": True,
            "output": json.dumps(
                {"ok": False, "error": "该 artifact 没有可用文本"},
                ensure_ascii=False,
            ),
        }))
        gate = evaluate_evidence_gate(trace, {"attachment_count": 2})
        self.assertTrue(gate["attachments_checked"])
        self.assertNotIn("pdf-no-text", gate["unread_text_artifact_ids"])

    def test_wrong_id_read_failure_does_not_satisfy_gate(self):
        # ID 写错之类的可恢复失败不算「已读」：模型仍需纠正后重读。
        trace = _full_trace()
        attachment_call = next(
            call for call in trace.tool_calls if "redmine_attachments" in call.tool_name
        )
        attachment_call.text_artifact_ids = ["text-1"]
        consume_line(trace, json.dumps({
            "type": "tool_call", "tool_call_id": "read-w",
            "tool_name": "gms_rt_redmine_artifact_read",
            "input": {"artifact_id": "text-1"},
        }))
        consume_line(trace, json.dumps({
            "type": "tool_result", "tool_call_id": "read-w",
            "is_error": True,
            "output": json.dumps({"ok": False, "error": "artifact 不存在"},
                                 ensure_ascii=False),
        }))
        gate = evaluate_evidence_gate(trace, {"attachment_count": 2})
        self.assertFalse(gate["attachments_checked"])
        self.assertIn("text-1", gate["unread_text_artifact_ids"])

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
            "issue_id": 1,
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


class TargetIssueScopeTests(unittest.TestCase):
    """当前 issue 与相似 issue 的证据必须严格分开。"""

    def test_current_issue_evidence_passes(self):
        trace = _full_trace(issue_id=123, history_searches=0)
        gate = evaluate_evidence_gate(
            trace, {"issue_id": 123, "attachment_count": 0}
        )
        self.assertTrue(gate["issue_fetched"])
        self.assertTrue(gate["journals_checked"])

    def test_issue_123_cannot_use_issue_999_journals(self):
        # 只 fetch 了 #123 自己（相似单 #999 的 journals/attachments 不算数）。
        fetch_only = [
            {"type": "session", "session_id": "s1"},
            {"type": "tool_call", "tool_call_id": "f1",
             "tool_name": "gms_rt_redmine_issue_fetch", "input": {"issue": 123}},
            {"type": "tool_result", "tool_call_id": "f1", "is_error": False,
             "output": json.dumps({"issue_id": 123, "snapshot_id": "snap-123"})},
        ]
        trace = _trace(*fetch_only)
        gate = evaluate_evidence_gate(
            trace, {"issue_id": 123, "attachment_count": 0}
        )
        self.assertTrue(gate["issue_fetched"])
        self.assertFalse(gate["journals_checked"])
        errors = gate_errors(gate)
        self.assertTrue(any("journals" in e for e in errors))

    def test_issue_123_cannot_use_issue_999_attachments(self):
        events = [
            {"type": "session", "session_id": "s1"},
            {"type": "tool_call", "tool_call_id": "f1",
             "tool_name": "gms_rt_redmine_issue_fetch", "input": {"issue": 123}},
            {"type": "tool_result", "tool_call_id": "f1", "is_error": False,
             "output": json.dumps({"issue_id": 123, "snapshot_id": "snap-123"})},
            {"type": "tool_call", "tool_call_id": "a1",
             "tool_name": "gms_rt_redmine_attachments",
             "input": {"snapshot_id": "snap-999"}},
            {"type": "tool_result", "tool_call_id": "a1", "is_error": False,
             "output": json.dumps({"data": {"artifacts": [
                 {"artifact_id": "x1", "kind": "image", "status": "ready"},
             ]}})},
        ]
        trace = _trace(*events)
        gate = evaluate_evidence_gate(
            trace, {"issue_id": 123, "attachment_count": 2}
        )
        self.assertFalse(gate["attachments_listed"])
        self.assertFalse(gate["attachments_checked"])
        self.assertTrue(any("attachments" in e for e in gate_errors(gate)))


class ReproducibleSourceEvidenceTests(unittest.TestCase):
    """动态索引（reproducible=false）证据不能单独 confirm 根因。"""

    @staticmethod
    def _entry() -> dict:
        return {
            "issue_id": 1,
            "subject": "VTS vts_ltp_test_arm_64 fail",
            "attachment_count": 0,
            "sdk_sources_available": True,
        }

    def _gate_with_source_output(self, payload: str) -> list[str]:
        trace = _full_trace()
        trace.tool_calls.append(ToolTrace(
            tool_call_id="sdk1",
            tool_name="gms_rt_sdk_search",
            status="succeeded",
        ))
        # 模拟 _record_tool_result 对 provider 信封的 reproducible 提取。
        from features.redmine.kkagent.trace import _source_reproducible_flag

        trace.tool_calls[-1].source_reproducible = _source_reproducible_flag(payload)
        result: dict = {"root_cause_type": "confirmed"}
        _gate, errors = gate_and_errors(trace, self._entry(), result)
        return [e for e in errors if "reproducible" in e]

    def test_dynamic_index_evidence_cannot_confirm_root_cause(self):
        errors = self._gate_with_source_output(
            json.dumps({"success": True, "data": {
                "source_id": "opengrok", "reproducible": False,
                "matches": [],
            }})
        )
        self.assertEqual(len(errors), 1)
        self.assertIn("cannot alone confirm", errors[0])

    def test_local_git_evidence_can_confirm_root_cause(self):
        trace = _full_trace()
        sdk_call = ToolTrace(
            tool_call_id="sdk1",
            tool_name="gms_rt_sdk_read",
            status="succeeded",
            tool_input={"path": "kernel/drivers/gpu/drm/panel/panel-rk3576.c"},
        )
        trace.tool_calls.append(sdk_call)
        from features.redmine.kkagent.trace import _source_reproducible_flag

        sdk_call.source_reproducible = _source_reproducible_flag(
            json.dumps({"success": True, "data": {
                "source_id": "git", "commit": "abc", "reproducible": True,
            }})
        )
        result = {
            "root_cause_type": "confirmed",
            "evidence": [{
                "source": "code",
                "reference": "kernel/drivers/gpu/drm/panel/panel-rk3576.c",
                "fact": "缺少 stable backport",
            }],
        }
        gate, errors = gate_and_errors(trace, self._entry(), result)
        self.assertEqual(errors, [])
        # claim 绑定到一条可复现源码证据。
        reproducible_ids = {
            item["evidence_id"] for item in gate["evidence_ledger"]
            if item["reproducible"] is True
        }
        self.assertTrue(reproducible_ids)
        self.assertTrue(
            any(set(b["evidence_ids"]) & reproducible_ids
                for b in gate["claim_bindings"])
        )

    def test_missing_flag_counts_as_not_reproducible(self):
        errors = self._gate_with_source_output(
            json.dumps({"success": True, "data": {"matches": []}})
        )
        self.assertEqual(len(errors), 1)

    def test_likely_root_cause_not_blocked(self):
        trace = _full_trace()
        trace.tool_calls.append(ToolTrace(
            tool_call_id="sdk1", tool_name="gms_rt_sdk_search", status="succeeded",
        ))
        result: dict = {"root_cause_type": "likely"}
        _gate, errors = gate_and_errors(trace, self._entry(), result)
        self.assertFalse(any("reproducible" in e for e in errors))


class ClaimEvidenceLedgerTests(unittest.TestCase):
    """源码证据 A 不足以证实无关根因 B（claim 级绑定）。"""

    @staticmethod
    def _entry() -> dict:
        return {
            "issue_id": 1,
            "subject": "VTS vts_ltp_test_arm_64 fail",
            "attachment_count": 0,
            "sdk_sources_available": True,
        }

    def _trace_with_reproducible_source(self, issue_id: int = 1) -> KkAgentTrace:
        trace = _full_trace(issue_id=issue_id)
        from features.redmine.kkagent.trace import _source_reproducible_flag

        call = ToolTrace(
            tool_call_id="sdk1",
            tool_name="gms_rt_sdk_read",
            status="succeeded",
            tool_input={"path": "kernel/mm/mmap.c"},
        )
        call.source_reproducible = _source_reproducible_flag(
            json.dumps({"success": True, "data": {
                "source_id": "git", "reproducible": True,
            }})
        )
        trace.tool_calls.append(call)
        return trace

    def test_confirmed_with_unrelated_evidence_fails_binding(self):
        trace = self._trace_with_reproducible_source()
        result = {
            "root_cause_type": "confirmed",
            "evidence": [{
                "source": "log",
                "reference": "host_log_12345.txt",
                "fact": "SELinux denial",
            }],
        }
        gate, errors = gate_and_errors(trace, self._entry(), result)
        self.assertEqual(gate["reproducible_source_evidence_count"], 1)
        self.assertTrue(any("cited evidence references" in e for e in errors))

    def test_confirmed_with_cited_source_passes_binding(self):
        trace = self._trace_with_reproducible_source()
        result = {
            "root_cause_type": "confirmed",
            "evidence": [{
                "source": "code",
                "reference": "kernel/mm/mmap.c",
                "fact": "缺少 upstream 修复",
            }],
        }
        _gate, errors = gate_and_errors(trace, self._entry(), result)
        self.assertFalse(any("cited evidence references" in e for e in errors))

    def test_ledger_assigns_stable_ids(self):
        trace = _full_trace()
        ledger = trace.evidence_ledger()
        self.assertTrue(all(item["evidence_id"].startswith("EV-") for item in ledger))
        self.assertEqual(
            [item["evidence_id"] for item in ledger],
            [f"EV-{i:03d}" for i in range(1, len(ledger) + 1)],
        )

    def test_numeric_reference_digit_boundary(self):
        """引用 #1000 不得被子串匹配误绑到 #100 的证据（伪造引用不算已证实）。"""
        trace = self._trace_with_reproducible_source(issue_id=100)
        ledger = trace.evidence_ledger()
        self.assertTrue(any("100" in ref for item in ledger for ref in item["refs"]),
                        "前置条件：ledger 里存在 #100 引用")

        def _result(extra_reference: str) -> dict:
            return {
                "root_cause_type": "confirmed",
                "evidence": [
                    {"source": "code", "reference": "kernel/mm/mmap.c",
                     "fact": "缺少 upstream 修复"},
                    {"source": "log", "reference": extra_reference,
                     "fact": "相关工单引用"},
                ],
            }

        def _bindings_for(reference: str, gate: dict) -> list[str]:
            for binding in gate["claim_bindings"]:
                if binding["reference"] == reference:
                    return binding["evidence_ids"]
            return []

        # 正向控制：真实引用 #100（及裸数字 100）能绑定到 issue_fetch 证据，
        # 同时 SDK 证据满足 confirmed 的可复现要求，无 integrity 错误。
        for extra in ("#100", "issue 100 ref"):
            gate, errors = gate_and_errors(trace, self._entry(), _result(extra))
            self.assertFalse(any("cited evidence references" in e for e in errors),
                             f"{extra!r} 应绑定到证据")
            self.assertTrue(_bindings_for(extra, gate),
                            f"{extra!r} 的 claim 绑定不应为空")
        # 数字边界：#1000 / 1001 与 #100 是不同 issue，不得被子串匹配
        # 静默绑定（旧子串匹配会把它绑到 #100 的证据上，伪造引用
        # 借此伪装成「已对应真实调用」）。SDK 路径引用仍满足 confirmed
        # 的可复现要求，因此不产生额外 gate 错误——缺陷的可见面是
        # claim_bindings 的误绑。
        for extra in ("#1000", "issue 1001 ref"):
            gate, _errors = gate_and_errors(trace, self._entry(), _result(extra))
            self.assertFalse(_bindings_for(extra, gate),
                             f"{extra!r} 不应绑定到 #100 的证据")


if __name__ == "__main__":
    unittest.main()
