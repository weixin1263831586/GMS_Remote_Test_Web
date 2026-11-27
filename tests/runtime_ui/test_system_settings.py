"""Live-browser regressions for independent configuration forms."""

from tests.runtime_ui.harness import RuntimeUiHarness, expect


class RuntimeSystemSettingsTests(RuntimeUiHarness):
    def test_opening_is_read_only_and_each_form_saves_its_own_configuration(self):
        page = self.new_page()
        writes = []
        fixtures = {
            "/api/config/update": {},
            "/api/config/static-routes": {"enabled": False, "routes": []},
            "/api/email/smtp": {"enabled": False},
            "/api/redmine-agent/config/stats": {"stale_days": 20},
            "/api/redmine-agent/config/credentials": {},
            "/api/redmine-agent/daily-brief/config": {},
        }

        def fixture(path, data):
            def respond(route):
                if route.request.method != "GET":
                    writes.append(path)
                route.fulfill(json={"success": True, "data": data})
            return respond

        for path, data in fixtures.items():
            page.route("**" + path, fixture(path, data))
        page.route("**/api/email/outbox?*", lambda route: route.fulfill(json={
            "success": True, "data": {"items": [{"subject": "<em>Plain subject</em>"}], "total": 1},
        }))
        try:
            self.goto_shell(page)
            for _ in range(2):
                page.evaluate("openSidebarVisibilityModal('system')")
                expect(page.locator("#system-config-footer button")).to_be_enabled()
                expect(page.locator("#save-static-routes")).to_be_enabled()
                expect(page.locator("#system-smtp-form button[type=submit]")).to_be_enabled()
                outbox = page.locator("#email-outbox-list")
                expect(outbox).to_contain_text("<em>Plain subject</em>")
                expect(outbox.locator("em")).to_have_count(0)
                page.evaluate("closeSidebarVisibilityModal()")
            self.assertEqual(writes, [])

            page.evaluate("openSidebarVisibilityModal('system')")
            fields = page.frame_locator("#system-redmine-settings-frame")
            expect(fields.locator("#save-redmine-settings")).to_be_enabled()
            self.assertEqual(writes, [])
            controls = (
                (page.locator("#system-config-footer button"), "/api/config/update"),
                (page.locator("#system-smtp-form button[type=submit]"), "/api/email/smtp"),
                (page.locator("#save-static-routes"), "/api/config/static-routes"),
                (fields.locator("#redmine-stats-form button[type=submit]"), "/api/redmine-agent/config/stats"),
                (fields.locator("#save-redmine-settings"), "/api/redmine-agent/daily-brief/config"),
            )
            for button, path in controls:
                with page.expect_response(lambda response, path=path: response.url.endswith(path)
                                          and response.request.method != "GET"):
                    button.click()
            self.assertEqual(writes, [path for _, path in controls])
            expect(page.locator("#sidebar-visibility-modal")).to_be_visible()
        finally:
            page.close()
