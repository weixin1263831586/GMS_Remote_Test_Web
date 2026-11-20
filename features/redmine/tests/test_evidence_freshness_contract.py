"""Evidence timestamps include naive legacy and timezone-aware formats."""
from datetime import datetime, timedelta, timezone

import pytest

from features.redmine.evidence_api import _snapshot_fresh


@pytest.mark.parametrize("tz", [None, timezone.utc])
def test_recent_snapshots_are_fresh(tz):
    stamp = (datetime.now(tz) - timedelta(seconds=10)).isoformat()
    assert _snapshot_fresh({"fetched_at": stamp}, 300)


@pytest.mark.parametrize(("tz", "minutes"), [(None, -15), (timezone.utc, -15), (timezone.utc, 15)])
def test_old_future_snapshots_are_not_fresh(tz, minutes):
    stamp = (datetime.now(tz) + timedelta(minutes=minutes)).isoformat()
    assert not _snapshot_fresh({"fetched_at": stamp}, 300)


def test_invalid_snapshot_is_not_fresh():
    assert not _snapshot_fresh({"fetched_at": "invalid"}, 300)
