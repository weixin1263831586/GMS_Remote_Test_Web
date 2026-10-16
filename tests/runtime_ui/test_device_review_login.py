"""Approval login accepts the resource owner and distinguishes missing requests."""

import json
import time

from tests.runtime_ui.harness import RuntimeUiHarness


class ReviewLoginBrowserTests(RuntimeUiHarness):
    def test_owner_login_from_anonymous_and_wrong_account_resumes_original_request(self):
        for kind in ("deployments", "debug-sessions"):
            for initial_status in (401, 403, 404):
                with self.subTest(kind=kind, initial_status=initial_status):
                    page = self.new_page()
                    role = {401: "admin", 403: "user", 404: "device_operator"}[initial_status]
                    username = "ui-admin" if role == "admin" else f"ui-owner-{kind}-{initial_status}"
                    if role != "admin":
                        elevated = page.request.post(self.base_url + "/api/auth/elevate", data={
                            "username": "ui-admin", "password": "UiSmokeAdmin-2026!",
                        })
                        self.assertTrue(elevated.ok, elevated.text())
                        created = page.request.post(self.base_url + "/api/auth/users", data={
                            "username": username, "password": "UiSmokeAdmin-2026!", "role": role,
                        })
                        self.assertTrue(created.ok, created.text())
                    if initial_status == 401:
                        page.context.clear_cookies()
                    path = "/api/devices/" + kind + "/" + "a" * 32
                    state = {"authenticated": False, "approvals": 0, "logins": 0}
                    errors = []
                    page.on("pageerror", lambda error, *, errors=errors: errors.append(str(error)))

                    def login_response(response, *, state=state):
                        if response.url.endswith("/api/auth/login"):
                            state["logins"] += 1
                            sent = response.request.post_data_json
                            state["username"] = sent.get("username")
                            state["fixture_password_matches"] = sent.get("password") == "UiSmokeAdmin-2026!"
                            state["switch_target"] = sent.get("account_switch_target")
                            state["authenticated"] = response.status == 200

                    def handle(route, _request, *, state=state, kind=kind, initial_status=initial_status):
                        if route.request.url.endswith("/review"):
                            route.continue_()
                            return
                        if route.request.method == "POST":
                            state["approvals"] += 1
                        if not state["authenticated"]:
                            route.fulfill(status=initial_status, content_type="application/json",
                                          body='{"success":false,"error":"unavailable"}')
                            return
                        item = {"filename": "fixture.apk", "package": "com.example.app", "signers": ["b" * 64],
                                "worker_id": "worker", "serial": "SERIAL", "sha256": "c" * 64,
                                "expires_at": time.time() + 300,
                                "status": ("completed" if kind == "deployments" else "active") if state["approvals"] else "pending",
                                "replace_existing": True, "actor_id": "agent:a", "baseline_sha256": "c" * 64,
                                "operations": ["stop"], "duration_minutes": 60}
                        route.fulfill(content_type="application/json", body=json.dumps({
                            "success": True, "deployment" if kind == "deployments" else "session": item,
                            "operations": [],
                        }))

                    page.on("response", login_response)
                    page.route("**" + path + "**", handle)
                    try:
                        page.goto(self.base_url + path + "/review")
                        modal = page.locator("#review-login-modal")
                        modal.wait_for(state="visible")
                        self.assertEqual(page.locator("#review-login-username").input_value(), "")
                        self.assertEqual(state["approvals"], 0)
                        self.assertEqual(state["logins"], 0)
                        page.keyboard.press("Escape")
                        modal.wait_for(state="hidden")
                        page.locator("#review-login-open").click()
                        modal.wait_for(state="visible")
                        # Let ModalManager finish its scheduled initial focus before typing.
                        page.evaluate("() => new Promise(resolve => requestAnimationFrame(resolve))")
                        # Only isolated test credentials are entered in this test browser.
                        page.locator("#review-login-username").fill(username)
                        self.assertEqual(page.locator("#review-login-username").input_value(), username)
                        if kind == "deployments" and initial_status == 401:
                            page.locator("#review-login-password").fill("invalid-fixture-password")
                            page.locator("#review-login-submit").click()
                            page.wait_for_function("document.getElementById('review-login-error').textContent.length > 0")
                            self.assertTrue(modal.is_visible())
                            self.assertEqual(page.locator("#review-login-password").input_value(), "")
                        page.locator("#review-login-password").fill("UiSmokeAdmin-2026!")
                        page.locator("#review-login-submit").click()
                        try:
                            modal.wait_for(state="hidden")
                        except Exception:
                            self.fail(f"login state={state}, error={page.locator('#review-login-error').inner_text()}, status={page.locator('#status').inner_text()}")
                        page.wait_for_function("!document.getElementById('approve').disabled")
                        self.assertEqual(page.url, self.base_url + path + "/review")
                        self.assertEqual(page.locator("#review-login-password").input_value(), "")
                        self.assertIsNone(state["switch_target"])
                        self.assertEqual(page.request.get(self.base_url + "/api/auth/status").json()["user"]["role"], role)
                        self.assertEqual(state["approvals"], 0)
                        page.locator("#approve").click()
                        page.wait_for_function("document.getElementById('approve').disabled && !document.getElementById('status').textContent.includes('等待批准')")
                        self.assertEqual(state["approvals"], 1)
                        self.assertEqual(errors, [])
                    finally:
                        page.close()

    def test_successful_login_with_missing_or_other_owner_request_stops_prompting(self):
        for kind in ("deployments", "debug-sessions"):
            with self.subTest(kind=kind):
                page = self.new_page()
                page.clock.install()
                path = "/api/devices/" + kind + "/" + "f" * 32
                reads = []

                def missing(route, _request=None, *, reads=reads):
                    if route.request.url.endswith("/review"):
                        route.continue_()
                        return
                    reads.append(route.request.method)
                    route.fulfill(status=404, content_type="application/json", body='{"success":false,"code":"NOT_FOUND"}')

                page.route("**" + path + "**", missing)
                try:
                    page.goto(self.base_url + path + "/review")
                    modal = page.locator("#review-login-modal")
                    modal.wait_for(state="visible")
                    page.clock.run_for(100)
                    page.locator("#review-login-username").fill("ui-admin")
                    page.locator("#review-login-password").fill("UiSmokeAdmin-2026!")
                    page.locator("#review-login-submit").click()
                    page.wait_for_function("document.getElementById('status').textContent.includes('ui-admin 已登录成功')")
                    modal.wait_for(state="hidden")
                    self.assertEqual(page.locator("#review-login-password").input_value(), "")
                    self.assertTrue(page.locator("#approve").is_disabled())
                    page.clock.fast_forward(60000)
                    self.assertEqual(reads, ["GET", "GET"])
                    self.assertFalse(modal.is_visible())
                    page.locator("#review-login-open").click()
                    modal.wait_for(state="visible")
                finally:
                    page.close()
