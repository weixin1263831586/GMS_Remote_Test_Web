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
