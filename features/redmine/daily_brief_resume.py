"""Lease- and snapshot-fenced Redmine analysis checkpoint restoration.

A stored session is only reusable after the *current* Worker has reacquired
an active job lease for the same run/issue. The old lease is audit metadata,
not permission to execute on behalf of its former holder.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .kkagent.trace import trace_from_summary


def valid_resume_trace(
    checkpoint: dict[str, Any] | None, lease: dict[str, Any] | None,
    *, previous_status: str, attempt_no: int, snapshot_hash: str,
):
    if not checkpoint or not lease or previous_status not in {"pending", "running", "failed"}:
        return None
    if (
        checkpoint.get("status") != "running"
        or int(checkpoint.get("attempt_no") or 0) >= attempt_no
        or checkpoint.get("snapshot_hash") != snapshot_hash
        or lease.get("status") != "running"
        or not lease.get("lease_token")
        or not checkpoint.get("lease_token")
        or checkpoint.get("lease_token") == lease.get("lease_token")
        or not checkpoint.get("session_id")
    ):
        return None
    try:
        expiry = datetime.fromisoformat(
            str(lease["lease_expires_at"]).replace("Z", "+00:00")
        )
        summary = checkpoint.get("trace") or {}
        if expiry <= datetime.now(timezone.utc):
            return None
        if summary.get("session_id") != checkpoint["session_id"]:
            return None
        return trace_from_summary(summary)
    except (TypeError, ValueError, KeyError):
        return None

class ResumeReceiptBusyError(RuntimeError):
    """An old provider still owns this receipt: retry without failing the job."""


def mark_deferred_issue(repository: Any, record: Any, progress: Any) -> None:
    """Leave an interrupted attempt pending and its progress terminal."""
    record.status = "pending"
    record.error_type = "receipt_busy"
    record.error = "previous AI analysis still active; queued for retry"
    record.started_at = record.finished_at = ""
    repository.upsert_issue(record)
    if progress is not None:
        progress.analysis_deferred()


async def admit_resume_receipt(
    ledger: Any, repository: Any, receipt_args: dict[str, Any], resumed: Any,
    run_id: str, issue_id: int, lease_token: str,
) -> dict[str, Any]:
    """Do not emit a second AI request while the previous provider is alive.

    The execution ledger remains the single transaction owner. A new Worker
    lease only authorizes waiting; taking over the receipt requires multiple
    missed heartbeats and the exact prior session.
    """
    import asyncio
    import time

    from .ai_execution_ledger import RECEIPT_RESUME_STALE_SECONDS
    from .daily_brief_cancellation import RunCancelledError

    if resumed is not None:
        receipt_args = {
            **receipt_args,
            "resume_session_id": resumed.session_id,
            "resume_stale_seconds": RECEIPT_RESUME_STALE_SECONDS,
        }
    receipt = ledger.begin(**receipt_args)
    if receipt.get("duplicate") and resumed is not None:
        deadline = time.monotonic() + RECEIPT_RESUME_STALE_SECONDS + 15
        while receipt.get("duplicate") and time.monotonic() < deadline:
            if repository.is_cancel_requested(run_id):
                raise RunCancelledError()
            await asyncio.sleep(3)
            lease = repository.running_lease_for_issue(run_id, issue_id)
            if not lease or lease.get("lease_token") != lease_token:
                raise RuntimeError("lost Worker lease while awaiting AI receipt recovery")
            receipt = ledger.begin(**receipt_args)
        if receipt.get("duplicate"):
            raise ResumeReceiptBusyError(
                "previous AI receipt is still active; analysis queued for retry"
            )
    return receipt
