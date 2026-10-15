"""Opening a grant page is read-only; approval and revocation require clicks."""

import json
import time
from unittest.mock import patch

from features.devices.debug_api import review_session
from tests.runtime_ui.harness import RuntimeUiHarness


class DebugReviewBrowserTests(RuntimeUiHarness):
    def test_repeated_navigation_approval_and_revocation(self):
        page = self.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        path = "/api/devices/debug-sessions/" + "a" * 32
        state = {"status": "pending"}
        mutations = []
        page.on("request", lambda request: mutations.append(request.url) if request.method == "POST" else None)
        with patch("features.devices.debug_api.service.read"):
            html = review_session(None, "a" * 32).body.decode()

        def handle(route):
            request = route.request
            if request.url.endswith("/review"):
                route.fulfill(content_type="text/html", body=html)
                return
            if request.url.endswith("/approve"):
                self.assertEqual(request.method, "POST")
                state["status"] = "active"
            if request.url.endswith("/revoke"):
                self.assertEqual(request.method, "POST")
                state["status"] = "revoked"
            payload = {"success": True, "session": {
                "actor_id": '<script>window.injected=true</script>', "worker_id": "worker", "serial": "SERIAL",
                "package": "com.example.app", "signers": ["a" * 64], "baseline_sha256": "b" * 64,
                "operations": ["install", "launch", "tap"], "duration_minutes": 60,
                "expires_at": time.time() + 3600, "status": state["status"], "busy": "",
            }, "operations": []}
            route.fulfill(content_type="application/json", body=json.dumps(payload))

        page.route("**" + path + "**", handle)
        try:
            for _ in range(2):
                page.goto(self.base_url + path + "/review")
                page.wait_for_function("!document.getElementById('approve').disabled")
                self.assertEqual(mutations, [])
                self.assertEqual(page.locator("#details script").count(), 0)
                self.assertFalse(page.evaluate("Boolean(window.injected)"))
                page.goto("about:blank")
            page.goto(self.base_url + path + "/review")
            page.wait_for_function("!document.getElementById('approve').disabled")
            page.locator("#approve").click()
            page.wait_for_function("document.getElementById('status').textContent === '调试授权有效'")
            self.assertEqual(mutations, [self.base_url + path + "/approve"])
            self.assertTrue(page.locator("#approve").is_disabled())
            page.locator("#revoke").click()
            page.wait_for_function("document.getElementById('status').textContent === '调试授权已撤销'")
            self.assertEqual(mutations, [self.base_url + path + "/approve", self.base_url + path + "/revoke"])
            self.assertTrue(page.locator("#revoke").is_disabled())
            self.assertEqual(errors, [])
        finally:
            page.close()
