from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp

from .models import RedmineAttachment
from .utils import (
    COMPILED_ISSUE_LINK_PATTERN,
    build_redmine_download_url,
    create_basic_auth_header,
    extract_redmine_issue_id_from_text,
)


logger = logging.getLogger(__name__)


class RedmineAttachmentMixin:
    def auth_headers(self) -> dict[str, str]:
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
                link_match = COMPILED_ISSUE_LINK_PATTERN.search(text)
                if link_match:
                    return link_match.group(1)
                return extract_redmine_issue_id_from_text(text)
        except Exception as e:
            logger.warning("[RedmineClient] Attachment issue lookup failed: %s, error=%s", detail_url, e)
            return None
