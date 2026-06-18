from __future__ import annotations

from collections.abc import Callable
from typing import Any


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
