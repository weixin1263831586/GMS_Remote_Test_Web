"""Plan authoring uses explicit actions; navigation and reload remain read-only."""

import json
from urllib.parse import urlsplit

from tests.runtime_ui.harness import RuntimeUiHarness, expect


class RuntimeAutomationPlanTests(RuntimeUiHarness):
    def mock_plans(self, page):
        calls, plans = [], []

        def fulfill(route):
            request = route.request
            path = urlsplit(request.url).path
            body = request.post_data_json if request.post_data else None
            calls.append((request.method, path, body))
            data = {}
            if path.endswith('/plans') and request.method == 'POST':
                plan = {**body, 'id': 'plan-1', 'version': 1, 'step_count': len(body['runs']),
                        'next_run_at': '2026-11-01T18:00:00Z', 'last_run_at': '', 'active_batch_id': ''}
                plan.pop('runs')
                plans[:] = [plan]
                data = plan
            elif path.endswith('/plans'):
                data = {'items': plans}
            elif path.endswith('/settings'):
                plans[0].update(body)
                plans[0]['version'] += 1
                data = plans[0]
            elif path.endswith('/enabled'):
                plans[0]['enabled'] = body['enabled']
                plans[0]['version'] += 1
                data = plans[0]
            elif path.endswith('/run'):
                plans[0]['active_batch_id'] = 'batch-1'
                data = {'batch_id': 'batch-1'}
            elif path.endswith('/stop'):
                data = {'stopped_future_steps': True}
            elif path.endswith('/worker/status'):
                data = {'running': False, 'interval_seconds': 5, 'last_tick_seconds_ago': None}
            elif path.endswith('/runs') or path.endswith('/profiles'):
                data = {'items': []}
            elif path.endswith('/dashboard'):
                data = {'run_total': 0, 'run_by_status': {}, 'run_by_profile': {}}
            route.fulfill(status=200, content_type='application/json',
                          body=json.dumps({'success': True, 'data': data}))

        page.route('**/api/automation/**', fulfill)
        return calls, plans

    def test_plan_draft_schedule_save_and_edit_with_no_implicit_writes(self):
        page = self.new_page()
        calls, _ = self.mock_plans(page)
        try:
            page.goto(f'{self.base_url}/automation?tab=create', wait_until='domcontentloaded')
            page.wait_for_function("document.body.dataset.automationReady === 'true'")
            page.locator('#automation-flash-mode').select_option('skip')
            page.evaluate("""() => {
                testSuites = [
                    {test_type:'CTS',version:'15',tools_path:'/opt/cts/tools'},
                    {test_type:'VTS',version:'15',tools_path:'/opt/vts/tools'}
                ];
                setSelectValue('automation-worker','worker-1');
                renderSuiteOptions('/opt/cts/tools');
            }""")
            page.locator('#automation-max-retries').fill('2')
            page.locator('.advanced-test-options > summary').click()
            page.locator('#automation-preparations input[value="wifi_on"]').check()
            page.locator('#automation-preparations input[value="stay_awake"]').check()
            page.locator('#automation-preparation-order [data-click="movePreparation"][data-a0="1"][data-a1="-1"]').click()
            page.locator('#automation-shard-count').fill('1')
            page.locator('#automation-queue-timeout').fill('120')
            page.locator('#automation-execution-timeout').fill('3600')
            page.locator('#automation-idle-timeout').fill('300')
            page.get_by_role('button', name='加入测试计划草稿').click()
            expect(page.locator('#test-plan-draft li')).to_have_count(1)
            self.assertFalse([item for item in calls if item[0] != 'GET'])
            page.get_by_role('button', name='配置下一步测试').click()
            page.locator('#automation-test-type').select_option('VTS')
            page.locator('#automation-max-retries').fill('0')
            page.get_by_role('button', name='加入测试计划草稿').click()
            expect(page.locator('#test-plan-draft li')).to_have_count(2)
            page.locator('#test-plan-name').fill('每夜兼容性回归')
            page.locator('#test-plan-kind').select_option('daily')
            expect(page.locator('#test-plan-time-field')).to_be_visible()
            expect(page.locator('#test-plan-interval-field')).to_be_hidden()
            page.locator('#test-plan-enabled').check()
            page.once('dialog', lambda dialog: dialog.accept())
            page.locator('#test-plan-save').click()
            expect(page.locator('#test-plan-list')).to_contain_text('每夜兼容性回归')
            saved = next(body for method, path, body in calls if method == 'POST' and path.endswith('/plans'))
            self.assertEqual([body['test_plan']['test_type'] for body in saved['runs']], ['CTS', 'VTS'])
            self.assertEqual(saved['runs'][0]['test_plan']['retry_policy']['max_retries'], 2)
            self.assertEqual(saved['runs'][0]['test_plan']['preparations'], ['stay_awake', 'wifi_on'])
            self.assertEqual(saved['runs'][0]['test_plan']['execution_timeout_seconds'], 3600)
            self.assertEqual(saved['runs'][0]['test_plan']['output_idle_timeout_seconds'], 300)
            self.assertEqual(saved['runs'][0]['test_plan']['shard_count'], 1)
            self.assertEqual(saved['schedule']['timezone'], 'Asia/Shanghai')
            expect(page.locator('#test-plan-draft li')).to_have_count(0)

            page.get_by_role('button', name='编辑调度', exact=True).click()
            page.locator('#test-plan-kind').select_option('weekly')
            expect(page.locator('#test-plan-weekdays')).to_be_visible()
            page.once('dialog', lambda dialog: dialog.accept())
            page.locator('#test-plan-save').click()
            expect(page.locator('#test-plan-list')).to_contain_text('周 一')
            updated = next(body for method, path, body in calls if method == 'PUT' and path.endswith('/settings'))
            self.assertNotIn('runs', updated)
            self.assertEqual(updated['version'], 1)

            before = len([item for item in calls if item[0] != 'GET'])
            for _ in range(3):
                page.locator('[data-workflow="runs"]').click()
                with page.expect_response(lambda response: response.url.endswith('/api/automation/plans')):
                    page.locator('[data-workflow="plans"]').click()
            page.reload(wait_until='domcontentloaded')
            page.wait_for_function("document.body.dataset.automationReady === 'true'")
            expect(page.locator('#test-plan-list')).to_contain_text('每夜兼容性回归')
            self.assertEqual(len([item for item in calls if item[0] != 'GET']), before)
            page.get_by_role('button', name='暂停调度', exact=True).click()
            expect(page.locator('#test-plan-list')).to_contain_text('已暂停')
            page.once('dialog', lambda dialog: dialog.accept())
            page.get_by_role('button', name='执行一次', exact=True).click()
            expect(page.get_by_role('button', name='执行一次', exact=True)).to_be_disabled()
            page.get_by_role('button', name='停止后续', exact=True).click()
            expect(page.locator('#automation-toast')).to_contain_text('当前已运行测试仍继续')
        finally:
            page.close()
