from __future__ import annotations

from dataclasses import dataclass

from foundation.cache import TTLCache


_CACHE_TTL_SECONDS = 600
_HISTORICAL_TTL_SECONDS = 7 * 24 * 3600
_ASSIGNEE_CACHE_MAX_ENTRIES = 512

# Credential identity isolates servers/accounts; trends also key their exact
# cutoff and limit so independently expiring segments always share a boundary.
_ASSIGNEE_COUNT_CACHE = TTLCache[tuple, dict](
    _CACHE_TTL_SECONDS, max_entries=_ASSIGNEE_CACHE_MAX_ENTRIES,
)
_ASSIGNEE_TREND_CACHE = TTLCache[tuple, dict](
    _CACHE_TTL_SECONDS, max_entries=_ASSIGNEE_CACHE_MAX_ENTRIES,
)
_ASSIGNEE_TREND_HISTORICAL_CACHE = TTLCache[tuple, dict](
    _HISTORICAL_TTL_SECONDS, max_entries=_ASSIGNEE_CACHE_MAX_ENTRIES,
)


@dataclass
class RedmineAttachment:
    id: str
    filename: str
    content_url: str = ""
    content_type: str = ""
    filesize: int = 0
