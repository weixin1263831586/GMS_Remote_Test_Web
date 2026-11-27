from __future__ import annotations

import asyncio
import tempfile
import unittest
from contextlib import closing, nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from bootstrap.application import create_app
from features.auth import auth_service
from features.system.host.desktop import (
    _relay_novnc_websockets,
    _upstream_query_string,
    pin_novnc_host,
)
from features.system.host.novnc_access import novnc_access_service


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

    def _assert_grant_opens_proxy(self, url, socket_prefix):
        upstream = SimpleNamespace(status=200, headers={"content-type": "text/html"})
        backend = SimpleNamespace(
            get=lambda *_args, **_kwargs: nullcontext(upstream),
            ws_connect=lambda *_args, **_kwargs: nullcontext(None),
        )
        socket_path = parse_qs(urlsplit(url).query)["path"][0]
        with (
            patch("features.system.host.desktop.novnc_client_session", return_value=nullcontext(backend)),
            patch("features.system.host.desktop._read_novnc_body", return_value=b"<html>noVNC</html>"),
            patch("features.system.host.desktop._relay_novnc_websockets") as relay,
        ):
            self.assertEqual(self.client.get(url).status_code, 200)
            with self.client.websocket_connect(socket_prefix + socket_path):
                pass
            relay.assert_awaited_once()

    def test_admin_login_grant_opens_proxy_and_is_bound_to_session_worker(self):
        self.assertIsNone(auth_service.get_elevated_until(self.client.cookies.get("gms_session")))
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
        self._assert_grant_opens_proxy(payload["url"], "/novnc/")
        with closing(TestClient(self.client.app)) as second_session:
            login = second_session.post(
                "/api/auth/login",
                headers={"Origin": "http://testserver"},
                json={"username": "admin", "password": "strongpass1"},
            )
            self.assertEqual(login.status_code, 200)
            self.assertEqual(second_session.get(payload["url"]).status_code, 403)

    def test_ordinary_user_grant_requires_active_temporary_elevation(self):
        auth_service.create_user("operator", "operatorpass1", role="user")
        origin = {"Origin": "http://testserver"}
        login = self.client.post("/api/auth/login", headers=origin,
                                 json={"username": "operator", "password": "operatorpass1"})
        self.assertEqual(login.status_code, 200)
        denied = self.client.post("/api/desktop/novnc/access", headers=origin, json={})
        self.assertEqual(denied.status_code, 403)
        self.assertTrue(denied.json()["detail"]["elevation_required"])
        self.assertEqual(self._elevate().status_code, 200)
        issued = self.client.post("/api/desktop/novnc/access", headers=origin, json={})
        self.assertEqual(issued.status_code, 200)
        url = issued.json()["url"]
        self._assert_grant_opens_proxy(url, "/novnc/")

        self.assertEqual(self.client.post("/api/auth/elevation/reset", headers=origin).status_code, 200)
        self.assertEqual(self.client.get(url).status_code, 403)
        socket_path = parse_qs(urlsplit(url).query)["path"][0]
        with self.assertRaises(WebSocketDisconnect) as closed, self.client.websocket_connect("/novnc/" + socket_path):
            self.fail("revoked elevation must reject the WebSocket handshake")
        self.assertEqual(closed.exception.code, 4403)

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

    def test_worker_desktop_address_accepts_vpn_but_rejects_loopback_and_metadata(self):
        for address, expected in (
            ("10.1.2.3", "10.1.2.3"),
            ("::ffff:192.168.1.9", "192.168.1.9"),
            ("100.95.20.10", "100.95.20.10"),
            ("::ffff:100.95.20.10", "100.95.20.10"),
        ):
            with self.subTest(address=address):
                self.assertEqual(pin_novnc_host(address, 6080), expected)
        for blocked in (
            "127.0.0.1", "169.254.169.254", "0.0.0.0", "::1",
            "100.100.100.200", "::ffff:100.100.100.200",
        ):
            with self.subTest(blocked=blocked), self.assertRaises(ValueError):
                pin_novnc_host(blocked, 6080)

    def test_access_grant_supports_registered_vpn_worker(self):
        cluster = SimpleNamespace(
            effective_enabled=True,
            config=SimpleNamespace(local_worker_id="local"),
            repository=SimpleNamespace(get_worker=lambda _: {
                "id": "vpn-worker", "status": "online", "address": "100.95.20.10",
            }),
        )
        with (
            patch("foundation.cluster_port.get_cluster_service", return_value=cluster),
            patch("foundation.cluster_port.worker_tokens", return_value={"vpn-worker": "fixture-token"}),
        ):
            issued = self.client.post(
                "/api/desktop/novnc/access",
                headers={"Origin": "http://testserver"},
                json={"worker_id": "vpn-worker"},
            )
            self.assertEqual(issued.status_code, 200, issued.text)
            self._assert_grant_opens_proxy(issued.json()["url"], "/")

        self.assertEqual(issued.json()["worker_id"], "vpn-worker")
        self.assertTrue(issued.json()["url"].startswith("/cluster/novnc/vpn-worker/vnc.html?"))


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
