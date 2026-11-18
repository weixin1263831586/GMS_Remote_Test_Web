"""Evidence timestamps include naive legacy and timezone-aware formats."""
from datetime import datetime, timedelta, timezone

import pytest

from features.redmine.evidence_api import _snapshot_fresh


@pytest.mark.parametrize("stamp", [
    (datetime.now() - timedelta(seconds=10)).isoformat(),
    (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat(),
])
def test_recent_snapshots_are_fresh(stamp):
    assert _snapshot_fresh({"fetched_at": stamp}, 300)


@pytest.mark.parametrize("stamp", [
    (datetime.now() - timedelta(minutes=15)).isoformat(),
    (datetime.now(timezone.utc) - timedelta(minutes=15)).isoformat(),
    (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat(),
    "invalid",
])
def test_old_future_invalid_snapshots_are_not_fresh(stamp):
    assert not _snapshot_fresh({"fetched_at": stamp}, 300)
