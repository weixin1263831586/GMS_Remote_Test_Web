from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from features.redmine.attachments import RedmineAttachmentMixin


class _UploadResponse:
    status = 201

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def json(self):
        return {"upload": {"token": "redmine-secret-upload-token-value"}}


class _UploadSession:
    def post(self, *args, **kwargs):
        return _UploadResponse()


class _AttachmentClient(RedmineAttachmentMixin):
    base_url = "https://redmine.example.test"
    username = "operator"
    password = "credential"

    def _get_session(self):
        return _UploadSession()


class RedmineAttachmentSecurityTests(unittest.IsolatedAsyncioTestCase):
    async def test_success_log_never_contains_upload_token_or_filename(self):
        filename = "customer-confidential-report.zip"
        token = "redmine-secret-upload-token-value"

        with self.assertLogs("features.redmine.attachments", level="INFO") as logs:
            returned = await _AttachmentClient().upload_file(b"report", filename)

        output = "\n".join(logs.output)
        self.assertEqual(returned, token)
        self.assertNotIn(token, output)
        self.assertNotIn(token[:16], output)
        self.assertNotIn(filename, output)


class _DownloadResponse:
    def __init__(self, status=200, location="", chunks=(), body=""):
        self.status = status
        self.headers = {"Location": location} if location else {}
        self._chunks = list(chunks)
        self._body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def text(self, errors="ignore"):
        return self._body

    class _Stream:
        def __init__(self, chunks):
            self._chunks = chunks

        async def iter_chunked(self, _size):
            for chunk in self._chunks:
                yield chunk

    @property
    def content(self):
        return self._Stream(self._chunks)


class _DownloadSession:
    """按 URL 顺序返回预设响应；记录每次请求的 URL 与参数。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests: list[tuple[str, dict]] = []

    def get(self, url, **kwargs):
        self.requests.append((url, kwargs))
        return self.responses.pop(0)


class _DownloadClient(RedmineAttachmentMixin):
    base_url = "https://redmine.example.test"
    username = "operator"
    password = "credential"

    def __init__(self, responses):
        self._session = _DownloadSession(responses)

    def _get_session(self):
        return self._session


class DownloadAttachmentGuardTests(unittest.IsolatedAsyncioTestCase):
    async def _download(self, responses, content_url=""):
        with tempfile.TemporaryDirectory() as tmp:
            client = _DownloadClient(responses)
            destination = Path(tmp) / "attachment.bin"
            total = await client.download_attachment("42", str(destination), content_url=content_url)
            return client, total, destination.read_bytes() if destination.exists() else None

    async def test_rejects_cross_origin_content_url(self):
        with self.assertRaises(RuntimeError) as ctx:
            await self._download([], content_url="https://evil.example.test/attachments/download/42")
        self.assertIn("escapes configured origin", str(ctx.exception))

    async def test_rejects_non_attachment_path(self):
        with self.assertRaises(RuntimeError) as ctx:
            await self._download(
                [], content_url="https://redmine.example.test/admin/settings?attachments=download"
            )
        self.assertIn("not an attachment download", str(ctx.exception))

    async def test_rejects_non_http_scheme(self):
        with self.assertRaises(RuntimeError) as ctx:
            await self._download([], content_url="file:///etc/passwd")
        self.assertIn("protocol not allowed", str(ctx.exception))

    async def test_follows_at_most_three_same_origin_redirects(self):
        redirect = _DownloadResponse(
            status=302, location="https://redmine.example.test/attachments/download/43"
        )
        with self.assertRaises(RuntimeError) as ctx:
            await self._download([redirect] * 5)
        self.assertIn("redirect limit exceeded", str(ctx.exception))

    async def test_redirect_to_other_origin_is_rejected(self):
        redirect = _DownloadResponse(status=302, location="https://evil.example.test/attachments/download/42")
        with self.assertRaises(RuntimeError) as ctx:
            await self._download([redirect])
        self.assertIn("escapes configured origin", str(ctx.exception))

    async def test_stream_size_cap_aborts_download(self):
        ok = _DownloadResponse(status=200, chunks=[b"a" * 16, b"b" * 16])
        with patch("features.redmine.attachments.ATTACHMENT_MAX_BYTES", 16), \
                self.assertRaises(RuntimeError) as ctx:
            await self._download([ok])
        self.assertIn("exceeds size limit", str(ctx.exception))

    async def test_happy_path_follows_one_redirect_and_writes_file(self):
        redirect = _DownloadResponse(
            status=302, location="/attachments/download/42/report.zip"
        )
        ok = _DownloadResponse(status=200, chunks=[b"AB", b"CD"])
        client, total, content = await self._download([redirect, ok])
        self.assertEqual(total, 4)
        self.assertEqual(content, b"ABCD")
        requested = [url for url, _kwargs in client._session.requests]
        self.assertEqual(
            requested,
            [
                "https://redmine.example.test/attachments/download/42/",
                "https://redmine.example.test/attachments/download/42/report.zip",
            ],
        )
        for _url, kwargs in client._session.requests:
            self.assertFalse(kwargs.get("allow_redirects", True))


if __name__ == "__main__":
    unittest.main()
