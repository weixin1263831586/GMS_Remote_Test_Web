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

import asyncio
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from features.redmine.ai_execution_ledger import (
    RECEIPT_LEASE_SECONDS,
    AIExecutionLedger,
    input_digest,
    logical_key,
)
from features.redmine.daily_brief_models import DailyBriefIssue, DailyBriefRun
from features.redmine.daily_brief_service import DailyBriefService


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
        def _key(**over):
            kwargs = dict(
                owner_id="a", purpose="daily_brief_issue", provider="kkagent",
                issue_id=1, input_hash="h", prompt_version="v7",
            )
            kwargs.update(over)
            return logical_key(**kwargs)

        first = _key()
        self.assertEqual(first, _key())
        self.assertNotEqual(first, _key(prompt_version="v8"))
        self.assertNotEqual(first, _key(issue_id=2))
        # purpose / provider prevent unrelated callers from sharing a receipt.
        self.assertNotEqual(first, _key(purpose="report_diagnosis"))
        self.assertNotEqual(first, _key(provider="openai"))

    def test_input_digest_is_sha256_and_stable(self):
        digest = input_digest("material")
        self.assertEqual(digest, input_digest("material"))
        self.assertEqual(len(digest), 64)
        self.assertNotEqual(digest, input_digest("material2"))
        # dict 形状 canonical 化：键序不影响 digest。
        self.assertEqual(
            input_digest({"a": 1, "b": "x"}), input_digest({"b": "x", "a": 1}),
        )
        # digest 不含原文材料（敏感自由文本不以明文持久化）。
        self.assertNotIn("hint text", input_digest({"hint": "hint text"}))


class ReceiptLifecycleTests(unittest.TestCase):
    def test_touch_renews_long_running_receipt_without_changing_state(self):
        from unittest.mock import patch

        with patch("features.redmine.ai_execution_ledger._now", return_value="2026-09-30T00:00:00"):
            receipt = self.ledger.begin(**_begin_kwargs())
            self.ledger.mark_received(receipt["receipt_id"])
        with patch("features.redmine.ai_execution_ledger._now", return_value="2026-09-30T03:59:00"):
            assert self.ledger.touch(receipt["receipt_id"])
        with patch("features.redmine.ai_execution_ledger._now", return_value="2026-09-30T05:00:00"):
            assert self.ledger.begin(**_begin_kwargs())["duplicate"]
            self.ledger.finish(receipt["receipt_id"], ok=True)
        terminal = self.ledger.get(receipt["receipt_id"])
        assert not self.ledger.touch(receipt["receipt_id"])
        assert self.ledger.get(receipt["receipt_id"]) == terminal
        assert not self.ledger.touch("missing")

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

    def test_fail_early_before_submission_is_failed_not_unknown(self):
        # A failure before submission has a known outcome and is retryable.
        receipt = self.ledger.begin(**_begin_kwargs())
        self.ledger.fail_early(receipt["receipt_id"], error="precollect crashed")
        stored = self.ledger.get(receipt["receipt_id"])
        self.assertEqual(stored["status"], "failed")
        self.assertEqual(stored["error"], "precollect crashed")

    def test_fail_early_never_overrides_received_or_terminal(self):
        receipt = self.ledger.begin(**_begin_kwargs())
        self.ledger.mark_received(receipt["receipt_id"])
        self.ledger.fail_early(receipt["receipt_id"], error="late deterministic failure")
        self.assertEqual(
            self.ledger.get(receipt["receipt_id"])["status"], "received",
        )
        self.ledger.fail_early(receipt["receipt_id"], error="again")
        self.assertEqual(
            self.ledger.get(receipt["receipt_id"])["status"], "received",
        )

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


def test_heartbeat_renews_quiet_session_and_stops_at_terminal(tmp_path):
    async def run():
        ledger = AIExecutionLedger(tmp_path / "receipts.sqlite3")
        receipt = ledger.begin(owner_id="a", purpose="diagnosis", issue_id=1)
        touched = asyncio.Event()
        touch = ledger.touch

        def observe(receipt_id):
            result = touch(receipt_id)
            touched.set()
            return result

        with patch("features.redmine.ai_execution_ledger.RECEIPT_HEARTBEAT_SECONDS", 0.001), \
                patch.object(ledger, "touch", side_effect=observe):
            task = asyncio.create_task(ledger.keep_alive(receipt["receipt_id"]))
            try:
                await asyncio.wait_for(touched.wait(), timeout=2)
                ledger.finish(receipt["receipt_id"], ok=True)
                await asyncio.wait_for(task, timeout=2)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())


def test_cancelled_heartbeat_leaves_no_further_refreshes(tmp_path):
    async def run():
        ledger = AIExecutionLedger(tmp_path / "receipts.sqlite3")
        with patch("features.redmine.ai_execution_ledger.asyncio.sleep", new_callable=AsyncMock) as sleep:
            sleep.side_effect = asyncio.CancelledError
            with patch.object(ledger, "touch") as touch:
                try:
                    await ledger.keep_alive("receipt")
                except asyncio.CancelledError:
                    pass
                touch.assert_not_called()

    asyncio.run(run())


@pytest.mark.parametrize("cancelled", [False, True])
def test_service_owns_heartbeat_and_uses_stored_issue_facts(tmp_path, cancelled):
    async def run():
        service = DailyBriefService.__new__(DailyBriefService)
        service.owner_id = "a"
        record = DailyBriefIssue(run_id="run", issue_id=1, buckets=[], subject="Title only")
        repository = Mock(db_path=tmp_path / "ledger.sqlite3")
        repository.get_issue.return_value = record
        repository.is_cancel_requested.return_value = False
        service.repository = repository
        brief = DailyBriefRun(owner_id="a", brief_date="2026-09-30", mode="issue:1", run_id="run")
        alive = asyncio.Event()
        stopped = asyncio.Event()

        async def heartbeat(receipt_id):
            alive.set()
            try:
                await asyncio.Future()
            finally:
                stopped.set()

        async def analyze(*args):
            args[-1]["_provider_started"]()
            await alive.wait()
            if cancelled:
                raise asyncio.CancelledError
            return SimpleNamespace(ok=False, raw_output="", trace={}, error="failed", error_type="test")

        stored = {"subject": "RK3576 Android16 CTS", "failures_json": [{
            "module": "M", "name": "T", "reason": "AssertionError: expected 32 but was 0",
        }]}
        redmine = Mock()
        redmine.repository.get_issue.return_value = stored
        with patch.object(service.ai_ledger, "keep_alive", side_effect=heartbeat), \
                patch("features.redmine.daily_brief_service.start_analysis_progress", return_value=None), \
                patch("features.redmine.daily_brief_service.precollect_deep_evidence", new_callable=AsyncMock), \
                patch("features.redmine.daily_brief_service._sdk_sources_available", return_value=False), \
                patch("features.redmine.daily_brief_cancellation.analyze_with_persisted_cancel", side_effect=analyze), \
                patch("features.redmine.daily_brief_cancellation.reset_cancelled_issue"), \
                patch("features.redmine.api.get_redmine_service_for_owner", return_value=redmine):
            try:
                await service._analyze_one(brief, 1, {}, Mock(), {})
            except asyncio.CancelledError:
                assert cancelled
        assert stopped.is_set()
        if cancelled:
            assert service.ai_ledger.stats("a")["unknown"] == 1
        else:
            execution = repository.record_ai_execution.call_args.args[-1]
            assert execution["failure_identity"]["device_class"] == "RK3576"
            assert execution["failure_identity"]["error_signature"] == "assertionerror expected 32 but was 0"
            assert service.ai_ledger.get(execution["receipt_id"])["status"] == "failed"

    asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
