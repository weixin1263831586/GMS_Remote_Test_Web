"""Redmine URL, attachment and naming helpers."""

import base64
import logging
import re
import urllib.parse
from typing import Dict, Optional

import aiohttp

logger = logging.getLogger(__name__)

REDMINE_ISSUE_PATTERN = r'/issues/(\d+)'
REDMINE_ATTACHMENT_PATTERN = r'/attachments/(?:download/)?(\d+)'

COMPILED_REDMINE_ISSUE_PATTERN = re.compile(REDMINE_ISSUE_PATTERN)
COMPILED_REDMINE_ATTACHMENT_PATTERN = re.compile(REDMINE_ATTACHMENT_PATTERN)
COMPILED_REPORT_NAME_PATTERN = re.compile(r'Redmine-(\d+)-(.+)')
COMPILED_CONTENT_DISPOSITION_PATTERN = re.compile(
    r"filename\*=UTF-8''([^\;]+)|filename=\"([^\"]+)\"|filename=([^\s;]+)"
)
COMPILED_ISSUE_LINK_PATTERN = re.compile(r'href=["\'][^"\']*/issues/(\d+)[^"\']*["\']')


def create_basic_auth_header(username: str, password: str) -> Dict[str, str]:
    """Create a Basic Authentication header."""
    credentials = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {'Authorization': f'Basic {credentials}'}


def build_redmine_download_url(base_url: str, attachment_id: str) -> str:
    """Build the canonical Redmine attachment download URL."""
    return f"{base_url}/attachments/download/{attachment_id}/"


def extract_filename_from_content_disposition(content_disposition: str) -> Optional[str]:
    """Extract filename from a Content-Disposition header."""
    if not content_disposition:
        return None
    filename_match = COMPILED_CONTENT_DISPOSITION_PATTERN.search(content_disposition)
    if filename_match:
        filename = filename_match.group(1) or filename_match.group(2) or filename_match.group(3)
        return urllib.parse.unquote(filename) if filename else None
    return None


def extract_redmine_issue_id_from_text(text: str) -> Optional[str]:
    """Extract a Redmine issue id from URL or dropped HTML/text context."""
    if not text:
        return None
    match = COMPILED_REDMINE_ISSUE_PATTERN.search(text)
    return match.group(1) if match else None


def strip_redmine_report_prefix(filename: str) -> str:
    """Remove an existing Redmine-{issue}- prefix before applying the current issue prefix."""
    match = COMPILED_REPORT_NAME_PATTERN.match(filename or '')
    return match.group(2) if match else (filename or 'downloaded_file.zip')


async def fetch_redmine_attachment_issue_id(base_url: str, attachment_id: str, headers: Dict[str, str]) -> Optional[str]:
    """Best-effort lookup of the issue page linked by a Redmine attachment detail page."""
    detail_url = f"{base_url}/attachments/{attachment_id}"
    request_headers = dict(headers or {})
    request_headers.setdefault('User-Agent', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36')
    request_headers.setdefault('Accept', 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8')

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(detail_url, headers=request_headers, timeout=aiohttp.ClientTimeout(total=30), allow_redirects=True) as response:
                final_url_issue_id = extract_redmine_issue_id_from_text(str(response.url))
                if final_url_issue_id:
                    return final_url_issue_id

                content_type = response.headers.get('Content-Type', '')
                if response.status != 200 or 'html' not in content_type.lower():
                    logger.info(f"[Report Analysis] 附件详情页未返回HTML: {detail_url}, status={response.status}, type={content_type}")
                    return None

                text = await response.text(errors='ignore')
                link_match = COMPILED_ISSUE_LINK_PATTERN.search(text)
                if link_match:
                    return link_match.group(1)
                return extract_redmine_issue_id_from_text(text)
    except Exception as e:
        logger.warning(f"[Report Analysis] 查询附件详情页失败: {detail_url}, error={e}")
        return None
