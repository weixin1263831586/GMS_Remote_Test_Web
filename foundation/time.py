from __future__ import annotations

from datetime import date, datetime
from typing import Any


def parse_datetime(value: Any) -> datetime | None:
    if value in (None, ''):
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time())
    text = str(value).strip().replace('Z', '+00:00')
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def iso_text(value: Any) -> str:
    if value in (None, ''):
        return ''
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)
