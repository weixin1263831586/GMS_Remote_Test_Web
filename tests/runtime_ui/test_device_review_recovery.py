"""Both review pages recover transient failures and stop on authentication errors."""

import json
import time
from unittest.mock import patch

from features.devices.debug_api import review_session
from features.devices.deployment_api import review_deployment
from tests.runtime_ui.harness import RuntimeUiHarness


class ReviewRecoveryBrowserTests(RuntimeUiHarness):
    def test_retry_backoff_auth_stop_and_navigation_cleanup(self):
        for kind in ("deployments", "debug-sessions"):
            for failure in ("network", "503", "404"):
                with self.subTest(kind=kind, failure=failure):
                    page = self.new_page()
                    page.clock.install()
                    path = "/api/devices/" + kind + "/" + "a" * 32
                    if kind == "deployments":
                        with patch("features.devices.deployment_api.deployment_status"):
                            html = review_deployment(None, "a" * 32).body.decode()
                    else:
                        with patch("features.devices.debug_api.service.read"):
                            html = review_session(None, "a" * 32).body.decode()
                    state = {"reads": 0, "mutations": 0}

                    def handle(route, _request, *, html=html, state=state, failure=failure, kind=kind):
                        if route.request.url.endswith("/review"):
                            route.fulfill(content_type="text/html", body=html)
                            return
                        if route.request.method != "GET":
                            state["mutations"] += 1
                        state["reads"] += 1
                        if state["reads"] == 1:
                            if failure == "network":
                                route.abort()
                            else:
                                route.fulfill(status=int(failure), content_type="application/json", body='{"error":"unavailable"}')
                            return
                        if state["reads"] >= 3:
                            route.fulfill(status=401 if failure == "network" else 403, body="login required")
                            return
                        item = {"filename": "app.apk", "package": "com.example.app", "signers": ["b" * 64],
                                "worker_id": "worker", "serial": "SERIAL", "sha256": "c" * 64,
                                "expires_at": time.time() + 300, "status": "pending", "replace_existing": True,
                                "actor_id": "agent:a", "baseline_sha256": "c" * 64,
                                "operations": ["stop"], "duration_minutes": 60}
                        route.fulfill(content_type="application/json", body=json.dumps({
                            "success": True, "deployment" if kind == "deployments" else "session": item,
                            "operations": [],
                        }))

                    page.route("**" + path + "**", handle)
                    try:
                        page.goto(self.base_url + path + "/review")
                        page.wait_for_function("document.getElementById('approve').disabled && !document.getElementById('status').textContent.includes('正在读取')")
                        if failure == "404":
                            self.assertIn("所属平台账号", page.locator("#status").inner_text())
                            page.clock.fast_forward(60000)
                            self.assertEqual(state, {"reads": 1, "mutations": 0})
                            continue
                        page.clock.fast_forward(6000)
                        page.wait_for_function("!document.getElementById('approve').disabled")
                        self.assertEqual(state["reads"], 2)
                        page.clock.fast_forward(3000)
                        page.wait_for_function("document.getElementById('status').textContent.includes('重新登录')")
                        page.clock.fast_forward(60000)
                        self.assertEqual(state, {"reads": 3, "mutations": 0})
                        page.goto("about:blank")
                        page.clock.fast_forward(60000)
                        self.assertEqual(state["reads"], 3)
                    finally:
                        page.close()
