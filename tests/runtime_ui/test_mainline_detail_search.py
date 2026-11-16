"""Detail-only Critical Fix matches remain visible across search and navigation."""

from urllib.parse import urlsplit

from tests.runtime_ui.harness import RuntimeUiHarness, expect


class RuntimeMainlineDetailSearchTests(RuntimeUiHarness):
    def test_detail_matches_are_visible_and_escaped_after_repeated_navigation(self):
        page = self.new_page()
        errors, mutations = [], []
        state = {'complete': True}
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.on('request', lambda request: mutations.append(request.method)
                if '/api/mainline-known-issues' in request.url and request.method != 'GET' else None)

        def respond(route):
            path = urlsplit(route.request.url).path
            if path.endswith('/summary'):
                data = {'total': 0, 'by_type': []}
            elif path.endswith('/critical-patches'):
                data = {'available': True, 'coverage_complete': state['complete'], 'total': 1, 'items': [{
                    'patch_id': 'A-568181732', 'description': 'Bluetooth correction',
                    'detail_excerpt': 'CVE-2026-58835 <img src=x onerror="window.evidenceInjected=true">',
                    'detail_status': 'ready' if state['complete'] else 'stale',
                    'bulletin_url': 'https://example.com/official#568181732',
                }]}
            else:
                data = {'total': 0, 'items': []}
            route.fulfill(json={'success': True, **data})

        page.route('**/api/mainline-known-issues*', respond)
        page.route('**/api/mainline-known-issues/**', respond)
        try:
            for index in range(3):
                state['complete'] = index != 1
                page.goto(f'{self.base_url}/mainline-known-issues?tab=critical')
                expect(page.locator('#tables-area')).to_contain_text('A-568181732')
                page.locator('#q').fill('CVE-2026-58835')
                page.locator('#q').press('Enter')
                expect(page.locator('#section-badge')).to_contain_text('全局搜索')
                page.locator('#tables-area details summary').click()
                expect(page.locator('#tables-area')).to_contain_text('CVE-2026-58835')
                expect(page.locator('#tables-area img')).to_have_count(0)
                self.assertFalse(page.evaluate('Boolean(window.evidenceInjected)'))
                if not state['complete']:
                    expect(page.locator('#pageinfo')).to_contain_text('详情未完整同步')
                page.locator('#q').fill('')
                page.locator('#q').press('Enter')
                page.locator('[data-tab="exemptions"]').click()
                page.locator('[data-tab="critical"]').click()
                expect(page.locator('#tables-area')).to_contain_text('A-568181732')
            self.assertEqual(mutations, [])
            self.assert_no_page_errors(errors)
        finally:
            page.close()
