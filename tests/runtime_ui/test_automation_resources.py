"""Catalog, digest-pinned warming, retention preview and telemetry in a browser."""

import json
from urllib.parse import urlsplit

from tests.runtime_ui.harness import RuntimeUiHarness, expect


class RuntimeAutomationResourceTests(RuntimeUiHarness):
    def mock_api(self, page):
        calls = []
        items = [{'id': 'log-1', 'job_id': 'job-1', 'filename': 'stdout.log', 'size_bytes': 100,
                  'sha256': 'a' * 64, 'pinned': False, 'operation': 'archive'},
                 {'id': 'report-1', 'job_id': 'job-1', 'filename': 'test_result.xml', 'size_bytes': 100,
                  'sha256': 'b' * 64, 'pinned': False, 'operation': ''}]

        def fulfill(route):
            request = route.request
            path = urlsplit(request.url).path
            body = request.post_data_json if request.post_data else None
            calls.append((request.method, path, body))
            data = {}
            if path.endswith('/suite-library'):
                data = {'archives': [{'name': 'cts-15_r2.zip', 'size': 100, 'modified': 42, 'complete': True}]}
            elif path.endswith('/fingerprint'):
                data = {'name': 'cts-15_r2.zip', 'size': 100, 'modified': 42, 'sha256': 'c' * 64}
            elif path.endswith('/workers'):
                data = {'workers': [{'id': 'worker-1', 'name': 'Worker One', 'status': 'online',
                                     'capabilities': {'suite_cache_sha256_v1': True, 'execution_options_v1': True}}]}
            elif path.endswith('/suites/download'):
                data = {'command_id': 'command-1', 'accepted': True}
            elif path.endswith('/commands/command-1'):
                data = {'command': {'status': 'completed', 'result': {'cache_hit': True}}}
            elif path.endswith('/retention/preview'):
                data = {'policy': {'enabled': False, 'archive_days': 7, 'delete_days': 30},
                        'items': items, 'total_bytes': 200, 'candidate_bytes': 100}
            elif path.endswith('/pin'):
                items[1]['pinned'] = body['pinned']
            elif path.endswith('/retention/apply'):
                items[0]['filename'] = 'stdout.log.gz'
                items[0]['operation'] = ''
            elif path.endswith('/runs'):
                data = {'items': [{'id': 'run-1', 'profile_id': 'manual', 'status': 'test_running',
                                   'worker_id': 'worker-1', 'cluster_job_id': 'job-1', 'attempt_id': 'attempt-1'}]}
            elif path.endswith('/profiles') or path.endswith('/timeline'):
                data = {'items': []}
            elif path.endswith('/progress'):
                data = {'modules': [{'module': '<img src=x onerror=alert(1)>', 'abi': 'arm64-v8a',
                                     'state': 'running', 'passed': 10, 'failed': 2}],
                        'preparations': [{'action': 'wifi_on', 'device': 'ABC', 'state': 'completed'}]}
            elif path.endswith('/worker/status'):
                data = {'running': False, 'interval_seconds': 5, 'last_tick_seconds_ago': None}
            route.fulfill(status=200, content_type='application/json',
                          body=json.dumps({'success': True, 'data': data}))

        page.route('**/api/automation/**', fulfill)
        page.route('**/api/cluster/**', fulfill)
        return calls

    def test_resource_warming_and_preview_only_mutate_on_explicit_actions(self):
        page = self.new_page()
        calls = self.mock_api(page)
        try:
            page.goto(self.base_url + '/automation?tab=resources', wait_until='domcontentloaded')
            page.wait_for_function("document.body.dataset.automationReady === 'true'")
            expect(page.locator('#resource-catalog')).to_contain_text('cts-15_r2.zip')
            expect(page.locator('#retention-preview')).to_contain_text('test_result.xml')
            self.assertFalse([item for item in calls if item[0] != 'GET'])
            page.get_by_role('button', name='校验 / 指纹').click()
            expect(page.get_by_role('button', name='预热到 Worker')).to_be_enabled()
            page.get_by_role('button', name='预热到 Worker').click()
            expect(page.locator('#resource-transfers')).to_contain_text('缓存命中')
            warm = next(body for method, path, body in calls if method == 'POST' and path.endswith('/suites/download'))
            self.assertEqual(warm['sha256'], 'c' * 64)
            self.assertEqual(warm['worker_id'], 'worker-1')
            page.get_by_role('checkbox', name='固定保留 test_result.xml', exact=True).check()
            expect(page.get_by_role('checkbox', name='选择产物 test_result.xml', exact=True)).to_be_disabled()
            page.get_by_role('checkbox', name='选择产物 stdout.log', exact=True).check()
            page.once('dialog', lambda dialog: dialog.accept())
            page.get_by_role('button', name='执行勾选的归档 / 清理').click()
            expect(page.locator('#retention-preview')).to_contain_text('stdout.log.gz')
            applied = next(body for method, path, body in calls if method == 'POST' and path.endswith('/retention/apply'))
            self.assertEqual(applied['selections'], [{'id': 'log-1', 'sha256': 'a' * 64, 'operation': 'archive'}])
            before = len([item for item in calls if item[0] != 'GET'])
            page.evaluate("switchWorkflowPane('plans')")
            page.evaluate("switchWorkflowPane('resources')")
            page.reload(wait_until='domcontentloaded')
            page.wait_for_function("document.body.dataset.automationReady === 'true'")
            expect(page.locator('#resource-catalog')).to_contain_text('cts-15_r2.zip')
            self.assertEqual(len([item for item in calls if item[0] != 'GET']), before)
        finally:
            page.close()

    def test_progress_is_observational_and_escapes_untrusted_module_names(self):
        page = self.new_page()
        self.mock_api(page)
        try:
            page.goto(self.base_url + '/automation?tab=runs', wait_until='domcontentloaded')
            page.wait_for_function("document.body.dataset.automationReady === 'true'")
            page.locator('#automation-runs').get_by_role('button', name='日志', exact=True).click()
            expect(page.locator('#automation-live-progress')).to_contain_text('<img src=x onerror=alert(1)>')
            expect(page.locator('#automation-live-progress')).to_contain_text('启用 Wi-Fi')
            expect(page.locator('#automation-live-progress img')).to_have_count(0)
            expect(page.locator('#automation-live-progress')).to_contain_text('运行中')
        finally:
            page.close()
