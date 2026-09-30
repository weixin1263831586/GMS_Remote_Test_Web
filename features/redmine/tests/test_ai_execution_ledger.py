"""AIExecutionLedger 状态机与防重复发送契约测试。

覆盖全局审查 AI execution governance 意见的最小落地：
- logical_key 稳定性 / 输入敏感性；
- pending → received → completed|failed 主流程；
- duplicate 拒绝（同一逻辑键的第二次 begin 在活跃租约内必须被拒）；
- 租约过期的活跃 receipt 转 unknown 并放行新 attempt；
- unknown/completed/failed 终态不可改写；
- 崩溃路径 mark_unknown；
- stats 治理视图。
"""

import unittest
from pathlib import Path

from features.redmine.ai_execution_ledger import (
    RECEIPT_LEASE_SECONDS,
    AIExecutionLedger,
    logical_key,
)


def _ledger(tmp: Path) -> AIExecutionLedger:
    return AIExecutionLedger(tmp / "daily_brief.sqlite3")


def _begin_kwargs(**overrides):
    kwargs = {
        "owner_id": "alice",
        "purpose": "issue_diagnosis",
        "issue_id": 648526,
        "subject": "#648526 boot loop",
        "provider": "kkagent",
        "model": "kimi-k2",
        "prompt_version": "v7",
        "analyzer_version": "0.22.36",
        "input_hash": "hash-1",
    }
    kwargs.update(overrides)
    return kwargs


class LogicalKeyTests(unittest.TestCase):
    def test_stable_and_input_sensitive(self):
        first = logical_key("a", 1, "h", "v7")
        self.assertEqual(first, logical_key("a", 1, "h", "v7"))
        self.assertNotEqual(first, logical_key("a", 1, "h", "v8"))
        self.assertNotEqual(first, logical_key("a", 2, "h", "v7"))


class ReceiptLifecycleTests(unittest.TestCase):
    def setUp(self):
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.ledger = _ledger(Path(self._tmp.name))

    def test_begin_creates_pending_receipt(self):
        receipt = self.ledger.begin(**_begin_kwargs())
        self.assertFalse(receipt["duplicate"])
        self.assertEqual(receipt["status"], "pending")
        self.assertEqual(receipt["attempt"], 1)
        self.assertTrue(receipt["logical_key"])

    def test_active_lease_blocks_duplicate_send(self):
        first = self.ledger.begin(**_begin_kwargs())
        second = self.ledger.begin(**_begin_kwargs())
        self.assertTrue(second["duplicate"])
        self.assertEqual(second["receipt_id"], first["receipt_id"])

    def test_main_flow_pending_received_completed(self):
        receipt = self.ledger.begin(**_begin_kwargs())
        self.ledger.mark_received(receipt["receipt_id"], session_id="sess-1")
        stored = self.ledger.get(receipt["receipt_id"])
        self.assertEqual(stored["status"], "received")
        self.assertEqual(stored["session_id"], "sess-1")
        self.ledger.finish(
            receipt["receipt_id"], ok=True, latency_ms=1234,
            usage={"input_tokens": 100, "output_tokens": 50},
        )
        stored = self.ledger.get(receipt["receipt_id"])
        self.assertEqual(stored["status"], "completed")
        self.assertEqual(stored["latency_ms"], 1234)
        self.assertIn("input_tokens", stored["usage_json"])

    def test_finish_failed_is_terminal(self):
        receipt = self.ledger.begin(**_begin_kwargs())
        self.ledger.finish(receipt["receipt_id"], ok=False, error="timeout")
        stored = self.ledger.get(receipt["receipt_id"])
        self.assertEqual(stored["status"], "failed")
        self.assertEqual(stored["error"], "timeout")
        # 终态不可改写：迟到的 completed 不得覆盖 failed。
        self.ledger.finish(receipt["receipt_id"], ok=True)
        self.assertEqual(self.ledger.get(receipt["receipt_id"])["status"], "failed")

    def test_terminal_receipt_does_not_block_new_attempt(self):
        first = self.ledger.begin(**_begin_kwargs())
        self.ledger.finish(first["receipt_id"], ok=False, error="boom")
        second = self.ledger.begin(**_begin_kwargs())
        self.assertFalse(second["duplicate"])
        self.assertEqual(second["attempt"], 2)
        self.assertNotEqual(second["receipt_id"], first["receipt_id"])

    def test_unknown_is_first_class_terminal(self):
        receipt = self.ledger.begin(**_begin_kwargs())
        self.ledger.mark_received(receipt["receipt_id"])
        self.ledger.mark_unknown(receipt["receipt_id"], reason="process killed mid-call")
        stored = self.ledger.get(receipt["receipt_id"])
        self.assertEqual(stored["status"], "unknown")
        # unknown 不可改写。
        self.ledger.finish(receipt["receipt_id"], ok=True)
        self.assertEqual(self.ledger.get(receipt["receipt_id"])["status"], "unknown")

    def test_input_change_produces_independent_key(self):
        self.ledger.begin(**_begin_kwargs())
        different = self.ledger.begin(**_begin_kwargs(input_hash="hash-2"))
        self.assertFalse(different["duplicate"])

    def test_expired_lease_becomes_unknown_and_allows_retry(self):
        first = self.ledger.begin(**_begin_kwargs())
        # 模拟持有进程死亡：把 updated_at 拨回租约之前。
        import sqlite3

        with sqlite3.connect(self.ledger.db_path) as conn:
            conn.execute(
                "UPDATE redmine_ai_execution_receipts SET updated_at=? WHERE receipt_id=?",
                ("2000-01-01T00:00:00", first["receipt_id"]),
            )
        second = self.ledger.begin(**_begin_kwargs())
        self.assertFalse(second["duplicate"])
        expired = self.ledger.get(first["receipt_id"])
        self.assertEqual(expired["status"], "unknown")
        self.assertIn("lease expired", expired["error"])
        self.assertEqual(second["attempt"], 2)

    def test_stats_reports_status_counts(self):
        ok_receipt = self.ledger.begin(**_begin_kwargs())
        self.ledger.finish(ok_receipt["receipt_id"], ok=True)
        unknown_receipt = self.ledger.begin(**_begin_kwargs(issue_id=2))
        self.ledger.mark_unknown(unknown_receipt["receipt_id"], reason="crash")
        stats = self.ledger.stats("alice")
        self.assertEqual(stats["by_status"].get("completed"), 1)
        self.assertEqual(stats["by_status"].get("unknown"), 1)
        self.assertEqual(stats["unknown"], 1)
        self.assertEqual(stats["total"], 2)

    def test_lease_constant_is_explicit(self):
        self.assertGreater(RECEIPT_LEASE_SECONDS, 0)


if __name__ == "__main__":
    unittest.main()
