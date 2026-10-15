"""Review navigation is read-only; one click approves one immutable deployment."""

import json
import time
from unittest.mock import patch

from features.devices.deployment_api import review_deployment
from tests.runtime_ui.harness import RuntimeUiHarness


class DeploymentReviewBrowserTests(RuntimeUiHarness):
    def test_navigation_never_approves_and_one_click_executes_once(self):
        page = self.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        request_id = "a" * 32
        path = "/api/devices/deployments/" + request_id
        state = {"status": "pending", "approvals": 0}
        mutations = []
        page.on("request", lambda request: mutations.append(request.url) if request.method == "POST" else None)
        with patch("features.devices.deployment_api.deployment_status"):
            html = review_deployment(None, request_id).body.decode()

        def handle(route):
            request = route.request
            if request.url.endswith("/review"):
                route.fulfill(content_type="text/html", body=html)
                return
            if request.url.endswith("/approve"):
                self.assertEqual(request.method, "POST")
                state["approvals"] += 1
                state["status"] = "completed"
            payload = {"success": True, "deployment": {
                "id": request_id, "filename": '<script>window.injected=true</script>.apk',
                "worker_id": "worker", "serial": "SERIAL", "sha256": "1" * 64,
                "replace_existing": True, "expires_at": time.time() + 300,
                "status": state["status"], "result": {},
            }}
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
            page.wait_for_function("document.getElementById('status').textContent === '安装完成'")
            self.assertEqual(state["approvals"], 1)
            self.assertEqual(mutations, [self.base_url + path + "/approve"])
            self.assertTrue(page.locator("#approve").is_disabled())
            self.assertEqual(errors, [])
        finally:
            page.close()

    def test_inaccessible_request_explains_account_and_stops_polling(self):
        page = self.new_page()
        page.clock.install()
        path = "/api/devices/deployments/" + "a" * 32
        state = {"reads": 0, "mutations": 0}
        with patch("features.devices.deployment_api.deployment_status"):
            html = review_deployment(None, "a" * 32).body.decode()

        def handle(route):
            if route.request.url.endswith("/review"):
                route.fulfill(content_type="text/html", body=html)
                return
            if route.request.method != "GET":
                state["mutations"] += 1
            state["reads"] += 1
            route.fulfill(status=404, content_type="application/json",
                          body='{"success":false,"error":"APK 部署请求不存在","code":"NOT_FOUND"}')

        page.route("**" + path + "**", handle)
        try:
            page.goto(self.base_url + path + "/review")
            page.wait_for_function("document.getElementById('status').textContent.includes('所属平台账号')")
            self.assertTrue(page.locator("#approve").is_disabled())
            self.assertEqual(page.locator("#details").inner_text(), "")
            page.clock.fast_forward(60000)
            self.assertEqual(state, {"reads": 1, "mutations": 0})
        finally:
            page.close()
