"""Redmine URL, attachment and naming helpers."""

import base64
import re
import urllib.parse
from datetime import datetime
from pathlib import PurePath
from typing import Any


REDMINE_ISSUE_PATTERN = r'/issues/(\d+)'
REDMINE_ATTACHMENT_PATTERN = r'/attachments/(?:download/)?(\d+)'

COMPILED_REDMINE_ISSUE_PATTERN = re.compile(REDMINE_ISSUE_PATTERN)
COMPILED_REDMINE_ATTACHMENT_PATTERN = re.compile(REDMINE_ATTACHMENT_PATTERN)
COMPILED_REPORT_NAME_PATTERN = re.compile(r'Redmine-(\d+)-(.+)')
COMPILED_CONTENT_DISPOSITION_PATTERN = re.compile(
    r"filename\*=UTF-8''([^\;]+)|filename=\"([^\"]+)\"|filename=([^\s;]+)"
)
COMPILED_ISSUE_LINK_PATTERN = re.compile(r'href=["\'][^"\']*/issues/(\d+)[^"\']*["\']')
SAFE_ATTACHMENT_FILENAME_RE = re.compile(r"[^A-Za-z0-9._ -]+")
MAX_ATTACHMENT_FILENAME_LENGTH = 180


_REDMINE_DATE_FORMATS = (
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S.%f",
    "%Y-%m-%d %H:%M:%S.%f",
    "%Y-%m-%d",
)


def parse_iso(value: Any) -> datetime | None:
    """Parse a Redmine timestamp into a naive ``datetime`` (or ``None``).

    Accepts datetimes, ISO strings, and the space-separated
    ``"YYYY-MM-DD HH:MM:SS"`` form python-redmine often returns.
    """
    if isinstance(value, datetime):
        return value.replace(tzinfo=None) if value.tzinfo else value
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    for fmt in _REDMINE_DATE_FORMATS:
        try:
            return datetime.strptime(text.replace("Z", ""), fmt)
        except ValueError:
            continue
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=None) if parsed.tzinfo else parsed
    except ValueError:
        return None


def to_iso8601(value: Any) -> str:
    """Normalize a Redmine timestamp to an ISO 8601 string (``T`` separator).

    Keeping the stored form consistent lets list views sort and compare
    timestamps uniformly regardless of whether Redmine returned a datetime or
    a space-separated string.
    """
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    text = str(value).strip()
    if not text:
        return ""
    parsed = parse_iso(text)
    if parsed is not None:
        return parsed.isoformat(timespec="seconds")
    # 时间格式有效时将空格分隔符规范为 T。
    return text.replace(" ", "T", 1) if len(text) >= 10 and text[4:5] == "-" else text


def create_basic_auth_header(username: str, password: str) -> dict[str, str]:
    """Create a Basic Authentication header."""
    credentials = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {'Authorization': f'Basic {credentials}'}


def build_redmine_download_url(base_url: str, attachment_id: str) -> str:
    """Build the canonical Redmine attachment download URL."""
    return f"{base_url}/attachments/download/{attachment_id}/"


def extract_filename_from_content_disposition(content_disposition: str) -> str | None:
    """Extract filename from a Content-Disposition header."""
    if not content_disposition:
        return None
    match = COMPILED_CONTENT_DISPOSITION_PATTERN.search(content_disposition)
    if not match:
        return None
    filename = match.group(1) or match.group(2) or match.group(3)
    return urllib.parse.unquote(filename) if filename else None


def sanitize_attachment_filename(filename: str | None, default: str = "attachment") -> str:
    """Return a filesystem/header safe Redmine attachment basename."""
    name = str(filename or "").strip()
    name = PurePath(name.replace("\\", "/")).name.strip()
    name = SAFE_ATTACHMENT_FILENAME_RE.sub("_", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    if not name or name in {".", ".."}:
        name = default
    if len(name) > MAX_ATTACHMENT_FILENAME_LENGTH:
        stem, dot, suffix = name.rpartition(".")
        if dot and stem:
            suffix = suffix[:32]
            max_stem = MAX_ATTACHMENT_FILENAME_LENGTH - len(suffix) - 1
            name = f"{stem[:max_stem]}.{suffix}"
        else:
            name = name[:MAX_ATTACHMENT_FILENAME_LENGTH]
        name = name.rstrip(" .") or default
    return name


def attachment_content_disposition(filename: str | None) -> str:
    """Build a safe Content-Disposition header with an RFC 5987 filename field."""
    safe_name = sanitize_attachment_filename(filename)
    # Header 文件名仅保留 ASCII 安全字符。
    ascii_name = safe_name.encode("ascii", "ignore").decode("ascii") or "attachment"
    encoded_name = urllib.parse.quote(safe_name, safe="")
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{encoded_name}"


def extract_redmine_issue_id_from_text(text: str) -> str | None:
    """Extract a Redmine issue id from URL or dropped HTML/text context."""
    if not text:
        return None
    match = COMPILED_REDMINE_ISSUE_PATTERN.search(text)
    return match.group(1) if match else None


def strip_redmine_report_prefix(filename: str) -> str:
    """Remove an existing Redmine-{issue}- prefix before applying the current issue prefix."""
    match = COMPILED_REPORT_NAME_PATTERN.match(filename or '')
    return match.group(2) if match else (filename or 'downloaded_file.zip')
