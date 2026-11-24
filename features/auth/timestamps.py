"""UTC timestamps used by authentication persistence."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def to_utc_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def from_utc_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def seen_recently(last_seen_at: str | None, now: datetime) -> bool:
    if not last_seen_at:
        return False
    try:
        return now - from_utc_iso(last_seen_at) < timedelta(seconds=60)
    except ValueError:
        return False
