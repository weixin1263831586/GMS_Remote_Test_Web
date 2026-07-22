import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from features.auth import CurrentUser


class ReportSourceApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_redmine_attachment_url_ignores_attachment_id_as_source_issue(self):
        from features.reports import source_api

        class FakeRequest:
            state = SimpleNamespace(current_user=CurrentUser(
                id="owner-1", username="owner", role="user"
            ))
            headers = {}
            cookies = {}

            async def json(self):
                return {
                    "url": "https://redmine.rock-chips.com/attachments/1588042",
                    "source_issue_id": "1588042",
                    "redmine_username": "user",
                    "redmine_password": "pass",
                }

        class FakeConfig:
            def get_redmine_config(self):
                return {
                    "domain": "redmine.rock-chips.com",
                    "base_url": "https://redmine.rock-chips.com",
                }

            def get_redmine_base_url(self, config=None):
                return "https://redmine.rock-chips.com"

        class FakeRedmineClient:
            def __init__(self, base_url, username="", password=""):
                self.base_url = base_url.rstrip("/")
                self.username = username
                self.password = password

            def download_url(self, attachment_id):
                return f"{self.base_url}/attachments/download/{attachment_id}/"

            async def find_attachment_issue_id(self, attachment_id):
                self.seen_attachment_id = attachment_id
                return "455845"

            async def close(self):
                pass

        class FakeContent:
            async def iter_chunked(self, _size):
                yield b"PK\x03\x04fake zip bytes"

        class FakeResponse:
            status = 200
            headers = {
                "Content-Disposition": 'attachment; filename="CtsOsTestCases.zip"',
                "Content-Type": "application/zip",
            }
            content = FakeContent()
            url = "https://redmine.rock-chips.com/attachments/download/1588042/"

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

        class FakeSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            def get(self, *_args, **_kwargs):
                return FakeResponse()

        original_redmine_client = source_api.RedmineClient
        original_client_session = source_api.aiohttp.ClientSession
        original_analyze = source_api._analyze_report_file
        original_load_creds = source_api._load_redmine_credentials
        original_save_creds = source_api._save_redmine_credentials
        source_api.REDMINE_ISSUE_ID_CACHE.clear()
        source_api.REDMINE_ISSUE_ID_CACHE["1588042"] = "1588042"
        try:
            source_api.RedmineClient = FakeRedmineClient
            source_api.aiohttp.ClientSession = lambda *args, **kwargs: FakeSession()
            async def fake_load_creds(_request):
                return {}

            async def fake_save_creds(_username, _password, _request):
                return True

            async def fake_analyze_report_file(*_args, **_kwargs):
                return {"failures": []}

            source_api._analyze_report_file = fake_analyze_report_file
            source_api._load_redmine_credentials = fake_load_creds
            source_api._save_redmine_credentials = fake_save_creds
            with patch.object(
                source_api, "_redmine_config_manager_for_request",
                return_value=FakeConfig(),
            ):
                response = await source_api.analyze_report_from_url(FakeRequest())
            payload = json.loads(response.body.decode("utf-8"))
        finally:
            source_api.RedmineClient = original_redmine_client
            source_api.aiohttp.ClientSession = original_client_session
            source_api._analyze_report_file = original_analyze
            source_api._load_redmine_credentials = original_load_creds
            source_api._save_redmine_credentials = original_save_creds
            source_api.REDMINE_ISSUE_ID_CACHE.clear()

        self.assertTrue(payload["success"])
        self.assertEqual(payload["filename"], "CtsOsTestCases.zip")
        self.assertEqual(
            payload["data"]["report_name"],
            "Redmine-455845-CtsOsTestCases.zip",
        )


if __name__ == "__main__":
    unittest.main()
