import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from features.auth import CurrentUser


class ReportSourceApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_staging_directory_is_removed_on_every_exit(self):
        from features.reports import source_api

        class FakeConfig:
            def get_redmine_config(self):
                return {"base_url": "https://redmine.example.test", "domain": "redmine.example.test"}

            def get_redmine_base_url(self, config=None):
                return "https://redmine.example.test"

            def redmine_credentials_error_message(self):
                return "Credentials required"

        class FakeRequest:
            state = SimpleNamespace(current_user=CurrentUser(id="test-owner", username="test", role="user"))
            headers = {}
            cookies = {}

            async def json(self):
                return {"url": "https://redmine.example.test/report.zip"}

        class FakeResponse:
            status = 200
            headers = {"Content-Type": "application/zip"}

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            @property
            def content(self):
                return self

            async def iter_chunked(self, _size):
                yield b"PK\x03\x04test report"

        class FakeSession:
            def __init__(self, **kwargs):
                self.connector = kwargs["connector"]

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                await self.connector.close()

            def get(self, *_args, **_kwargs):
                return response

        directory_factory = tempfile.TemporaryDirectory
        download_limit = source_api.MAX_REPORT_URL_DOWNLOAD_BYTES
        with directory_factory() as parent:
            for outcome in (400, 401, 403, 413, "stream-413", 422, 500, 200, "cancelled"):
                with self.subTest(outcome=outcome):
                    created = []

                    def staging_directory(created=created, **kwargs):
                        directory = directory_factory(dir=parent, **kwargs)
                        created.append(Path(directory.name))
                        return directory

                    response = FakeResponse()
                    response.status = 403 if outcome == 403 else 200
                    response.headers = {"Content-Type": "application/zip"}
                    if outcome == 413:
                        response.headers["Content-Length"] = str(source_api.MAX_REPORT_URL_DOWNLOAD_BYTES + 1)
                    analyze = AsyncMock(return_value=None if outcome == 422 else {"failures": []})
                    if outcome == 500:
                        analyze.side_effect = RuntimeError("Analysis failed")
                    if outcome == "cancelled":
                        analyze.side_effect = asyncio.CancelledError
                    addresses = ["127.0.0.1"] if outcome == 400 else ["93.184.216.34"]
                    request = FakeRequest()
                    if outcome == 400:
                        request.json = AsyncMock(return_value={"url": "https://127.0.0.1/report.zip"})
                    with patch.object(source_api.tempfile, "TemporaryDirectory", side_effect=staging_directory), \
                            patch.object(source_api, "_redmine_config_manager_for_request", return_value=FakeConfig()), \
                            patch.object(source_api, "_load_redmine_credentials", AsyncMock(return_value=None if outcome == 401 else {"username": "test", "password": "test"})), \
                            patch.object(source_api, "_analyze_report_file", analyze), \
                            patch.object(source_api, "MAX_REPORT_URL_DOWNLOAD_BYTES", 3 if outcome == "stream-413" else download_limit), \
                            patch.object(source_api.aiohttp, "ClientSession", FakeSession), \
                            patch("foundation.outbound.socket.getaddrinfo", return_value=[(2, 1, 6, "", (addresses[0], 443))]):
                        if outcome == "cancelled":
                            with self.assertRaises(asyncio.CancelledError):
                                await source_api.analyze_report_from_url(request)
                        else:
                            result = await source_api.analyze_report_from_url(request)
                            self.assertEqual(result.status_code, 413 if outcome == "stream-413" else outcome, result.body)
                    self.assertEqual(len(created), 1)
                    self.assertTrue(all(not path.exists() for path in created))

    def test_url_log_target_omits_credentials_query_and_fragment(self):
        from features.reports.source_api import _url_log_target

        safe = _url_log_target(
            "https://user:password@example.test/report.zip?token=secret#part"
        )

        self.assertEqual(safe, "https://example.test/report.zip")

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
            def __init__(self, connector):
                self.connector = connector

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                await self.connector.close()
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
            source_api.aiohttp.ClientSession = lambda *args, **kwargs: FakeSession(kwargs["connector"])
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
            ), patch("foundation.outbound.socket.getaddrinfo", return_value=[
                (2, 1, 6, "", ("93.184.216.34", 443)),
            ]):
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
