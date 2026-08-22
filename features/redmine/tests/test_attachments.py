from __future__ import annotations

import unittest

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


if __name__ == "__main__":
    unittest.main()
