from __future__ import annotations

from collections.abc import Callable
from typing import Any

from features.redmine.client import RedmineClient
from features.redmine.config import config_manager as redmine_config_manager
from features.redmine.utils import (
    COMPILED_REDMINE_ATTACHMENT_PATTERN,
    COMPILED_REDMINE_ISSUE_PATTERN,
    COMPILED_REPORT_NAME_PATTERN,
    REDMINE_ISSUE_PATTERN,
    create_basic_auth_header,
    extract_filename_from_content_disposition,
    extract_redmine_issue_id_from_text,
    strip_redmine_report_prefix,
)


__all__ = [
    "COMPILED_REDMINE_ATTACHMENT_PATTERN",
    "COMPILED_REDMINE_ISSUE_PATTERN",
    "COMPILED_REPORT_NAME_PATTERN",
    "REDMINE_ISSUE_PATTERN",
    "RedmineClient",
    "ReportToRedmineWorkflow",
    "create_basic_auth_header",
    "extract_filename_from_content_disposition",
    "extract_redmine_issue_id_from_text",
    "redmine_config_manager",
    "strip_redmine_report_prefix",
]

class ReportToRedmineWorkflow:
    """Publish report artifacts and a reply through a Redmine client."""

    def __init__(self, client_factory: Callable[[], Any]):
        self.client_factory = client_factory

    async def publish(
        self,
        *,
        issue_id: str,
        notes: str,
        files: list[dict[str, Any]],
    ) -> dict[str, Any]:
        client = self.client_factory()
        uploads = []
        try:
            for item in files:
                filename = str(item.get("filename") or "attachment.bin")
                content_type = str(
                    item.get("content_type")
                    or "application/octet-stream"
                )
                token = await client.upload_file(
                    item.get("content") or b"",
                    filename,
                    content_type,
                )
                uploads.append(
                    {
                        "token": token,
                        "filename": filename,
                        "content_type": content_type,
                    }
                )
            return await client.reply_issue(issue_id, notes, uploads)
        finally:
            await client.close()
