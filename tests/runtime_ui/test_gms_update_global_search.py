"""Regression checks for global-search errors, stale results and real filters."""

from tests.runtime_ui.harness import RuntimeUiHarness, expect


MOCK_REFERENCE_SEARCH = r"""
(() => {
  const originalFetch = window.fetch.bind(window);
  window.__referenceQueries = [];
  window.fetch = (input, init) => {
    const url = new URL(typeof input === 'string' ? input : input.url, window.location.href);
    const mainline = /^\/api\/gms-update-center\/(?:exemptions|security-patches|critical-patches)(?:\/|$)/.test(url.pathname);
    const monitor = url.pathname.startsWith('/api/gms-update-center/') && !mainline;
    if (!mainline && !monitor) return originalFetch(input, init);
    window.__referenceQueries.push(url.pathname + url.search);
    const respond = (body, status = 200) => new Response(JSON.stringify(body), {
      status, headers: {'Content-Type': 'application/json'},
    });
    if (url.pathname.endsWith('/scan/status')) return Promise.resolve(respond({success:true,data:{status:{running:false}}}));
    if (url.pathname.endsWith('/summary')) {
      return Promise.resolve(respond(mainline
        ? {success: true, total: 0, by_type: []}
        : {success: true, data: {sources: [], last_run: null}}));
    }
    const query = url.searchParams.get('q') || '';
    const label = query === 'slow' ? 'SLOW-RESULT'
      : query === 'fast' ? 'FAST-RESULT'
      : query === 'malicious' ? 'MALICIOUS-LINK' : '';
    const unavailable = query === 'unavailable';
    const isMatch = label && (mainline
      ? url.pathname.endsWith('/critical-patches')
      : url.pathname.endsWith('/artifacts'));
    const items = isMatch ? (mainline
      ? [{patch_id: 'A-12345678', patch_kind: 'Mandatory',
          description: label, bulletin_url: query === 'malicious' ? 'javascript:alert(1)' : 'https://example.test/official'}]
      : [{suite_type: 'CTS', android_version: '17',
          release_name: label, file_name: 'cts-demo.zip', download_url: query === 'malicious' ? 'javascript:alert(1)' : 'https://example.test/cts.zip'}]) : [];
    const body = mainline
      ? {success: !unavailable, available: !unavailable,
         coverage_complete: true, items, total: items.length}
      : {success: !unavailable, data: {items},
         meta: {total: items.length}};
    const delay = query === 'slow' ? 350 : 10;
    return new Promise(resolve => setTimeout(() => resolve(
      respond(body, unavailable ? 503 : 200)), delay));
  };
})();
"""


class ReferenceGlobalSearchRuntimeTests(RuntimeUiHarness):
    def test_mainline_search_does_not_overwrite_newer_query_or_claim_empty_on_failure(self):
        page = self.new_page()
        self.addCleanup(page.close)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.add_init_script(MOCK_REFERENCE_SEARCH)
        page.goto(self.base_url + "/gms-update-center?tab=exemptions", wait_until="domcontentloaded")
        page.locator("#q").fill("slow")
        page.locator("#q").press("Enter")
        page.locator("#q").fill("fast")
        page.locator("#q").press("Enter")
        expect(page.locator("#tables-area")).to_contain_text("FAST-RESULT")
        page.wait_for_timeout(450)
        expect(page.locator("#tables-area")).not_to_contain_text("SLOW-RESULT")
        page.locator("#q").fill("unavailable")
        page.locator("#q").press("Enter")
        expect(page.locator("#tables-area")).to_contain_text("查询不可用")
        expect(page.locator("#tables-area")).not_to_contain_text("无匹配「unavailable」")
        page.locator("#q").fill("")
        page.locator("#q").press("Enter")
        page.locator("#product_section").select_option("Android Go")
        page.locator("#exemption_id").fill("12345678")
        page.locator("#exemption_id").press("Enter")
        expect(page.locator("#pageinfo")).to_contain_text("0 / 0")
        query_urls = page.evaluate("window.__referenceQueries")
        self.assertTrue(any(
            u.startswith("/api/gms-update-center/exemptions?")
            and "product_section=Android+Go" in u
            and "exemption_id=12345678" in u
            for u in query_urls
        ), query_urls)
        self.assert_no_page_errors(errors)

    def test_untrusted_reference_urls_do_not_create_script_links(self):
        for path in ("/gms-update-center?tab=exemptions", "/gms-update-center?tab=artifacts"):
            with self.subTest(path=path):
                page = self.new_page()
                try:
                    page.add_init_script(MOCK_REFERENCE_SEARCH)
                    page.goto(self.base_url + path, wait_until="domcontentloaded")
                    page.locator("#q").fill("malicious")
                    page.locator("#q").press("Enter")
                    expect(page.locator("#tables-area")).to_contain_text("MALICIOUS-LINK")
                    self.assertEqual(page.locator('a[href^="javascript:"]').count(), 0)
                    self.assertEqual(page.locator('[data-url^="javascript:"]').count(), 0)
                finally:
                    page.close()

    def test_monitor_search_keeps_newest_results_and_surfaces_unavailable_sources(self):
        page = self.new_page()
        self.addCleanup(page.close)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.add_init_script(MOCK_REFERENCE_SEARCH)
        page.goto(self.base_url + "/gms-update-center", wait_until="domcontentloaded")
        page.locator("#q").fill("slow")
        page.locator("#q").press("Enter")
        page.locator("#q").fill("fast")
        page.locator("#q").press("Enter")
        expect(page.locator("#tables-area")).to_contain_text("FAST-RESULT")
        page.wait_for_timeout(450)
        expect(page.locator("#tables-area")).not_to_contain_text("SLOW-RESULT")
        page.locator("#q").fill("unavailable")
        page.locator("#q").press("Enter")
        expect(page.locator("#tables-area")).to_contain_text("查询不可用")
        expect(page.locator("#tables-area")).not_to_contain_text("无匹配「unavailable」")
        self.assert_no_page_errors(errors)


    def test_unified_center_has_eight_tabs_and_retires_old_routes(self):
        page = self.new_page()
        self.addCleanup(page.close)
        page.add_init_script(MOCK_REFERENCE_SEARCH)
        for old in ('/gms-update-monitor', '/gms-update-monitor/page.js', '/mainline-known-issues', '/mainline-known-issues/page.js',
                    '/api/gms-update-monitor/summary', '/api/mainline-known-issues', '/api/mainline-known-issues/critical-patches'):
            self.assertEqual(page.request.get(self.base_url + old).status, 404, old)
        for path, tab in (
            ("/gms-update-center?tab=packages", "packages"),
            ("/gms-update-center?tab=critical", "critical"),
            ("/gms-update-center?tab=changes", "changes"),
        ):
            page.goto(self.base_url + path, wait_until="domcontentloaded")
            expect(page.locator(".tabs button")).to_have_count(8)
            expect(page.locator(f'[data-tab="{tab}"]')).to_have_class("active")
        page.locator("#q").fill("fast")
        page.locator("#q").press("Enter")
        expect(page.locator("#tables-area")).to_contain_text("FAST-RESULT")
        expect(page.locator("#tables-area .group-row")).to_have_count(2)
        queries = page.evaluate("window.__referenceQueries")
        self.assertEqual(len({url.split("?")[0] for url in queries if "q=fast" in url}), 8)
        page.locator('[data-tab="exemptions"]').click()
        expect(page.locator("#q")).to_have_value("")
        expect(page.locator("#issue_type")).to_be_visible()
        expect(page.locator("#type_filter")).to_be_hidden()
        page.locator('[data-tab="artifacts"]').click()
        expect(page.locator("#source_key")).to_be_visible()
        expect(page.locator("#issue_type")).to_be_hidden()
        expect(page.locator("#pageinfo")).to_contain_text("0 / 0")
