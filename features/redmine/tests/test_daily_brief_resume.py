"""Lease- and heartbeat-fenced checkpoint restoration regression tests."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from features.redmine.daily_brief_resume import (
    ResumeReceiptBusyError, admit_resume_receipt, valid_resume_trace,
)


def _checkpoint(**changes):
    value = {
        "status": "running", "attempt_no": 1,
        "session_id": "sess-1", "snapshot_hash": "hash-1", "lease_token": "old",
        "trace": {"session_id": "sess-1", "tools": []},
    }
    value.update(changes)
    return value


def _lease(**changes):
    value = {
        "status": "running", "lease_token": "new",
        "lease_expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
    }
    value.update(changes)
    return value


def test_resume_requires_a_new_live_worker_lease_and_matching_snapshot():
    options = dict(previous_status="pending", attempt_no=2, snapshot_hash="hash-1")
    assert valid_resume_trace(_checkpoint(), _lease(), **options).session_id == "sess-1"
    assert valid_resume_trace(_checkpoint(lease_token="new"), _lease(), **options) is None
    assert valid_resume_trace(_checkpoint(snapshot_hash="other"), _lease(), **options) is None
    assert valid_resume_trace(_checkpoint(attempt_no=2), _lease(), **options) is None
    assert valid_resume_trace(_checkpoint(), _lease(
        lease_expires_at=(datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    ), **options) is None


def test_resume_receipt_admission_retries_only_under_same_running_lease():
    async def scenario():
        ledger = Mock()
        ledger.begin.side_effect = [
            {"duplicate": True, "receipt_id": "old"},
            {"duplicate": False, "receipt_id": "new"},
        ]
        repository = Mock()
        repository.is_cancel_requested.return_value = False
        repository.running_lease_for_issue.return_value = _lease()
        trace = SimpleNamespace(session_id="sess-1")
        with patch("asyncio.sleep", new_callable=AsyncMock) as sleep:
            receipt = await admit_resume_receipt(
                ledger, repository, {"owner_id": "owner"}, trace,
                "run-id", 662309, "new",
            )
        assert receipt["receipt_id"] == "new"
        assert ledger.begin.call_count == 2
        sleep.assert_awaited_once()
        assert ledger.begin.call_args.kwargs["resume_session_id"] == "sess-1"
    asyncio.run(scenario())


def test_active_provider_deferred_instead_of_marked_failed():
    async def scenario():
        ledger = Mock()
        ledger.begin.return_value = {"duplicate": True, "receipt_id": "active"}
        repository = Mock()
        repository.is_cancel_requested.return_value = False
        repository.running_lease_for_issue.return_value = _lease()
        trace = SimpleNamespace(session_id="sess-1")
        # Fast-forward the grace period without changing asyncio's clock.
        with patch("features.redmine.ai_execution_ledger.RECEIPT_RESUME_STALE_SECONDS", -16):
            try:
                await admit_resume_receipt(
                    ledger, repository, {"owner_id": "owner"}, trace,
                    "run-id", 123, "new",
                )
            except ResumeReceiptBusyError:
                pass
            else:
                raise AssertionError("active provider must defer rather than fail")
        ledger.begin.assert_called_once()
    asyncio.run(scenario())


def test_cancel_in_admission_resets_issue_and_closes_progress_attempt():
    from features.redmine.daily_brief_cancellation import RunCancelledError

    async def scenario():
        ledger = Mock()
        ledger.begin.return_value = {"duplicate": True, "receipt_id": "active"}
        repository = Mock()
        repository.is_cancel_requested.return_value = True
        record, progress = Mock(), Mock()
        with patch("asyncio.sleep", new_callable=AsyncMock) as sleep:
            try:
                await admit_resume_receipt(
                    ledger, repository, {"owner_id": "owner"},
                    SimpleNamespace(session_id="sess-1"), "run-id", 123, "new",
                    record, progress,
                )
            except RunCancelledError:
                pass
            else:
                raise AssertionError("cancellation must interrupt receipt wait")
            sleep.assert_not_awaited()
        repository.upsert_issue.assert_called_once_with(record)
        assert record.status == "pending" and record.duration_ms == 0
        progress.analysis_finished.assert_called_once_with(ok=False, cancelled=True)
    asyncio.run(scenario())


def test_unverified_resume_never_retries_an_active_receipt():
    async def scenario():
        ledger = Mock()
        ledger.begin.return_value = {"duplicate": True}
        with patch("asyncio.sleep", new_callable=AsyncMock) as sleep:
            receipt = await admit_resume_receipt(ledger, Mock(), {"owner_id": "a"},
                                                None, "run", 1, "")
        assert receipt["duplicate"]
        sleep.assert_not_awaited()
        assert "resume_session_id" not in ledger.begin.call_args.kwargs
    asyncio.run(scenario())
