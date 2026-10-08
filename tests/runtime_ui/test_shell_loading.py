"""Slow runtime chunks must not prevent parsing the complete Shell."""

import re
from urllib.parse import urlsplit

from tests.runtime_ui.harness import RuntimeUiHarness, expect


class RuntimeShellLoadingTests(RuntimeUiHarness):
    def test_slow_shell_chunk_does_not_block_document_parsing(self):
        page = self.new_page()
        held, errors = [], []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.route("**/static/js/shell/shell-runtime.js?*", lambda route: held.append(route))
        try:
            page.goto(self.base_url, wait_until="commit")
            # This modal follows the former synchronous runtime script block.
            # It must parse while that chunk is still awaiting its response.
            expect(page.locator("#report-diagnosis-modal")).to_be_attached()
            page.wait_for_function("document.readyState === 'interactive'")
            self.assertEqual(len(held), 1)
            expect(page.locator("#sidebar-nav")).to_be_visible()
            held.pop().continue_()
            page.wait_for_load_state("domcontentloaded")
            page.wait_for_function("window.GmsNavigationReady === true")
            expect(page.locator("#page-test")).to_have_class(re.compile(r"\bactive\b"))
            self.assert_no_page_errors(errors)
        finally:
            for route in held:
                route.abort()
            page.close()

    def test_critical_boot_preload_is_reused_by_the_script(self):
        authenticated = self.new_page()
        page = self.browser.new_page(viewport={"width": 1440, "height": 960}, bypass_csp=False)
        page.context.add_cookies(authenticated.context.cookies())
        authenticated.close()
        requests, errors, shell_scripts = [], [], []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("request", lambda request: requests.append(request.url)
                if urlsplit(request.url).path.endswith("/shell-early-boot.js") else None)
        page.on("request", lambda request: shell_scripts.append(urlsplit(request.url).path)
                if "/static/js/shell/shell-" in request.url else None)
        try:
            page.goto(self.base_url, wait_until="domcontentloaded")
            self.assertEqual(len(requests), 1)
            self.assertEqual(shell_scripts.count("/static/js/shell/shell-runtime.js"), 1)
            self.assertNotIn("/static/js/shell/shell-device-management.js", shell_scripts)
            self.assertEqual(page.evaluate("window.__targetPage"), "test")
            self.assertTrue(page.evaluate("window.GmsNavigationReady"))
            self.assert_no_page_errors(errors)
        finally:
            page.close()
