"""Redmine Daily Brief refresh regressions for navigation from conversational Agent."""

import json

from tests.runtime_ui.harness import RuntimeUiHarness, expect


class RedmineAgentNavigationTests(RuntimeUiHarness):
    def test_reentry_refreshes_agent_issue_history_without_manual_refresh(self):
        """A mounted Redmine iframe must reload history when made visible."""
        page = self.new_page()
        backend = {'status': 'running', 'calls': 0, 'writes': 0}

        def respond(route):
            url = route.request.url
            if route.request.method != 'GET':
                backend['writes'] += 1
            if url.endswith('/daily-brief/latest'):
                data = {'run': {'run_id': 'brief-done', 'status': 'completed'}, 'issues': []}
            elif url.endswith('/daily-brief/issue-analyses?limit=30'):
                backend['calls'] += 1
                data = {'items': [{
                    'run': {'run_id': 'agent-662309', 'status': backend['status']},
                    'issues': [{'issue_id': 662309, 'subject': 'Agent 分析',
                                'status': backend['status']}],
                }]}
            else:
                data = {}
            route.fulfill(status=200, content_type='application/json',
                          body=json.dumps({'success': True, 'data': data}))

        page.route('**/api/redmine-agent/**', respond)
        try:
            page.goto(f'{self.base_url}/redmine-agent', wait_until='domcontentloaded')
            page.evaluate("switchTab('daily-brief')")
            row = page.locator('[data-single-issue-id="662309"]')
            expect(row).to_contain_text('分析中')
            previous = backend['calls']
            backend['status'] = 'completed'
            page.evaluate("""() => {
                window.dispatchEvent(new CustomEvent('gms:embedded-visibility',
                    {detail: {visible: false}}));
                window.dispatchEvent(new CustomEvent('gms:embedded-visibility',
                    {detail: {visible: true}}));
            }""")
            expect(row).to_contain_text('已完成')
            self.assertGreater(backend['calls'], previous)
            self.assertEqual(backend['writes'], 0)
        finally:
            page.close()

    def test_independent_agent_analysis_schedules_poll_with_finished_daily_batch(self):
        """An Agent run is not covered by the morning batch's terminal status."""
        page = self.new_page()

        def respond(route):
            if route.request.url.endswith('/daily-brief/latest'):
                data = {'run': {'run_id': 'brief-done', 'status': 'completed'}, 'issues': []}
            elif route.request.url.endswith('/daily-brief/issue-analyses?limit=30'):
                data = {'items': [{
                    'run': {'run_id': 'agent-active', 'status': 'analyzing'},
                    'issues': [{'issue_id': 662309, 'status': 'running'}],
                }]}
            else:
                data = {}
            route.fulfill(status=200, content_type='application/json',
                          body=json.dumps({'success': True, 'data': data}))

        page.route('**/api/redmine-agent/**', respond)
        try:
            page.goto(f'{self.base_url}/redmine-agent', wait_until='domcontentloaded')
            page.evaluate("switchTab('daily-brief')")
            expect(page.locator('[data-single-issue-id="662309"]')).to_contain_text('分析中')
            page.wait_for_function("dailyBriefAutoPollTimer !== null")
            page.evaluate("""() => window.dispatchEvent(
                new CustomEvent('gms:embedded-visibility', {detail: {visible: false}})
            )""")
            page.wait_for_function("dailyBriefAutoPollTimer === null")
        finally:
            page.close()

