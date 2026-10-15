"""Approval pages prompt for admin login and resume reads without approving."""

import json
import time

from tests.runtime_ui.harness import RuntimeUiHarness


class ReviewLoginBrowserTests(RuntimeUiHarness):
    def test_admin_login_from_anonymous_and_wrong_account_resumes_original_request(self):
        for kind in ("deployments", "debug-sessions"):
            for initial_status in (401, 403, 404):
                with self.subTest(kind=kind, initial_status=initial_status):
                    page = self.new_page()
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
                        self.assertEqual(page.locator("#review-login-username").input_value(), "gms")
                        self.assertEqual(state["approvals"], 0)
                        self.assertEqual(state["logins"], 0)
                        page.keyboard.press("Escape")
                        modal.wait_for(state="hidden")
                        page.locator("#review-login-open").click()
                        modal.wait_for(state="visible")
                        # Let ModalManager finish its scheduled initial focus before typing.
                        page.evaluate("() => new Promise(resolve => requestAnimationFrame(resolve))")
                        # Only isolated test credentials are entered in this test browser.
                        page.locator("#review-login-username").fill("ui-admin")
                        self.assertEqual(page.locator("#review-login-username").input_value(), "ui-admin")
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
                        self.assertEqual(state["approvals"], 0)
                        page.locator("#approve").click()
                        page.wait_for_function("document.getElementById('approve').disabled && !document.getElementById('status').textContent.includes('等待批准')")
                        self.assertEqual(state["approvals"], 1)
                        self.assertEqual(errors, [])
                    finally:
                        page.close()
