from __future__ import annotations

from dataclasses import dataclass


_ASSIGNEE_COUNT_CACHE: dict[int, tuple] = {}
_ASSIGNEE_TREND_CACHE: dict[int, tuple] = {}
_CACHE_TTL_SECONDS = 600
# 历史趋势长期缓存（freshness_days 之前的已关闭工单趋势）。
# 工单关闭半年后其趋势计数几乎不再变化，故冻结 7 天，避免每次拉全量分页。
# value: (cached_at_ts, granularity->label->count)
_ASSIGNEE_TREND_HISTORICAL_CACHE: dict[int, tuple] = {}
_HISTORICAL_TTL_SECONDS = 7 * 24 * 3600


@dataclass
class RedmineAttachment:
    id: str
    filename: str
    content_url: str = ""
    content_type: str = ""
    filesize: int = 0
