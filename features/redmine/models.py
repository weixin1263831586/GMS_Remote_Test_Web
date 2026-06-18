from __future__ import annotations

from dataclasses import dataclass

_ASSIGNEE_COUNT_CACHE: Dict[int, tuple] = {}
_ASSIGNEE_TREND_CACHE: Dict[int, tuple] = {}
_CACHE_TTL_SECONDS = 600


@dataclass
class RedmineAttachment:
    id: str
    filename: str
    content_url: str = ""
    content_type: str = ""
    filesize: int = 0
