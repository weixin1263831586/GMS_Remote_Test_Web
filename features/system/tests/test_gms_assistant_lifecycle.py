"""GMS Assistant proxy lifecycle tests.

The bare /gms-assistant document must complete the browser DOM lifecycle
immediately (no 15s upstream wait); the upstream SPA is loaded
asynchronously by the local boot shell. Vite dev-server proxy routes must
only exist when GMS_ASSISTANT_DEV_PROXY=1.
"""

from __future__ import annotations

import importlib
import os
import unittest
from unittest import mock

from fastapi.testclient import TestClient


# Isolated data root + locked environment for every test in this file.
# This MUST NOT live at module import time: pytest collects the whole tree
# into one process, and a module-level os.environ write permanently leaks
# GMS_DATA_ROOT/GMS_SKIP_RUNTIME_ENV/GMS_AUTH_REQUIRED into every later
# test — 17 config-contract tests read the polluted root and fail
# (config_paths/config_migration/config_examples/dashboard_config …).
# Instead, each test activates the env in setUp and restores the previous
# values in tearDown.
_DATA_ROOT = "/tmp/gms-assistant-lifecycle-tests"
_ISOLATED_ENV = {
    "GMS_DATA_ROOT": _DATA_ROOT,
    "GMS_SKIP_RUNTIME_ENV": "1",
    "GMS_AUTH_REQUIRED": "false",
}


def _activate_isolated_env() -> None:
    for key, value in _ISOLATED_ENV.items():
        os.environ[key] = value
    os.environ.pop("GMS_ASSISTANT_URL", None)
    os.environ.pop("GMS_ASSISTANT_DEV_PROXY", None)


class _IsolatedEnvTestCase(unittest.TestCase):
    """Snapshot/restore the process env around each test method."""

    def setUp(self) -> None:
        self._env_snapshot = dict(os.environ)
        _activate_isolated_env()

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._env_snapshot)


def _load_app():
    import features.system.gms_assistant_proxy as assistant_proxy

    importlib.reload(assistant_proxy)

    import features.system.api as system_api

    importlib.reload(system_api)

    import app as app_module

    importlib.reload(app_module)
    return app_module.app


def _client_without_upstream(app):
    """TestClient with the configured upstream stripped from the proxy.

    A dev machine's configs/config.json may point gms_assistant_url at a
    live dev server; these tests must exercise the UNCONFIGURED behavior
    deterministically, so patch the resolver instead of relying on env.
    """

    from unittest import mock

    import features.system.gms_assistant_proxy as assistant_proxy

    patched = mock.patch.object(
        assistant_proxy, "_gms_assistant_upstream", return_value=""
    )
    patched.start()
    client = TestClient(app)
    client._upstream_patch = patched
    return client


def _stop_upstream_patch(client):
    client._upstream_patch.stop()


class GmsAssistantBootShellTests(_IsolatedEnvTestCase):
    def test_bare_assistant_page_is_local_shell_without_upstream(self):
        app = _load_app()
        # No `with`: the lifespan startup takes the process-global controller
        # lock (conflicts with a real Controller on a dev machine); the boot
        # shell routes do not depend on app.state, so skip startup events.
        client = TestClient(app)
        response = client.get("/gms-assistant/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "text/html; charset=utf-8")
        body = response.text
        # Local shell served synchronously: an unconfigured upstream must
        # not turn this into a 502/503 or block the DOM lifecycle.
        self.assertIn("<!doctype html>", body.lstrip().lower())
        self.assertIn("assistant-upstream-frame", body)
        self.assertIn("assistant-boot", body)
        self.assertIn("重试", body)
        self.assertIn("payload.boot_error === true", body)
        # The boot marker opts the inner frame into proxying.
        self.assertIn("__gms_boot=upstream", body)
        self.assertNotIn("__GMS_BOOT_MARKER__", body)

    def test_bare_assistant_page_without_trailing_slash(self):
        app = _load_app()
        client = TestClient(app)
        response = client.get("/gms-assistant")
        self.assertEqual(response.status_code, 200)
        self.assertIn("assistant-boot", response.text)

    def test_marker_query_still_proxies_upstream(self):
        app = _load_app()
        client = _client_without_upstream(app)
        # No upstream configured → the proxy path returns its own
        # "not configured" response (503 JSON / chat HTML), NOT the
        # boot shell — i.e. the marker really reached the proxy.
        response = client.get("/gms-assistant/?__gms_boot=upstream")
        _stop_upstream_patch(client)
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("assistant-boot", response.text)
        self.assertTrue(response.json()["boot_error"])

    def test_dev_proxy_routes_absent_by_default(self):
        app = _load_app()
        paths = {route.path for route in app.routes}
        for dev_path in (
            "/@vite/{path:path}",
            "/@react-refresh",
            "/@id/{path:path}",
            "/@fs/{path:path}",
            "/src/{path:path}",
            "/node_modules/{path:path}",
        ):
            self.assertNotIn(dev_path, paths, dev_path)
        # Production routes stay registered.
        self.assertIn("/gms-assistant", paths)
        self.assertIn("/gms-assistant/{path:path}", paths)
        self.assertIn("/assets/{path:path}", paths)

    def test_dev_proxy_routes_registered_with_env_flag(self):
        os.environ["GMS_ASSISTANT_DEV_PROXY"] = "1"
        try:
            # Assert on the proxy module's own router: reload(app) cannot
            # propagate into the cached bootstrap router aggregation, and
            # module-level conditional registration is what we contract on.
            import features.system.gms_assistant_proxy as assistant_proxy

            importlib.reload(assistant_proxy)
            paths = {route.path for route in assistant_proxy.router.routes}
            self.assertIn("/@vite/{path:path}", paths)
            self.assertIn("/@react-refresh", paths)
            self.assertIn("/node_modules/{path:path}", paths)
            self.assertIn("/@fs/{path:path}", paths)
        finally:
            os.environ.pop("GMS_ASSISTANT_DEV_PROXY", None)
            import features.system.gms_assistant_proxy as assistant_proxy

            importlib.reload(assistant_proxy)

    def test_unrelated_paths_do_not_get_boot_shell(self):
        app = _load_app()
        client = _client_without_upstream(app)
        # Only the bare assistant document is local; proxied sub-paths
        # keep the upstream proxy semantics (here: not configured → 503).
        response = client.get("/gms-assistant/assets/app.js")
        _stop_upstream_patch(client)
        self.assertEqual(response.status_code, 503)


class _FakeUpstreamContent:
    def __init__(self, body: bytes):
        self._body = body
        self._offset = 0

    async def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            size = len(self._body)
        chunk = self._body[self._offset : self._offset + size]
        self._offset += len(chunk)
        return chunk


class _FakeUpstreamResponse:
    def __init__(self, body: bytes = b"", content_type: str = "text/html; charset=utf-8"):
        self.status = 200
        self.body = body
        self.charset = "utf-8"
        self.headers = {"content-type": content_type}
        self.content = _FakeUpstreamContent(body)


class GmsAssistantProxySecurityTests(_IsolatedEnvTestCase):
    """Security contract: browser session credentials never reach upstream."""

    def _proxy_request(
        self,
        upstream_url,
        path,
        request_headers=None,
        fake_body=b"ok",
        max_bytes=None,
    ):
        import features.system.gms_assistant_proxy as assistant_proxy

        app = _load_app()
        captured = []
        response = _FakeUpstreamResponse(body=fake_body)

        class _FakeContext:
            def __init__(self, method, url, headers):
                self._entry = {
                    "method": method,
                    "url": url,
                    "headers": dict(headers or {}),
                }

            async def __aenter__(self):
                captured.append(self._entry)
                return response

            async def __aexit__(self, *exc):
                return False

        class _FakeSession:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            def request(self, method, url, headers=None, data=None, allow_redirects=False):
                return _FakeContext(method, url, headers)

        patches = [
            mock.patch.object(
                assistant_proxy, "_gms_assistant_upstream", return_value=upstream_url
            ),
            mock.patch.object(assistant_proxy.aiohttp, "ClientSession", _FakeSession),
        ]
        if max_bytes is not None:
            patches.append(
                mock.patch.object(
                    assistant_proxy, "_GMS_ASSISTANT_MAX_RESPONSE_BYTES", max_bytes
                )
            )
        for patch in patches:
            patch.start()
        try:
            client = TestClient(app)
            resp = client.get(path, headers=request_headers or {})
        finally:
            for patch in patches:
                patch.stop()
        return resp, captured

    def test_browser_session_credentials_never_reach_upstream(self):
        # 无效 Cookie 在 dev 模式解析为匿名；Authorization 用非 Bearer
        # 形式（Basic）——无效 Bearer 会被鉴权层在代理之前 fail-closed，
        # 由下一条测试单独验证。
        resp, captured = self._proxy_request(
            "https://assistant.example",
            "/gms-assistant/api/public/agents/demo/chat",
            request_headers={
                "Cookie": "gms_session=controller-secret",
                "Authorization": "Basic dXNlcjpwYXNz",
                "Accept": "application/json",
            },
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(captured), 1)
        sent = {key.lower(): value for key, value in captured[0]["headers"].items()}
        self.assertNotIn("cookie", sent)
        self.assertNotIn("authorization", sent)
        self.assertEqual(sent.get("accept"), "application/json")
        self.assertEqual(sent.get("host"), "assistant.example")

    def test_invalid_bearer_on_proxied_path_fails_closed_before_upstream(self):
        # Invalid Bearer never downgrades to anonymous
        # — the request must 401 at the Controller and never hit upstream.
        resp, captured = self._proxy_request(
            "https://assistant.example",
            "/gms-assistant/api/public/agents/demo/chat",
            request_headers={"Authorization": "Bearer controller-token"},
        )
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(captured, [])

    def test_non_https_remote_upstream_is_rejected_before_dialing(self):
        resp, captured = self._proxy_request(
            "http://assistant.example",
            "/gms-assistant/assets/app.js",
        )
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(captured, [])

    def test_loopback_http_upstream_is_allowed_for_development(self):
        resp, captured = self._proxy_request(
            "http://127.0.0.1:8787",
            "/gms-assistant/assets/app.js",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(captured), 1)
        sent = {key.lower(): value for key, value in captured[0]["headers"].items()}
        self.assertNotIn("cookie", sent)

    def test_upstream_response_over_cap_returns_502(self):
        resp, captured = self._proxy_request(
            "https://assistant.example",
            "/gms-assistant/assets/app.js",
            fake_body=b"x" * 64,
            max_bytes=8,
        )
        self.assertEqual(resp.status_code, 502)
        self.assertEqual(len(captured), 1)
        payload = resp.json()
        self.assertFalse(payload["success"])
        self.assertTrue(payload["boot_error"])
        # Same generic text as the dial-failure path so the boot shell's
        # same-origin peek keeps detecting hard failures.
        self.assertEqual(payload["error"], "GMS助手服务暂不可用")


if __name__ == "__main__":
    unittest.main()
