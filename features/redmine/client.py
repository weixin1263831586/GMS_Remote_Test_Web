"""Redmine client facade.

Standard Redmine resources are read through python-redmine. File upload/download
and issue replies stay on aiohttp so large attachments can use explicit
timeouts, streaming-friendly APIs, and existing error handling.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urljoin, urlparse

import aiohttp
from redminelib import Redmine

from features.redmine.evidence.service import MAX_ATTACHMENT_BYTES, MAX_REDIRECTS, _attachment_path_ok, _same_origin
from features.redmine.users import RESOLVED_STATUS_NAMES, _parse_dt, _sorted_slice, _time_key
from features.redmine.utils import (
    COMPILED_ISSUE_LINK_PATTERN,
    build_redmine_download_url,
    create_basic_auth_header,
    extract_redmine_issue_id_from_text,
)
from foundation.cache import TTLCache


logger = logging.getLogger(__name__)

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


def clear_historical_trend_cache() -> None:
    """Reset both sides of the trend boundary after configuration changes."""
    _ASSIGNEE_TREND_CACHE.clear()
    _ASSIGNEE_TREND_HISTORICAL_CACHE.clear()


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

    def __init__(self, base_url: str, username: str = "", password: str = "", api_key: str = ""):
        self.base_url = (base_url or "").rstrip("/")
        self.username = username or ""
        self.password = password or ""
        self.api_key = api_key or ""
        # assignee 缓存的 owner 维度：端点+凭据的不可逆摘要（摘要只作
        # 分键，不落盘不外发），避免不同 owner 的客户端共享同 assignee_id
        # 的计数/趋势缓存。
        self._cache_owner_key = hashlib.sha256(
            json.dumps((self.base_url, self.username, self.api_key, self.password)).encode("utf-8")
        ).hexdigest()
        kwargs = {"key": self.api_key} if self.api_key else ({"username": self.username, "password": self.password} if self.username and self.password else {})
        # 显式超时：python-redmine 同步调用跑在 to_thread 里，无超时会在
        # Redmine 端卡死时永久占死 worker 线程。
        self._redmine = Redmine(self.base_url, requests={"timeout": 30}, **kwargs)
        self._session: aiohttp.ClientSession | None = None

    def _get_session(self) -> aiohttp.ClientSession:
        """Return a reusable aiohttp session (created on first use)."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=300),
            )
        return self._session

    async def close(self) -> None:
        """Close the underlying aiohttp session."""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    def auth_headers(self) -> dict[str, str]:
        # python-redmine 在有 API Key 时忽略密码。aiohttp 上传/下载/回复
        # 必须走同一优先级，否则仅配置 Key 的账号会匿名失败，或在两者
        # 同时存在时用密码用户发出写请求。
        api_key = str(getattr(self, "api_key", "") or "")
        if api_key:
            return {"X-Redmine-API-Key": api_key}
        if not (self.username and self.password):
            return {}
        return create_basic_auth_header(self.username, self.password)

    def download_url(self, attachment_id: str) -> str:
        return build_redmine_download_url(self.base_url, str(attachment_id))

    async def list_issue_attachments(self, issue_id: str) -> list[RedmineAttachment]:
        return await asyncio.to_thread(self._list_issue_attachments_redminelib, issue_id)

    def _list_issue_attachments_redminelib(self, issue_id: str) -> list[RedmineAttachment]:
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

    async def first_issue_attachment(self, issue_id: str) -> RedmineAttachment | None:
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
        # Redmine upload tokens authorize a later issue update.  Never include
        # the token (or the customer-supplied filename) in persistent logs.
        logger.info("[Redmine Upload] Attachment uploaded successfully")
        return token

    async def download_attachment(self, attachment_id: str, destination: str, content_url: str = "") -> int:
        """Download an attachment to destination using aiohttp.

        content_url 来自远端 JSON，视作不可信输入：与 evidence_fetch 的
        附件下载同一套守卫——同源、路径白名单（/attachments/...）、重定向
        ≤MAX_REDIRECTS 且每一跳重新校验、流式大小上限。
        """
        raw_url = content_url or self.download_url(attachment_id)
        if raw_url.startswith("/"):
            raw_url = urljoin(self.base_url + "/", raw_url)
        url = raw_url
        headers = self.auth_headers()
        headers.setdefault("User-Agent", "GMS Remote Test/1.0")
        total = 0
        redirects = 0
        session = self._get_session()
        while True:
            parsed = urlparse(url)
            if parsed.scheme not in {"http", "https"}:
                raise RuntimeError("Redmine attachment URL protocol not allowed")
            if not _same_origin(url, self.base_url):
                raise RuntimeError("Redmine attachment URL escapes configured origin")
            if not _attachment_path_ok(url):
                raise RuntimeError("Redmine attachment URL path is not an attachment download")
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=None, connect=10, sock_read=120), allow_redirects=False) as response:
                if response.status in (301, 302, 303, 307, 308):
                    location = response.headers.get("Location") or ""
                    if not location:
                        raise RuntimeError("Redmine attachment redirect missing Location")
                    url = urljoin(url, location)
                    redirects += 1
                    if redirects > MAX_REDIRECTS:
                        raise RuntimeError("Redmine attachment redirect limit exceeded")
                    continue
                if response.status not in (200, 206):
                    body = await response.text(errors="ignore")
                    raise RuntimeError(f"Redmine attachment download failed: HTTP {response.status} - {body[:300]}")
                with open(destination, "wb") as target:
                    async for chunk in response.content.iter_chunked(1024 * 1024):
                        if not chunk:
                            continue
                        if total + len(chunk) > MAX_ATTACHMENT_BYTES:
                            raise RuntimeError(
                                f"Redmine attachment download exceeded {MAX_ATTACHMENT_BYTES} bytes"
                            )
                        total += len(chunk)
                        target.write(chunk)
                break
        return total

    async def reply_issue(self, issue_id: str, notes: str, files: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        uploads = []
        for item in files or []:
            content = item.get("content") or b""
            if not content:
                continue
            filename = item.get("filename") or "attachment"
            content_type = item.get("content_type") or "application/octet-stream"
            token = await self.upload_file(content, filename, content_type)
            uploads.append({"token": token, "filename": filename, "content_type": content_type})

        payload_issue: dict[str, Any] = {"notes": notes}
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

    async def find_attachment_issue_id(self, attachment_id: str) -> str | None:
        """Find the issue ID that owns this attachment by querying the attachment detail page.

        Strategy:
        1. First try to get it from the final redirected URL (most reliable if Redmine redirects to issue page)
        2. Then parse HTML content to find the "parent" issue link
        3. Use multiple patterns to match common Redmine HTML structures
        """
        detail_url = f"{self.base_url}/attachments/{attachment_id}"
        headers = self.auth_headers()
        headers.setdefault("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")
        headers.setdefault("Accept", "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8")
        try:
            session = self._get_session()
            async with session.get(detail_url, headers=headers, timeout=aiohttp.ClientTimeout(total=30), allow_redirects=True) as response:
                final_url_issue_id = extract_redmine_issue_id_from_text(str(response.url))
                if final_url_issue_id:
                    logger.debug("[RedmineClient] Found issue ID from redirect URL: %s", final_url_issue_id)
                    return final_url_issue_id
                content_type = response.headers.get("Content-Type", "")
                if response.status != 200 or "html" not in content_type.lower():
                    logger.info("[RedmineClient] Attachment detail did not return HTML: %s status=%s type=%s", detail_url, response.status, content_type)
                    return None
                text = await response.text(errors="ignore")

                patterns = [
                    r'href=["\'][^"\']*/issues/(\d+)["\'][^>]*>\s*Show\s*<',  # "Show" link
                    r'<a[^>]+href=["\'][^"\']*/issues/(\d+)["\'][^>]*class=["\']*issue[^"\']*["\']',  # link with issue class
                    r'href=["\'][^"\']*/issues/(\d+)["\']',  # any issue link (fallback)
                ]
                for pattern in patterns:
                    match = re.search(pattern, text, re.IGNORECASE)
                    if match:
                        issue_id = match.group(1)
                        logger.debug("[RedmineClient] Found issue ID from HTML pattern %s: %s", pattern[:30] + "...", issue_id)
                        return issue_id

                # Fallback to generic search
                link_match = COMPILED_ISSUE_LINK_PATTERN.search(text)
                if link_match:
                    return link_match.group(1)
                return extract_redmine_issue_id_from_text(text)
        except Exception as e:
            logger.warning("[RedmineClient] Attachment issue lookup failed: %s, error=%s", detail_url, e)
            return None

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

    async def fetch_issue_subjects(self, issue_ids: list[int]) -> dict[int, str]:
        """Fetch only each issue's current subject (cheap display refresh).

        展示层标题同步入口：单号分析/晨报记录在入队时冻结了 subject，刷新时
        只需要拿回最新标题，不必抓 journals/附件等完整元数据。优先一次批量
        请求（Redmine /issues.json 的 issue_id 过滤），缺失项逐个兜底。
        """
        ids = sorted({int(i) for i in issue_ids if i})
        subjects: dict[int, str] = {}
        if not ids:
            return subjects

        def _fetch_batch() -> dict[int, str]:
            result: dict[int, str] = {}
            for issue in self._redmine.issue.filter(issue_id=ids, limit=len(ids)):
                result[int(getattr(issue, "id", 0) or 0)] = str(
                    getattr(issue, "subject", "") or ""
                ).strip()
            return result

        def _fetch_one(issue_id: int) -> str:
            issue = self._redmine.issue.get(int(issue_id))
            return str(getattr(issue, "subject", "") or "").strip()

        try:
            subjects.update(await asyncio.to_thread(_fetch_batch))
        except Exception:
            logger.warning("batch subject fetch failed", exc_info=True)
        for issue_id in ids:
            if issue_id in subjects:
                continue
            try:
                subjects[issue_id] = await asyncio.to_thread(_fetch_one, issue_id)
            except Exception:
                logger.warning("fetch subject failed for issue #%s", issue_id, exc_info=True)
        return subjects

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
            # redmine_user 是 Redmine 远端用户资源，非平台 CurrentUser，其 .id 与 owner 无关。
            remote_users = self._redmine.user.filter(name=term, limit=limit)
            return [
                {
                    "id": int(redmine_user.id),
                    "login": str(getattr(redmine_user, "login", "") or ""),
                    "firstname": str(getattr(redmine_user, "firstname", "") or ""),
                    "lastname": str(getattr(redmine_user, "lastname", "") or ""),
                    "mail": str(getattr(redmine_user, "mail", "") or ""),
                    "name": f"{getattr(redmine_user, 'firstname', '')} {getattr(redmine_user, 'lastname', '')}".strip(),
                }
                for redmine_user in remote_users
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

        results = await asyncio.gather(
            *[_guarded(issue) for issue in candidates],
            return_exceptions=True,
        )
        snapshots: list[dict[str, Any]] = []
        for item in results:
            # 单条 issue 的 403/网络错只应丢掉该条快照，不能拖垮整批。
            if isinstance(item, BaseException):
                logger.warning("assignee 快照抓取单条失败，已跳过: %s", item)
                continue
            if item.get("issue_id"):
                snapshots.append(item)
        return snapshots

    async def count_issues_by_assignee(self, assignee_id: int) -> dict[str, int]:
        """Count all/open/closed issues assigned to a Redmine user id."""
        cache_key = (self._cache_owner_key, int(assignee_id))
        cached = _ASSIGNEE_COUNT_CACHE.get(cache_key)
        if cached is not None:
            return dict(cached)

        def _count(status_id: str) -> int:
            result = self._redmine.issue.filter(
                assigned_to_id=int(assignee_id),
                status_id=status_id,
                limit=1,
            )
            list(result)
            return int(getattr(result, "total_count", 0) or 0)

        async def load():
            return await asyncio.to_thread(lambda: {
                "total_owned": (total := _count("*")),
                "open_count": (open_count := _count("open")),
                "closed_count": max(0, total - open_count),
            })

        return dict(await _ASSIGNEE_COUNT_CACHE.get_or_load_async(cache_key, load))

    async def resolved_trends_by_assignee(
        self,
        assignee_id: int,
        freshness_days: int = 180,
        limit: int = 5000,
    ) -> dict[str, list[dict[str, Any]]]:
        """按数据新鲜度聚合用户已关闭工单趋势。

        近期数据使用短期缓存，较早数据使用七天缓存；任一查询失败时返回
        另一部分可用结果。
        """
        granularity_fields = {
            "day": "resolved_daily",
            "week": "resolved_weekly",
            "month": "resolved_monthly",
            "year": "resolved_yearly",
        }
        label_keys = {"day": "date", "week": "week", "month": "month", "year": "year"}

        cutoff_date = (datetime.now() - timedelta(days=int(freshness_days))).date()

        # 近期和较早数据共用区间查询，并用谓词再次校验边界。
        cutoff_iso = cutoff_date.isoformat()
        cache_key = (self._cache_owner_key, int(assignee_id), cutoff_iso, int(limit))
        recent_data = await self._fetch_trend_segment(
            cache_key,
            assignee_id=assignee_id,
            limit=limit,
            filters={"closed_on": f"><{cutoff_iso}|9999-12-31"},
            keep_predicate=lambda closed_at: closed_at.date() >= cutoff_date,
            cache=_ASSIGNEE_TREND_CACHE,
            label="recent",
            granularity_fields=granularity_fields,
            label_keys=label_keys,
        )
        historical_data = await self._fetch_trend_segment(
            cache_key,
            assignee_id=assignee_id,
            limit=limit,
            # 服务端就过滤掉近期段，避免历史段重复扫描近期窗口后客户端再丢弃。
            filters={"closed_on": f"><1900-01-01|{cutoff_iso}"},
            keep_predicate=lambda closed_at: closed_at.date() < cutoff_date,
            cache=_ASSIGNEE_TREND_HISTORICAL_CACHE,
            label="historical",
            granularity_fields=granularity_fields,
            label_keys=label_keys,
        )

        return self._merge_trend_fields(recent_data, historical_data, granularity_fields, label_keys)

    async def _fetch_trend_segment(
        self,
        cache_key: tuple[str, int, str, int],
        *,
        assignee_id: int,
        limit: int,
        filters: dict[str, Any],
        keep_predicate,
        cache: TTLCache,
        label: str,
        granularity_fields: dict[str, str],
        label_keys: dict[str, str],
    ) -> dict[str, list[dict[str, Any]]]:
        """Fetch one closed-issue trend segment (cache-first) and bucket it.

        未命中缓存时按 ``filters`` 服务端过滤拉取，再按 ``keep_predicate`` 二次
        过滤（防御服务端过滤不支持该语法的部署），分桶后写入 ``cache``。拉取
        失败时返回空段但**不写缓存**——避免把瞬态失败（过滤语法/网络/5xx）冻结
        成一份长期空数据；返回的空段仍能让合并逻辑走另一段兜底的可用性语义。
        """
        cached = cache.get(cache_key)
        if cached is not None:
            return copy.deepcopy(cached)

        async def load():
            buckets: dict[str, dict[str, int]] = {g: {} for g in granularity_fields}
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
            return self._format_trend(buckets, granularity_fields, label_keys)

        try:
            return copy.deepcopy(await cache.get_or_load_async(cache_key, load))
        except Exception as exc:
            # 拉取失败时不写缓存：服务端过滤语法错误、网络抖动、Redmine 5xx 等
            # 一旦把空段冻结进缓存（历史段 TTL 长达 7 天），后续请求会持续命中
            # 这份空数据，等于把一次瞬态失败放大成长期空段（正是本次 bug 的形态）。
            # 返回空段仍能让 _merge_trend_fields 走「另一段兜底」的可用性语义。
            logger.warning(
                "Redmine %s trend fetch failed for assignee %s: %s", label, assignee_id, exc, exc_info=True
            )
            return {field: [] for field in granularity_fields.values()}

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
        project_id: str | None = None,
        limit: int = 10,
        status_id: str = "*",
    ) -> list[dict[str, Any]]:
        """Search Redmine issues by subject using the Issues API."""
        term = (term or "").strip()
        if not term:
            return []
        limit = max(1, min(int(limit or 10), 50))

        def _search():
            def _name(obj: Any) -> str:
                return str(getattr(obj, "name", "") or obj or "")

            # Omit project_id to search every project visible to the authenticated
            # Redmine account. A hard-coded "fae" silently excluded other SDK/GMS
            # projects and made cross-project similarity searches incomplete.
            params = {
                "status_id": status_id,
                "subject": f"~{term}",
                "sort": "updated_on:desc",
                "limit": limit,
            }
            if project_id and str(project_id).strip():
                params["project_id"] = str(project_id).strip()
            issues = self._redmine.issue.filter(**params)
            return [
                {
                    "issue_id": int(issue.id),
                    "subject": str(getattr(issue, "subject", "") or ""),
                    "status_name": _name(getattr(issue, "status", None)),
                    "updated_on": str(getattr(issue, "updated_on", "") or ""),
                    "project_name": _name(getattr(issue, "project", None)),
                }
                for issue in issues
            ]

        return await asyncio.to_thread(_search)
