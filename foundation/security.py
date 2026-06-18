from __future__ import annotations


def safe_int(value: str | None, default: int = 0) -> int:
    try:
        return int(value or default)
    except (TypeError, ValueError):
        return default
