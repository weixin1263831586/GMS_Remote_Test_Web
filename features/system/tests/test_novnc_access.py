from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient

from bootstrap.application import create_app
from features.auth import auth_service
from features.system.desktop import _relay_novnc_websockets, _upstream_query_string
from features.system.novnc_access import novnc_access_service


class NoVNCAccessBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.original_db_path = auth_service.db_path
        self.original_initialized = auth_service._initialized
        auth_service.db_path = Path(self.tmp.name) / "auth.sqlite3"
        auth_service._initialized = False
        self.environment = patch.dict(
            "os.environ",
            {
                "GMS_AUTH_REQUIRED": "true",
                "GMS_SECURE_COOKIES": "false",
                "TRUSTED_HOSTS": "testserver",
            },
        )
        self.environment.start()
        self.client = TestClient(create_app())
        response = self.client.post(
            "/api/auth/setup",
            headers={"Origin": "http://testserver"},
            json={"username": "admin", "password": "strongpass1"},
        )
        self.assertEqual(response.status_code, 200)

    def tearDown(self):
        self.client.close()
        self.environment.stop()
        auth_service.db_path = self.original_db_path
        auth_service._initialized = self.original_initialized
        self.tmp.cleanup()

    def _elevate(self):
        return self.client.post(
            "/api/auth/elevate",
            headers={"Origin": "http://testserver"},
            json={"username": "admin", "password": "strongpass1"},
        )

    def test_access_grant_requires_elevation_and_is_bound_to_session_worker(self):
        denied = self.client.post(
            "/api/desktop/novnc/access",
            headers={"Origin": "http://testserver"},
            json={},
        )
        self.assertEqual(denied.status_code, 403)
        self.assertTrue(denied.json()["detail"]["elevation_required"])

        self.assertEqual(self._elevate().status_code, 200)
        issued = self.client.post(
            "/api/desktop/novnc/access",
            headers={"Origin": "http://testserver"},
            json={},
        )
        self.assertEqual(issued.status_code, 200)
        payload = issued.json()
        self.assertNotIn("password", payload["url"].lower())
        query = parse_qs(urlsplit(payload["url"]).query)
        access_token = query["access_token"][0]
        user = auth_service.get_user_for_token(self.client.cookies.get("gms_session"))
        self.assertIsNotNone(user)
        self.assertTrue(
            novnc_access_service.validate(
                access_token,
                user,
                self.client.cookies.get("gms_session"),
                payload["worker_id"],
            )
        )
        self.assertFalse(
            novnc_access_service.validate(
                access_token,
                user,
                self.client.cookies.get("gms_session"),
                "another-worker",
            )
        )

    def test_entry_html_rejects_missing_or_forged_grant(self):
        missing = self.client.get("/novnc/vnc.html")
        forged = self.client.get(
            "/novnc/vnc.html",
            params={"access_token": "forged"},
        )

        self.assertEqual(missing.status_code, 403)
        self.assertEqual(forged.status_code, 403)

    def test_controller_token_is_not_forwarded_to_upstream_http(self):
        query = _upstream_query_string(
            b"autoconnect=true&access_token=secret&path=websockify%3Faccess_token%3Dsecret"
        )

        self.assertNotIn("&access_token=secret", f"&{query}")
        self.assertIn("autoconnect=true", query)
        self.assertIn("path=websockify%3Faccess_token%3Dsecret", query)


class NoVNCRelayTests(unittest.IsolatedAsyncioTestCase):
    async def test_upstream_close_sends_normal_downstream_close(self):
        class Downstream:
            def __init__(self):
                self.sent = []
                self.closed = None
                self.wait = asyncio.Event()

            async def receive(self):
                await self.wait.wait()

            async def send_bytes(self, data):
                self.sent.append(data)

            async def send_text(self, data):
                self.sent.append(data)

            async def close(self, code):
                self.closed = code

        class Upstream:
            close_code = 1000

            def __aiter__(self):
                async def messages():
                    yield SimpleNamespace(type=2, data=b"RFB 003.008\n")
                return messages()

            async def close(self):
                return None

            async def send_bytes(self, _data):
                return None

            async def send_str(self, _data):
                return None

        downstream = Downstream()
        await _relay_novnc_websockets(downstream, Upstream())
        self.assertEqual(downstream.sent, [b"RFB 003.008\n"])
        self.assertEqual(downstream.closed, 1000)


if __name__ == "__main__":
    unittest.main()
