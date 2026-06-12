"""Redmine client facade.

Standard Redmine resources are read through python-redmine. File upload/download
and issue replies stay on aiohttp so large attachments can use explicit
timeouts, streaming-friendly APIs, and existing error handling.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import aiohttp
from redminelib import Redmine

from core.redmine_utils import (
    COMPILED_ISSUE_LINK_PATTERN,
    build_redmine_download_url,
    create_basic_auth_header,
    extract_redmine_issue_id_from_text,
)
from core.redmine_agent_db import _parse_dt, _time_key, _sorted_slice

logger = logging.getLogger(__name__)
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


class RedmineClient:
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
        self._session: Optional[aiohttp.ClientSession] = None

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

    async def get_issue(self, issue_id: str, include: Optional[List[str]] = None) -> Any:
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

    async def list_issue_journals(self, issue_id: str) -> List[Any]:
        """Fetch issue journals through python-redmine."""
        issue = await self.get_issue(issue_id, include=["journals"])
        return list(getattr(issue, "journals", []) or [])

    async def get_current_user(self) -> Any:
        """Fetch the authenticated Redmine user."""
        return await asyncio.to_thread(self._redmine.user.get, "current")

    async def search_users(self, term: str, limit: int = 10) -> List[Dict[str, Any]]:
        """Search Redmine users by name/login/mail."""
        term = (term or "").strip()
        if not term:
            return []
        limit = max(1, min(int(limit or 10), 50))

        def _search():
            users = self._redmine.user.filter(name=term, limit=limit)
            return [
                {
                    "id": int(getattr(user, "id")),
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
    ) -> List[Any]:
        """Fetch ALL issues assigned to the authenticated user (no date window).

        Use this to build a complete local database of assigned issues.
        """
        return await self._paginate_issues("me", status_id, limit, sort)

    async def fetch_issues_by_assignee(
        self,
        assignee_id: int,
        status_id: str = "*",
        limit: int = 1000,
        sort: str = "updated_on:desc",
    ) -> List[Any]:
        """Fetch issues assigned to a specific Redmine user id."""
        return await self._paginate_issues(int(assignee_id), status_id, limit, sort)

    async def _paginate_issues(
        self,
        assigned_to_id: Any,
        status_id: str = "*",
        limit: int = 1000,
        sort: str = "updated_on:desc",
    ) -> List[Any]:
        """Shared paginated issue fetcher for both 'me' and specific assignee."""
        limit = max(1, min(int(limit or 1000), 5000))
        page_size = min(limit, 100)

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
                )
                page = list(issues)
                if not page:
                    break
                added = 0
                for issue in page:
                    issue_id = int(getattr(issue, "id"))
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

    async def count_issues_by_assignee(self, assignee_id: int) -> Dict[str, int]:
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

    async def resolved_trends_by_assignee(self, assignee_id: int, limit: int = 5000) -> Dict[str, List[Dict[str, Any]]]:
        """Aggregate closed issue trends for a Redmine user from issue stubs."""
        cache_key = int(assignee_id)
        cached = _ASSIGNEE_TREND_CACHE.get(cache_key)
        if cached and time.time() - cached[0] < _CACHE_TTL_SECONDS:
            return {key: list(value) for key, value in cached[1].items()}

        issues = await self.fetch_issues_by_assignee(
            assignee_id=int(assignee_id),
            status_id="closed",
            limit=limit,
            sort="closed_on:desc",
        )
        buckets: Dict[str, Dict[str, int]] = {"day": {}, "week": {}, "month": {}, "year": {}}

        for issue in issues:
            closed_at = _parse_dt(getattr(issue, "closed_on", None)) or _parse_dt(getattr(issue, "updated_on", None))
            if not closed_at:
                continue
            for granularity in ("day", "week", "month", "year"):
                key = _time_key(closed_at, granularity)
                if key:
                    buckets[granularity][key] = buckets[granularity].get(key, 0) + 1

        data = {
            "resolved_daily": _sorted_slice(buckets["day"], "date", 90),
            "resolved_weekly": _sorted_slice(buckets["week"], "week", 52),
            "resolved_monthly": _sorted_slice(buckets["month"], "month", 24),
            "resolved_yearly": _sorted_slice(buckets["year"], "year", 10),
        }
        _ASSIGNEE_TREND_CACHE[cache_key] = (time.time(), {key: list(value) for key, value in data.items()})
        return data

    async def discover_assignees_from_issues(
        self,
        limit: int = 2000,
        status_id: str = "*",
        sort: str = "updated_on:desc",
    ) -> List[Dict[str, Any]]:
        """Discover assignable users from issue payloads when /users is forbidden."""
        limit = max(1, min(int(limit or 2000), 5000))
        page_size = min(limit, 100)

        def _fetch():
            users: Dict[int, Dict[str, Any]] = {}
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
    ) -> List[Any]:
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
    ) -> List[Dict[str, Any]]:
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
                    "issue_id": int(getattr(issue, "id")),
                    "subject": str(getattr(issue, "subject") or ""),
                    "status_name": str(getattr(getattr(issue, "status", None), "name") or ""),
                    "updated_on": str(getattr(issue, "updated_on") or ""),
                    "project_name": str(getattr(getattr(issue, "project", None), "name") or ""),
                }
                for issue in issues
            ]

        return await asyncio.to_thread(_search)

    def auth_headers(self) -> Dict[str, str]:
        if not (self.username and self.password):
            return {}
        return create_basic_auth_header(self.username, self.password)

    def download_url(self, attachment_id: str) -> str:
        return build_redmine_download_url(self.base_url, str(attachment_id))

    async def list_issue_attachments(self, issue_id: str) -> List[RedmineAttachment]:
        return await asyncio.to_thread(self._list_issue_attachments_redminelib, issue_id)

    def _list_issue_attachments_redminelib(self, issue_id: str) -> List[RedmineAttachment]:
        issue = self._redmine.issue.get(int(issue_id), include=["attachments"])
        attachments = []
        for item in getattr(issue, "attachments", []) or []:
            attachment_id = str(getattr(item, "id", "") or "")
            if not attachment_id:
                continue
            attachments.append(
                RedmineAttachment(
                    id=attachment_id,
                    filename=str(getattr(item, "filename", "") or f"attachment_{attachment_id}"),
                    content_url=str(getattr(item, "content_url", "") or self.download_url(attachment_id)),
                    content_type=str(getattr(item, "content_type", "") or ""),
                    filesize=int(getattr(item, "filesize", 0) or 0),
                )
            )
        return attachments

    async def first_issue_attachment(self, issue_id: str) -> Optional[RedmineAttachment]:
        attachments = await self.list_issue_attachments(issue_id)
        return attachments[0] if attachments else None

    async def upload_file(self, file_content: bytes, filename: str, content_type: str = "application/octet-stream") -> str:
        url = f"{self.base_url}/uploads.json"
        headers = self.auth_headers()
        headers["Content-Type"] = "application/octet-stream"
        headers["User-Agent"] = "GMS Remote Test/1.0"

        session = self._get_session()
        async with session.post(url, data=file_content, headers=headers, timeout=aiohttp.ClientTimeout(total=120)) as response:
                if response.status not in (200, 201):
                    error_body = await response.text()
                    raise RuntimeError(f"Redmine upload failed: HTTP {response.status} - {error_body}")
                result = await response.json()

        token = (result.get("upload") or {}).get("token", "")
        if not token:
            raise RuntimeError(f"Redmine upload returned no token: {result}")
        logger.info("[Redmine Upload] File '%s' uploaded, token=%s...", filename, token[:16])
        return token

    async def download_attachment(self, attachment_id: str, destination: str, content_url: str = "") -> int:
        """Download an attachment to destination using aiohttp."""
        url = content_url or self.download_url(attachment_id)
        headers = self.auth_headers()
        headers.setdefault("User-Agent", "GMS Remote Test/1.0")
        total = 0
        session = self._get_session()
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=180), allow_redirects=True) as response:
                if response.status not in (200, 206):
                    body = await response.text(errors="ignore")
                    raise RuntimeError(f"Redmine attachment download failed: HTTP {response.status} - {body[:300]}")
                with open(destination, "wb") as target:
                    async for chunk in response.content.iter_chunked(1024 * 1024):
                        if not chunk:
                            continue
                        target.write(chunk)
                        total += len(chunk)
        return total

    async def reply_issue(self, issue_id: str, notes: str, files: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        uploads = []
        for item in files or []:
            content = item.get("content") or b""
            if not content:
                continue
            filename = item.get("filename") or "attachment"
            content_type = item.get("content_type") or "application/octet-stream"
            token = await self.upload_file(content, filename, content_type)
            uploads.append({"token": token, "filename": filename, "content_type": content_type})

        payload_issue: Dict[str, Any] = {"notes": notes}
        if uploads:
            payload_issue["uploads"] = uploads
        payload = {"issue": payload_issue}

        headers = self.auth_headers()
        headers["Content-Type"] = "application/json"
        headers["User-Agent"] = "GMS Remote Test/1.0"
        api_url = f"{self.base_url}/issues/{issue_id}.json"
        session = self._get_session()
        async with session.put(api_url, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=30)) as response:
                if response.status in (200, 204):
                    return {"issue_url": f"{self.base_url}/issues/{issue_id}", "attachments": len(uploads)}
                error_body = await response.text()
                raise RuntimeError(f"Redmine API returned HTTP {response.status}: {error_body}")

    async def find_attachment_issue_id(self, attachment_id: str) -> Optional[str]:
        detail_url = f"{self.base_url}/attachments/{attachment_id}"
        headers = self.auth_headers()
        headers.setdefault("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")
        headers.setdefault("Accept", "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8")
        try:
            session = self._get_session()
            async with session.get(detail_url, headers=headers, timeout=aiohttp.ClientTimeout(total=30), allow_redirects=True) as response:
                    final_url_issue_id = extract_redmine_issue_id_from_text(str(response.url))
                    if final_url_issue_id:
                        return final_url_issue_id
                    content_type = response.headers.get("Content-Type", "")
                    if response.status != 200 or "html" not in content_type.lower():
                        logger.info("[RedmineClient] Attachment detail did not return HTML: %s status=%s type=%s", detail_url, response.status, content_type)
                        return None
                    text = await response.text(errors="ignore")
        except Exception as e:
            logger.warning("[RedmineClient] Attachment issue lookup failed: %s, error=%s", detail_url, e)
            return None

        link_match = COMPILED_ISSUE_LINK_PATTERN.search(text)
        if link_match:
            return link_match.group(1)
        return extract_redmine_issue_id_from_text(text)
