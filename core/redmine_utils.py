"""Redmine URL, attachment and naming helpers."""

import base64
import re
import urllib.parse
from typing import Dict, Optional

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
    match = COMPILED_CONTENT_DISPOSITION_PATTERN.search(content_disposition)
    if not match:
        return None
    filename = match.group(1) or match.group(2) or match.group(3)
    return urllib.parse.unquote(filename) if filename else None


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
