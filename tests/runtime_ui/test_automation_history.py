"""ATS-inspired history controls with real browser navigation and read-only load."""

import json
from urllib.parse import parse_qs, urlsplit

from tests.runtime_ui.harness import RuntimeUiHarness, expect


class RuntimeAutomationHistoryTests(RuntimeUiHarness):
    def mock_history(self, page):
        calls = []
        records = [{
            'id': 'run-new', 'profile_id': 'nightly', 'project': 'rk3576',
            'status': 'completed', 'worker_id': 'worker-1', 'devices_json': '["ABC"]',
            'created_by': 'alice', 'permissions': {'can_manage': True},
            'report_timestamp': 'report-new', 'test_type': 'CTS', 'test_suite': '/opt/cts',
            'test_summary': {'outcome': 'failed', 'total': 100, 'passed': 98, 'failed': 2},
        }, {
            'id': 'run-old', 'profile_id': '<img src=x onerror=alert(1)>',
            'status': 'completed', 'worker_id': 'worker-1', 'devices_json': '[]',
            'created_by': 'bob', 'permissions': {'can_manage': False},
            'report_timestamp': 'report-old', 'test_summary': {}, 'retry_of_run_id': 'run-parent',
        }]

        def fulfill(route):
            request = route.request
            url = urlsplit(request.url)
            query = parse_qs(url.query)
            calls.append((request.method, url.path, query))
            data = {}
            if url.path.endswith('/dashboard'):
                data = {'run_total': 2, 'run_by_status': {}, 'run_by_profile': {}}
            elif url.path.endswith('/profiles'):
                data = {'items': []}
            elif url.path.endswith('/runs'):
                older = query.get('cursor') == ['older']
                data = {'items': [records[1] if older else records[0]],
                        'has_more': not older, 'next_cursor': '' if older else 'older'}
            elif url.path.endswith('/timeline'):
                data = {'items': []}
            elif url.path.endswith('/trace'):
                data = {'run_id': 'run-parent', 'profile_id': 'nightly', 'status': 'completed'}
            elif url.path.endswith('/retry'):
                data = {'id': 'run-retry', 'status': 'queued'}
            elif '/runs/' in url.path:
                data = {'id': url.path.rsplit('/', 1)[-1], 'profile_id': 'nightly',
                        'worker_id': 'worker-1', 'status': 'queued'}
            elif url.path.endswith('/worker/status'):
                data = {'running': False, 'interval_seconds': 5, 'last_tick_seconds_ago': None}
            route.fulfill(status=200, content_type='application/json',
                          body=json.dumps({'success': True, 'data': data}))

        page.route('**/api/automation/**', fulfill)
        return calls

    def open_history(self, page):
        page.goto(f'{self.base_url}/automation?tab=runs', wait_until='domcontentloaded')
        page.wait_for_function("document.body.dataset.automationReady === 'true'")
        expect(page.locator('#automation-runs .run-card')).to_have_count(1)

    def test_history_filters_pagination_and_reload_are_read_only(self):
        page = self.new_page()
        calls = self.mock_history(page)
        try:
            self.open_history(page)
            expect(page.locator('#automation-runs')).to_contain_text('用例失败 · 失败 2 / 100')
            expect(page.locator('#automation-runs')).to_contain_text('完成')
            page.locator('#history-next-runs').click()
            expect(page.locator('#history-page-runs')).to_contain_text('第 2 页')
            expect(page.locator('#automation-runs')).to_contain_text('结果待确认')
            expect(page.locator('#automation-runs')).to_contain_text('<img src=x onerror=alert(1)>')
            expect(page.locator('#automation-runs img')).to_have_count(0)
            expect(page.locator('#history-next-runs')).to_be_disabled()
            page.locator('#history-first-runs').click()
            expect(page.locator('#history-page-runs')).to_contain_text('第 1 页')
            page.locator('#history-next-runs').click()
            expect(page.locator('#history-page-runs')).to_contain_text('第 2 页')
            page.locator('#history-previous-runs').click()
            expect(page.locator('#automation-runs')).to_contain_text('run-new')
            page.locator('#history-query-runs').fill('rk3576')
            page.locator('#history-outcome-runs').select_option('failed')
            page.locator('#history-profile_id-runs').fill('nightly')
            page.locator('#history-worker_id-runs').fill('worker-1')
            page.locator('#history-query-runs').press('Enter')
            page.wait_for_function("atsHistoryViews.runs.query === 'rk3576'")
            matching = [query for _, path, query in calls if path.endswith('/runs') and query.get('query')]
            self.assertEqual(matching[-1]['outcome'], ['failed'])
            self.assertEqual(matching[-1]['profile_id'], ['nightly'])
            self.assertEqual(matching[-1]['worker_id'], ['worker-1'])

            page.reload(wait_until='domcontentloaded')
            page.wait_for_function("document.body.dataset.automationReady === 'true'")
            expect(page.locator('#history-query-runs')).to_have_value('rk3576')
            expect(page.locator('#history-outcome-runs')).to_have_value('failed')
            for _ in range(3):
                with page.expect_response(lambda r: '/api/automation/runs?' in r.url and 'reports_only=true' in r.url):
                    page.locator('[data-workflow="reports"]').click()
                expect(page.locator('#automation-runs-report')).to_contain_text('用例失败')
                expect(page.locator('#history-query-reports')).to_have_value('')
                with page.expect_response(lambda r: '/api/automation/runs?' in r.url and 'query=rk3576' in r.url):
                    page.locator('[data-workflow="runs"]').click()
            self.assertFalse([call for call in calls if call[0] != 'GET'])
            page.locator('#workflow-pane-runs [data-click="clearRunHistoryFilters"]').click()
            expect(page.locator('#history-query-runs')).to_have_value('')
            page.locator('[data-status="testing"]').click()
            page.wait_for_function("atsStatus === 'testing'")
        finally:
            page.close()

    def test_pipeline_rerun_explains_scope_and_requires_actual_user_action(self):
        page = self.new_page()
        calls = self.mock_history(page)
        try:
            self.open_history(page)
            messages = []

            def dismiss(dialog):
                messages.append(dialog.message)
                dialog.dismiss()

            page.on('dialog', dismiss)
            page.get_by_role('button', name='重跑流水线', exact=True).click()
            self.assertIn('构建、烧写和测试', messages[0])
            self.assertFalse([call for call in calls if call[0] == 'POST'])
            page.remove_listener('dialog', dismiss)
            page.once('dialog', lambda dialog: dialog.accept())
            with page.expect_request(lambda r: r.method == 'POST' and r.url.endswith('/run-new/retry')):
                page.get_by_role('button', name='重跑流水线', exact=True).click()
            expect(page.locator('#workflow-pane-events')).to_have_attribute('aria-hidden', 'false')
            posts = [call for call in calls if call[0] == 'POST']
            self.assertEqual(len(posts), 1)
        finally:
            page.close()

    def test_team_runs_show_creator_and_allow_only_owner_or_admin_actions(self):
        page = self.new_page()
        calls = self.mock_history(page)
        records = [
            {'id': 'peer-queued', 'status': 'queued', 'created_by': 'bob', 'permissions': {'can_manage': False}},
            {'id': 'peer-finished', 'status': 'completed', 'created_by': 'bob', 'permissions': {'can_manage': False}},
            {'id': 'own-queued', 'status': 'queued', 'created_by': 'alice', 'permissions': {'can_manage': True}},
        ]

        def runs(route):
            route.fulfill(status=200, content_type='application/json', body=json.dumps({
                'success': True, 'data': {'items': records, 'has_more': False, 'next_cursor': ''},
            }))

        try:
            page.route('**/api/automation/runs?*', runs)
            page.goto(f'{self.base_url}/automation?tab=runs', wait_until='domcontentloaded')
            page.wait_for_function("document.body.dataset.automationReady === 'true'")
            cards = page.locator('#automation-runs .run-card')
            expect(cards).to_have_count(3)
            for run_id in ['peer-queued', 'peer-finished']:
                card = cards.filter(has=page.locator(f'button[data-click="loadEvents"][data-a0="{run_id}"]'))
                expect(card).to_contain_text('创建者 bob')
                expect(card).to_contain_text('只读')
                expect(card.locator('button[data-click="cancelRun"], button[data-click="retryRun"]')).to_have_count(0)
                expect(card.locator('button[data-click="loadTrace"]')).to_be_enabled()
            expect(page.locator('button[data-click="cancelRun"][data-a0="own-queued"]')).to_be_enabled()
            # Directly invoking the UI handler must also leave foreign runs untouched.
            page.evaluate("async () => {await cancelRun('peer-queued'); await retryRun('peer-finished');}")
            self.assertFalse([call for call in calls if call[0] == 'POST'])
            # The server supplies administrator authority; the renderer never guesses it from names.
            records[0]['permissions']['can_manage'] = True
            records[1]['permissions']['can_manage'] = True
            page.evaluate('loadRuns()')
            expect(page.locator('button[data-click="cancelRun"][data-a0="peer-queued"]')).to_be_enabled()
            expect(page.locator('button[data-click="retryRun"][data-a0="peer-finished"]')).to_be_enabled()
            self.assertFalse([call for call in calls if call[0] == 'POST'])
        finally:
            page.close()

    def test_completed_run_offers_reservation_cleanup_without_rerunning_the_pipeline(self):
        page = self.new_page()
        self.mock_history(page)
        released = []
        records = [
            {'id': 'own-completed', 'status': 'completed', 'created_by': 'alice',
             'permissions': {'can_manage': True}, 'device_reservation_id': 'reservation-old', 'resource_cleanup_state': ''},
            {'id': 'peer-completed', 'status': 'completed', 'created_by': 'bob',
             'permissions': {'can_manage': False}, 'device_reservation_id': 'reservation-peer', 'resource_cleanup_state': ''},
        ]

        def runs(route):
            route.fulfill(status=200, content_type='application/json', body=json.dumps({
                'success': True, 'data': {'items': records, 'has_more': False, 'next_cursor': ''},
            }))

        def release(route):
            released.append(route.request.url)
            records[0]['resource_cleanup_state'] = 'released'
            route.fulfill(status=200, content_type='application/json', body=json.dumps({'success': True, 'data': records[0]}))

        try:
            page.route('**/api/automation/runs?*', runs)
            page.route('**/api/automation/runs/*/release-reservation', release)
            page.goto(f'{self.base_url}/automation?tab=runs', wait_until='domcontentloaded')
            page.wait_for_function("document.body.dataset.automationReady === 'true'")
            button = page.locator('button[data-click="releaseRunReservation"]')
            expect(button).to_have_count(1)
            expect(button).to_have_attribute('data-a0', 'own-completed')
            self.assertEqual(released, [])
            page.evaluate("releaseRunReservation('peer-completed')")
            self.assertEqual(released, [])
            button.click()
            expect(page.locator('#automation-toast')).to_contain_text('设备预约已释放')
            expect(button).to_have_count(0)
            self.assertEqual(len(released), 1)
            self.assertTrue(released[0].endswith('/own-completed/release-reservation'))
            page.reload(wait_until='domcontentloaded')
            page.wait_for_function("document.body.dataset.automationReady === 'true'")
            self.assertEqual(len(released), 1)
        finally:
            page.close()
