"""Redmine client facade.

Standard Redmine resources are read through python-redmine. File upload/download
and issue replies stay on aiohttp so large attachments can use explicit
timeouts, streaming-friendly APIs, and existing error handling.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta
from typing import Any

import aiohttp
from redminelib import Redmine

from features.redmine.attachments import RedmineAttachmentMixin
from features.redmine.models import (
    _ASSIGNEE_COUNT_CACHE,
    _ASSIGNEE_TREND_CACHE,
    _ASSIGNEE_TREND_HISTORICAL_CACHE,
    _CACHE_TTL_SECONDS,
    _HISTORICAL_TTL_SECONDS,
)
from features.redmine.users import RESOLVED_STATUS_NAMES, _parse_dt, _sorted_slice, _time_key


logger = logging.getLogger(__name__)


def clear_historical_trend_cache() -> None:
    """清除历史趋势长期缓存。

    仅在 ``freshness_days`` 变化（近期/历史边界移动）或显式重置时调用，使历史
    分桶在下一次请求时重建。缓存的所有权属于本模块（resolved_trends_by_assignee
    读写它），故失效入口也集中在此，避免调用方 reach 进 models 的私有字典。
    """
    _ASSIGNEE_TREND_HISTORICAL_CACHE.clear()


class RedmineClient(RedmineAttachmentMixin):
    """Small project-facing Redmine API wrapper.

    Holds a lazily-created aiohttp.ClientSession that is reused across requests
    for connection pooling and HTTP keep-alive. Call ``close()`` when done
    (the RedmineAgent creates short-lived clients per operation, so the session
    is lightweight).
    """

    def __init__(self, base_url: str, username: str = "", password: str = ""):
        self.base_url = (base_url or "").rstrip("/")
        self.username = username or ""
        self.password = password or ""
        kwargs = {}
        if self.username and self.password:
            kwargs.update({"username": self.username, "password": self.password})
        self._redmine = Redmine(self.base_url, **kwargs)
        self._session: aiohttp.ClientSession | None = None

    def _get_session(self) -> aiohttp.ClientSession:
        """Return a reusable aiohttp session (created on first use)."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self) -> None:
        """Close the underlying aiohttp session."""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    async def get_issue(self, issue_id: str, include: list[str] | None = None) -> Any:
        """Fetch an issue through python-redmine."""
        return await asyncio.to_thread(
            self._redmine.issue.get,
            int(issue_id),
            include=include or [],
        )

    async def create_issue(self, project_id: str, subject: str, **fields: Any) -> Any:
        """Create a Redmine issue through python-redmine."""
        payload = {"project_id": project_id, "subject": subject, **fields}
        return await asyncio.to_thread(self._redmine.issue.create, **payload)

    async def update_issue(self, issue_id: str, **fields: Any) -> None:
        """Update a Redmine issue through python-redmine."""
        await asyncio.to_thread(self._redmine.issue.update, int(issue_id), **fields)

    async def list_issue_journals(self, issue_id: str) -> list[Any]:
        """Fetch issue journals through python-redmine."""
        issue = await self.get_issue(issue_id, include=["journals"])
        return list(getattr(issue, "journals", []) or [])

    async def fetch_issue_metadata_snapshot(self, issue_id: int) -> dict[str, Any]:
        """Fetch one issue's current metadata and journals for live dashboards."""

        def _obj_name(obj: Any) -> str:
            if isinstance(obj, dict):
                return str(obj.get("name") or "")
            return str(getattr(obj, "name", "") or obj or "")

        def _obj_email(obj: Any) -> str:
            if obj is None:
                return ""
            return str(getattr(obj, "mail", "") or getattr(obj, "email", "") or getattr(obj, "login", "") or "")

        def _detail() -> dict[str, Any]:
            issue = self._redmine.issue.get(int(issue_id), include=["journals"])
            journals = []
            for item in getattr(issue, "journals", []) or []:
                details = []
                for detail in getattr(item, "details", []) or []:
                    details.append({
                        "property": str(getattr(detail, "property", "")),
                        "name": str(getattr(detail, "name", "")),
                        "old_value": str(getattr(detail, "old_value", "")),
                        "new_value": str(getattr(detail, "new_value", "")),
                    })
                journals.append({
                    "id": str(getattr(item, "id", "")),
                    "user": _obj_name(getattr(item, "user", None)),
                    "user_email": _obj_email(getattr(item, "user", None)),
                    "created_on": str(getattr(item, "created_on", "") or ""),
                    "notes": str(getattr(item, "notes", "") or "")[:2000],
                    "details": details,
                })
            status_name = _obj_name(getattr(issue, "status", None))
            return {
                "issue_id": int(getattr(issue, "id", 0) or 0),
                "subject": str(getattr(issue, "subject", "") or ""),
                "status_name": status_name,
                "priority_name": _obj_name(getattr(issue, "priority", None)),
                "assigned_to_name": _obj_name(getattr(issue, "assigned_to", None)),
                "created_on": str(getattr(issue, "created_on", "") or ""),
                "updated_on": str(getattr(issue, "updated_on", "") or ""),
                "closed_on": str(getattr(issue, "closed_on", "") or ""),
                "description": str(getattr(issue, "description", "") or ""),
                "journals_json": journals,
                "attachments_json": [],
                "failures_json": [],
                "is_resolved": int(status_name in RESOLVED_STATUS_NAMES),
                "last_scanned_at": datetime.now().isoformat(timespec="seconds"),
            }

        return await asyncio.to_thread(_detail)

    async def get_current_user(self) -> Any:
        """Fetch the authenticated Redmine user."""
        return await asyncio.to_thread(self._redmine.user.get, "current")

    async def search_users(self, term: str, limit: int = 10) -> list[dict[str, Any]]:
        """Search Redmine users by name/login/mail."""
        term = (term or "").strip()
        if not term:
            return []
        limit = max(1, min(int(limit or 10), 50))

        def _search():
            users = self._redmine.user.filter(name=term, limit=limit)
            return [
                {
                    "id": int(user.id),
                    "login": str(getattr(user, "login", "") or ""),
                    "firstname": str(getattr(user, "firstname", "") or ""),
                    "lastname": str(getattr(user, "lastname", "") or ""),
                    "mail": str(getattr(user, "mail", "") or ""),
                    "name": f"{getattr(user, 'firstname', '')} {getattr(user, 'lastname', '')}".strip(),
                }
                for user in users
            ]

        return await asyncio.to_thread(_search)

    async def fetch_all_assigned_issues(
        self,
        status_id: str = "*",
        limit: int = 100,
        sort: str = "updated_on:desc",
    ) -> list[Any]:
        """Fetch up to `limit` issues assigned to me (no date window); raise limit toward 5000 for a fuller local DB."""
        return await self._paginate_issues("me", status_id, limit, sort)

    async def fetch_issues_by_assignee(
        self,
        assignee_id: int,
        status_id: str = "*",
        limit: int = 1000,
        sort: str = "updated_on:desc",
        filters: dict[str, Any] | None = None,
    ) -> list[Any]:
        """Fetch issues assigned to a specific Redmine user id."""
        return await self._paginate_issues(int(assignee_id), status_id, limit, sort, filters=filters)

    async def fetch_project_issues(
        self,
        project_id: str,
        status_id: str = "*",
        limit: int = 1000,
        sort: str = "updated_on:desc",
    ) -> list[Any]:
        """Fetch issues in a Redmine project."""
        project_id = str(project_id or "").strip().strip("/")
        if not project_id:
            return []
        limit = max(1, min(int(limit or 1000), 5000))
        page_size = min(limit, 100)

        def _fetch():
            all_items = []
            seen_ids = set()
            offset = 0
            while len(all_items) < limit:
                issues = self._redmine.issue.filter(
                    project_id=project_id,
                    status_id=status_id,
                    sort=sort,
                    limit=page_size,
                    offset=offset,
                )
                page = list(issues)
                if not page:
                    break
                added = 0
                for issue in page:
                    issue_id = int(issue.id)
                    if issue_id in seen_ids:
                        continue
                    seen_ids.add(issue_id)
                    all_items.append(issue)
                    added += 1
                    if len(all_items) >= limit:
                        break
                if len(page) < page_size or added == 0:
                    break
                offset += page_size
            return all_items

        return await asyncio.to_thread(_fetch)

    async def _paginate_issues(
        self,
        assigned_to_id: Any,
        status_id: str = "*",
        limit: int = 1000,
        sort: str = "updated_on:desc",
        filters: dict[str, Any] | None = None,
    ) -> list[Any]:
        """Shared paginated issue fetcher for both 'me' and specific assignee."""
        limit = max(1, min(int(limit or 1000), 5000))
        page_size = min(limit, 100)
        extra_filters = dict(filters or {})

        def _fetch():
            all_items = []
            seen_ids = set()
            offset = 0
            while len(all_items) < limit:
                issues = self._redmine.issue.filter(
                    assigned_to_id=assigned_to_id,
                    status_id=status_id,
                    sort=sort,
                    limit=page_size,
                    offset=offset,
                    **extra_filters,
                )
                page = list(issues)
                if not page:
                    break
                added = 0
                for issue in page:
                    issue_id = int(issue.id)
                    if issue_id in seen_ids:
                        continue
                    seen_ids.add(issue_id)
                    all_items.append(issue)
                    added += 1
                    if len(all_items) >= limit:
                        break
                if len(page) < page_size or added == 0:
                    break
                offset += page_size
            return all_items

        return await asyncio.to_thread(_fetch)

    async def fetch_open_issue_snapshots_by_assignee(
        self,
        assignee_id: int,
        limit: int = 500,
        window_days: int = 0,
    ) -> list[dict[str, Any]]:
        """Fetch recent open issues with journals for live reply statistics."""
        max_items = max(1, min(int(limit or 500), 1000))
        cutoff = datetime.now() - timedelta(days=int(window_days)) if int(window_days or 0) > 0 else None
        filters: dict[str, Any] = {}
        if cutoff:
            filters["updated_on"] = f">={cutoff.date().isoformat()}"
        issues = await self.fetch_issues_by_assignee(
            assignee_id=int(assignee_id),
            status_id="open",
            limit=max_items,
            sort="updated_on:desc",
            filters=filters,
        )
        candidates = []
        for issue in issues:
            updated = _parse_dt(getattr(issue, "updated_on", None))
            if cutoff and (not updated or updated < cutoff):
                continue
            candidates.append(issue)

        def _obj_name(obj: Any) -> str:
            if isinstance(obj, dict):
                return str(obj.get("name") or "")
            return str(getattr(obj, "name", "") or obj or "")

        def _obj_email(obj: Any) -> str:
            if obj is None:
                return ""
            return str(getattr(obj, "mail", "") or getattr(obj, "email", "") or getattr(obj, "login", "") or "")

        def _detail(issue_id: int) -> dict[str, Any]:
            issue = self._redmine.issue.get(int(issue_id), include=["journals"])
            journals = []
            for item in getattr(issue, "journals", []) or []:
                details = []
                for detail in getattr(item, "details", []) or []:
                    details.append({
                        "property": str(getattr(detail, "property", "")),
                        "name": str(getattr(detail, "name", "")),
                        "old_value": str(getattr(detail, "old_value", "")),
                        "new_value": str(getattr(detail, "new_value", "")),
                    })
                journals.append({
                    "id": str(getattr(item, "id", "")),
                    "user": _obj_name(getattr(item, "user", None)),
                    "user_email": _obj_email(getattr(item, "user", None)),
                    "created_on": str(getattr(item, "created_on", "") or ""),
                    "notes": str(getattr(item, "notes", "") or "")[:2000],
                    "details": details,
                })
            status = getattr(issue, "status", None)
            priority = getattr(issue, "priority", None)
            assigned_to = getattr(issue, "assigned_to", None)
            return {
                "issue_id": int(getattr(issue, "id", 0) or 0),
                "subject": str(getattr(issue, "subject", "") or ""),
                "status_name": _obj_name(status),
                "priority_name": _obj_name(priority),
                "assigned_to_name": _obj_name(assigned_to),
                "created_on": str(getattr(issue, "created_on", "") or ""),
                "updated_on": str(getattr(issue, "updated_on", "") or ""),
                "closed_on": str(getattr(issue, "closed_on", "") or ""),
                "description": str(getattr(issue, "description", "") or ""),
                "journals_json": journals,
                "attachments_json": [],
                "failures_json": [],
                "is_resolved": 0,
                "last_scanned_at": datetime.now().isoformat(timespec="seconds"),
            }

        async def _one(issue: Any) -> dict[str, Any]:
            return await asyncio.to_thread(_detail, int(issue.id))

        semaphore = asyncio.Semaphore(8)

        async def _guarded(issue: Any) -> dict[str, Any]:
            async with semaphore:
                return await _one(issue)

        return [item for item in await asyncio.gather(*[_guarded(issue) for issue in candidates]) if item.get("issue_id")]

    async def count_issues_by_assignee(self, assignee_id: int) -> dict[str, int]:
        """Count all/open/closed issues assigned to a Redmine user id."""
        cache_key = int(assignee_id)
        cached = _ASSIGNEE_COUNT_CACHE.get(cache_key)
        if cached and time.time() - cached[0] < _CACHE_TTL_SECONDS:
            return dict(cached[1])

        def _count(status_id: str) -> int:
            result = self._redmine.issue.filter(
                assigned_to_id=int(assignee_id),
                status_id=status_id,
                limit=1,
            )
            list(result)
            return int(getattr(result, "total_count", 0) or 0)

        data = await asyncio.to_thread(lambda: {
            "total_owned": (total := _count("*")),
            "open_count": (open_count := _count("open")),
            "closed_count": max(0, total - open_count),
        })
        _ASSIGNEE_COUNT_CACHE[cache_key] = (time.time(), dict(data))
        return data

    async def resolved_trends_by_assignee(
        self,
        assignee_id: int,
        freshness_days: int = 180,
        limit: int = 5000,
    ) -> dict[str, list[dict[str, Any]]]:
        """Aggregate closed issue trends for a Redmine user, split by freshness.

        近 ``freshness_days`` 天关闭的工单每次实时拉取（``closed_on>=cutoff``
        服务端过滤，通常仅 1~2 页），结果进短期缓存（``_CACHE_TTL_SECONDS``）。
        更早关闭的工单趋势基本冻结，故拉全量一次后冻结进长期缓存
        （``_ASSIGNEE_TREND_HISTORICAL_CACHE``，7 天）。两段按 granularity
        合并后返回，与原单段返回结构一致。

        ``freshness_days`` 越大，历史段越小、实时段越大；设为 3650 即等同
        旧行为（全部实时）。任一段拉取失败时回退到另一段已有结果，保证
        Redmine 抖动时不至于整片空白。
        """
        granularity_fields = {
            "day": "resolved_daily",
            "week": "resolved_weekly",
            "month": "resolved_monthly",
            "year": "resolved_yearly",
        }
        label_keys = {"day": "date", "week": "week", "month": "month", "year": "year"}

        cache_key = int(assignee_id)
        cutoff_date = (datetime.now() - timedelta(days=int(freshness_days))).date()

        # 近期段与历史段共用同一份「按 closed_on 拉取并分桶」逻辑，差异仅在
        # 过滤条件、缓存表与 TTL；抽取为 _fetch_trend_segment 避免两份近似副本。
        recent_data = await self._fetch_trend_segment(
            cache_key,
            assignee_id=assignee_id,
            limit=limit,
            filters={"closed_on": f">={cutoff_date.isoformat()}"},
            keep_predicate=lambda closed_at: closed_at.date() >= cutoff_date,
            cache=_ASSIGNEE_TREND_CACHE,
            ttl=_CACHE_TTL_SECONDS,
            label="recent",
            granularity_fields=granularity_fields,
            label_keys=label_keys,
        )
        historical_data = await self._fetch_trend_segment(
            cache_key,
            assignee_id=assignee_id,
            limit=limit,
            # 服务端就过滤掉近期段，避免历史段重复扫描近期窗口后客户端再丢弃。
            filters={"closed_on": f"<{cutoff_date.isoformat()}"},
            keep_predicate=lambda closed_at: closed_at.date() < cutoff_date,
            cache=_ASSIGNEE_TREND_HISTORICAL_CACHE,
            ttl=_HISTORICAL_TTL_SECONDS,
            label="historical",
            granularity_fields=granularity_fields,
            label_keys=label_keys,
        )

        # ---- 合并两段 ----
        return self._merge_trend_fields(recent_data, historical_data, granularity_fields, label_keys)

    async def _fetch_trend_segment(
        self,
        cache_key: int,
        *,
        assignee_id: int,
        limit: int,
        filters: dict[str, Any],
        keep_predicate,
        cache: dict[int, tuple],
        ttl: float,
        label: str,
        granularity_fields: dict[str, str],
        label_keys: dict[str, str],
    ) -> dict[str, list[dict[str, Any]]]:
        """Fetch one closed-issue trend segment (cache-first) and bucket it.

        未命中缓存时按 ``filters`` 服务端过滤拉取，再按 ``keep_predicate`` 二次
        过滤（防御服务端过滤不支持该语法的部署），分桶后写入 ``cache``。拉取
        失败时返回空段（仍写缓存，TTL 由调用方决定：近期段短、历史段长）。
        """
        cached = cache.get(cache_key)
        if cached and time.time() - cached[0] < ttl:
            return {key: list(value) for key, value in cached[1].items()}

        buckets: dict[str, dict[str, int]] = {g: {} for g in granularity_fields}
        data: dict[str, list[dict[str, Any]]] = {field: [] for field in granularity_fields.values()}
        try:
            issues = await self.fetch_issues_by_assignee(
                assignee_id=int(assignee_id),
                status_id="closed",
                limit=limit,
                sort="closed_on:desc",
                filters=filters,
            )
            for issue in issues:
                closed_at = _parse_dt(getattr(issue, "closed_on", None)) or _parse_dt(
                    getattr(issue, "updated_on", None)
                )
                if not closed_at or not keep_predicate(closed_at):
                    continue
                self._bump_trend(buckets, closed_at, granularity_fields.keys())
            data = self._format_trend(buckets, granularity_fields, label_keys)
        except Exception as exc:
            logger.warning(
                "Redmine %s trend fetch failed for assignee %s: %s", label, assignee_id, exc, exc_info=True
            )
        cache[cache_key] = (time.time(), {key: list(value) for key, value in data.items()})
        return data

    @staticmethod
    def _bump_trend(
        buckets: dict[str, dict[str, int]],
        closed_at: datetime,
        granularities,
    ) -> None:
        for granularity in granularities:
            key = _time_key(closed_at, granularity)
            if key:
                buckets[granularity][key] = buckets[granularity].get(key, 0) + 1

    @staticmethod
    def _format_trend(
        buckets: dict[str, dict[str, int]],
        granularity_fields: dict[str, str],
        label_keys: dict[str, str],
    ) -> dict[str, list[dict[str, Any]]]:
        return {
            field: _sorted_slice(buckets[granularity], label_keys[granularity], 0)
            for granularity, field in granularity_fields.items()
        }

    @staticmethod
    def _merge_trend_fields(
        recent: dict[str, list[dict[str, Any]]],
        historical: dict[str, list[dict[str, Any]]],
        granularity_fields: dict[str, str],
        label_keys: dict[str, str],
    ) -> dict[str, list[dict[str, Any]]]:
        """Merge recent + historical trend rows per granularity, summing counts."""
        merged: dict[str, list[dict[str, Any]]] = {}
        for granularity, field in granularity_fields.items():
            label_key = label_keys[granularity]
            counts: dict[str, int] = {}
            for row in (historical.get(field) or []) + (recent.get(field) or []):
                label = str(row.get(label_key) or "").strip()
                if not label:
                    continue
                counts[label] = counts.get(label, 0) + int(row.get("count") or 0)
            merged[field] = _sorted_slice(counts, label_key, 0)
        return merged

    async def fetch_resolved_issues_by_assignee(
        self,
        assignee_id: int,
        start: str = "",
        end: str = "",
        limit: int = 2000,
    ) -> list[dict[str, Any]]:
        """Fetch closed issues for a Redmine user within [start, end).

        Returns normalized dicts so the caller does not need access to the raw
        python-redmine objects. Dates are compared as ISO string prefixes. This
        powers the department trend drill-down independent of the local DB sync
        scope (which only covers the configured sync user).
        """
        max_items = max(1, min(int(limit or 2000), 5000))
        filters: dict[str, Any] = {}
        if start or end:
            filters["closed_on"] = f"><{start or '1900-01-01'}|{end or '9999-12-31'}"
        issues = await self.fetch_issues_by_assignee(
            assignee_id=int(assignee_id),
            status_id="closed",
            limit=max_items,
            sort="closed_on:desc",
            filters=filters,
        )

        def _obj_name(obj: Any) -> str:
            if isinstance(obj, dict):
                return str(obj.get("name") or "")
            return str(getattr(obj, "name", "") or obj or "")

        result: list[dict[str, Any]] = []
        for issue in issues:
            closed_on = str(getattr(issue, "closed_on", "") or "")[:19]
            resolved_on = closed_on or str(getattr(issue, "updated_on", "") or "")[:19]
            if start and resolved_on[:10] < start:
                continue
            if end and resolved_on[:10] >= end:
                continue
            tracker = getattr(issue, "tracker", None)
            priority = getattr(issue, "priority", None)
            status = getattr(issue, "status", None)
            category = getattr(issue, "category", None)
            result.append({
                "issue_id": int(getattr(issue, "id", 0) or 0),
                "subject": str(getattr(issue, "subject", "") or ""),
                "status_name": _obj_name(status),
                "priority_name": _obj_name(priority),
                "assigned_to_name": _obj_name(getattr(issue, "assigned_to", None)),
                "closed_on": closed_on[:10],
                "resolved_on": resolved_on,
                "updated_on": str(getattr(issue, "updated_on", "") or "")[:19],
                "tracker_name": _obj_name(tracker),
                "category": _obj_name(category),
            })
            if len(result) >= max_items:
                break
        result.sort(key=lambda item: (item.get("resolved_on") or "", item.get("issue_id") or 0), reverse=True)
        return result

    async def discover_assignees_from_issues(
        self,
        limit: int = 2000,
        status_id: str = "*",
        sort: str = "updated_on:desc",
    ) -> list[dict[str, Any]]:
        """Discover assignable users from issue payloads when /users is forbidden."""
        limit = max(1, min(int(limit or 2000), 5000))
        page_size = min(limit, 100)

        def _fetch():
            users: dict[int, dict[str, Any]] = {}
            offset = 0
            fetched = 0
            while fetched < limit:
                issues = self._redmine.issue.filter(
                    status_id=status_id,
                    sort=sort,
                    limit=page_size,
                    offset=offset,
                )
                page = list(issues)
                if not page:
                    break
                fetched += len(page)
                for issue in page:
                    assigned = getattr(issue, "assigned_to", None)
                    user_id = int(getattr(assigned, "id", 0) or 0) if assigned else 0
                    if not user_id:
                        continue
                    users[user_id] = {
                        "id": user_id,
                        "name": str(getattr(assigned, "name", "") or assigned or ""),
                        "login": "",
                        "firstname": "",
                        "lastname": "",
                        "mail": "",
                    }
                if len(page) < page_size:
                    break
                offset += page_size
            return list(users.values())

        return await asyncio.to_thread(_fetch)

    async def search_assigned_issues(
        self,
        created_from: str,
        created_to: str,
        limit: int = 5,
        status_id: str = "*",
    ) -> list[Any]:
        """Fetch issues assigned to the authenticated user in a created_on range.

        Dates must be formatted as YYYY-MM-DD. Redmine date filters use the
        ><start|end syntax.
        """
        limit = max(1, min(int(limit or 5), 100))

        def _search():
            issues = self._redmine.issue.filter(
                assigned_to_id="me",
                status_id=status_id,
                created_on=f"><{created_from}|{created_to}",
                sort="created_on:desc",
                limit=limit,
            )
            return list(issues)

        return await asyncio.to_thread(_search)

    async def search_issues_by_subject(
        self,
        term: str,
        project_id: str = "fae",
        limit: int = 10,
        status_id: str = "*",
    ) -> list[dict[str, Any]]:
        """Search Redmine issues by subject using the Issues API."""
        term = (term or "").strip()
        if not term:
            return []
        limit = max(1, min(int(limit or 10), 50))

        def _search():
            issues = self._redmine.issue.filter(
                project_id=project_id,
                status_id=status_id,
                subject=f"~{term}",
                sort="updated_on:desc",
                limit=limit,
            )
            return [
                {
                    "issue_id": int(issue.id),
                    "subject": str(issue.subject or ""),
                    "status_name": str(getattr(issue, "status", None).name or ""),
                    "updated_on": str(issue.updated_on or ""),
                    "project_name": str(getattr(issue, "project", None).name or ""),
                }
                for issue in issues
            ]

        return await asyncio.to_thread(_search)
