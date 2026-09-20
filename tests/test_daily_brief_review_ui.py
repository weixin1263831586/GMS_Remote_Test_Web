"""Browser regressions for untrusted categories and long brief content."""

import json
import re
from datetime import datetime, timedelta

from tests.test_runtime_ui_smoke import RuntimeUiHarness, expect


class DailyBriefReviewUiTests(RuntimeUiHarness):
    def test_single_issue_input_locates_without_filtering_history(self):
        """「Redmine 单号」输入框只定位不过滤：命中翻页高亮，未命中不隐藏列表。"""
        page = self.new_page()
        page.route('**/api/redmine-agent/**', lambda route: route.fulfill(
            status=200, content_type='application/json', body='{"success":true,"data":{}}',
        ))
        try:
            page.goto(f'{self.base_url}/redmine-agent', wait_until='domcontentloaded')
            page.evaluate("switchTab('daily-brief')")
            page.evaluate("""() => {
                singleIssueAnalysisHistory = Array.from({length: 9}, (_, index) => ({
                    run: {run_id: 'history-' + index, status: 'completed', started_at: '2026-09-16T10:00:00'},
                    issues: [{issue_id: 650000 + index, subject: 'Issue title ' + index, status: 'completed'}]
                }));
                renderSingleIssueAnalysisHistory();
            }""")
            history = page.locator('#singleIssueAnalysisHistory')
            expect(history).to_contain_text('#650000')
            self.assertNotIn('#650008', history.inner_text())
            page.locator('[data-single-issue-page="2"]').click()
            expect(history).to_contain_text('#650008')
            # 输入已存在的单号：自动翻回所在页并高亮（只定位，不过滤）。
            page.locator('#singleIssueAnalysisId').fill('650003')
            entry = page.locator('article[data-single-issue-id="650003"]')
            expect(entry).to_have_class(re.compile(r'\bis-indexed\b'))
            expect(history).to_contain_text('#650000')
            # 无命中：列表原样保留（不隐藏任何历史条目，分页也在）。
            page.locator('#singleIssueAnalysisId').fill('999999')
            page.wait_for_timeout(400)
            expect(history).to_contain_text('#650000')
            expect(history.locator('article[data-single-issue-id="650004"]')).to_have_count(1)
            expect(page.locator('#singleIssueAnalysisPagination')).to_contain_text('1 / 2')
        finally:
            page.close()

    def test_saved_single_issue_analysis_is_shown_above_daily_brief_per_issue(self):
        page = self.new_page()

        def respond(route):
            data = {}
            if route.request.url.endswith('/daily-brief/issue-analyses?limit=30'):
                data = {'items': [{
                    'run': {'run_id': 'saved-652654', 'status': 'completed'},
                    'issues': [{'issue_id': 652654, 'subject': 'RK3576 自动亮度调节问题',
                        'status': 'completed', 'result': {'detailed_report': '# 分析结论\n\n已直接显示'},
                        'ai_statistics': {'execution_count': 1, 'tokens': {'total_tokens': 42},
                            'timing': {'total_duration_ms': 1000}}}],
                }]}
            route.fulfill(status=200, content_type='application/json', body=json.dumps({'success': True, 'data': data}))

        page.route('**/api/redmine-agent/**', respond)
        try:
            page.goto(f'{self.base_url}/redmine-agent', wait_until='domcontentloaded')
            page.evaluate("switchTab('daily-brief')")
            history = page.locator('#singleIssueAnalysisHistory')
            expect(history).to_contain_text('#652654')
            self.assertNotIn('已直接显示', history.inner_text())
            self.assertNotIn('总 Tokens', history.inner_text())
            history.get_by_text('查看分析', exact=True).click()
            expect(page.locator('.daily-brief-modal').last).to_contain_text('已直接显示')
            page.locator('.daily-brief-modal').last.get_by_text('关闭', exact=True).click()
            history.get_by_text('AI 统计', exact=True).click()
            expect(page.locator('.daily-brief-statistics-modal').last).to_contain_text('总 Tokens')
            self.assertLess(
                history.bounding_box()['y'],
                page.locator('.daily-brief-summary .daily-brief-toolbar').bounding_box()['y'],
            )
        finally:
            page.close()

    def test_daily_brief_row_mirrors_single_issue_analysis_status(self):
        """同单号状态联动：单号分析运行中/停止中 → 晨报行实时镜像；
        其终态晚于晨报记录时，晨报行展示该最新结论。"""
        page = self.new_page()

        def respond(route):
            url = route.request.url
            if url.endswith('/daily-brief/latest'):
                data = {'run': {'run_id': 'brief-1', 'brief_date': '2026-09-18', 'status': 'completed'},
                        'issues': [{'issue_id': 652135, 'subject': '晨报单', 'status': 'failed',
                                    'error_type': 'evidence_gate_failed', 'error': '证据门禁未通过'}]}
            elif url.endswith('/daily-brief/issue-analyses?limit=30'):
                data = {'items': [{'run': {'run_id': 'single-1', 'status': 'analyzing',
                                           'started_at': '2026-09-18T20:05:50'},
                                   'issues': [{'issue_id': 652135, 'status': 'running'}]}]}
            else:
                data = {}
            route.fulfill(status=200, content_type='application/json',
                          body=json.dumps({'success': True, 'data': data}))

        page.route('**/api/redmine-agent/**', respond)
        try:
            page.goto(f'{self.base_url}/redmine-agent', wait_until='domcontentloaded')
            page.evaluate("switchTab('daily-brief')")
            row = page.locator('#dailyBriefCard .daily-brief-row', has_text='#652135')
            # 单号分析运行中：晨报行同步显示分析中，行级停止指向该独立 run。
            expect(row).to_contain_text('分析中')
            expect(row.locator('[data-daily-brief-overlay-stop="single-1"]')).to_have_count(1)
            page.evaluate("singleIssueStopRequested['single-1'] = true; renderSingleIssueAnalysisHistory();")
            expect(row).to_contain_text('停止中')
            page.evaluate("delete singleIssueStopRequested['single-1']; renderSingleIssueAnalysisHistory();")
            # 单号分析完成（started_at 晚于晨报记录）→ 晨报行镜像最新结论，
            # 并恢复 增量/全量 + 重新分析 按钮组。
            page.evaluate("""() => {
                upsertSingleIssueAnalysis({
                    run: {run_id: 'single-1', status: 'completed', started_at: '2026-09-18T20:05:50'},
                    issues: [{issue_id: 652135, status: 'completed'}],
                });
                renderSingleIssueAnalysisHistory();
            }""")
            expect(row).to_contain_text('已完成')
            expect(row.locator('[data-daily-brief-overlay-stop]')).to_have_count(0)
            expect(row.locator('[data-daily-brief-reanalyze="652135"]')).to_have_count(1)
        finally:
            page.close()

    def test_refresh_on_daily_brief_syncs_redmine_subjects_first(self):
        """刷新按钮先同步 Redmine 最新标题：改过标题的单号刷新后直接显示新标题。"""
        page = self.new_page()
        latest = {
            'run': {'run_id': 'brief-1', 'brief_date': '2026-09-18', 'status': 'completed'},
            'issues': [{'issue_id': 653167, 'subject': 'rk3588 POWER', 'status': 'completed',
                        'result': {'confidence': 0.9}}],
        }
        sync_calls = {'count': 0}

        def respond(route):
            url = route.request.url
            if url.endswith('/daily-brief/sync-subjects'):
                sync_calls['count'] += 1
                data = {'checked': 1, 'updated': 1,
                        'issues': [{'issue_id': 653167, 'subject': 'rk3588 Android16 SSI GMS测试项支持----POWER问题'}]}
            elif url.endswith('/daily-brief/latest'):
                data = dict(latest)
                if sync_calls['count']:
                    latest['issues'][0]['subject'] = 'rk3588 Android16 SSI GMS测试项支持----POWER问题'
            elif url.endswith('/daily-brief/issue-analyses?limit=30'):
                data = {'items': []}
            else:
                data = {}
            route.fulfill(status=200, content_type='application/json',
                          body=json.dumps({'success': True, 'data': data}))

        page.route('**/api/redmine-agent/**', respond)
        try:
            page.goto(f'{self.base_url}/redmine-agent', wait_until='domcontentloaded')
            page.evaluate("switchTab('daily-brief')")
            card = page.locator('#dailyBriefCard')
            expect(card).to_contain_text('#653167')
            self.assertIn('rk3588 POWER', card.inner_text())
            page.locator('#refreshBtn').click()
            expect(card).to_contain_text('rk3588 Android16 SSI GMS测试项支持----POWER问题')
            self.assertEqual(sync_calls['count'], 1)
        finally:
            page.close()

    def test_enter_indexes_saved_issue_and_highlights_it_without_reanalysis(self):
        page = self.new_page()
        submissions = []
        cancellations = []
        state = {'cancelled': False}

        def respond(route):
            if route.request.method == 'POST':
                if route.request.url.endswith('/cancel'):
                    cancellations.append(route.request.url)
                    state['cancelled'] = True
                    data = {'run_id': 'reanalysis-fixture', 'cancel_requested': True}
                else:
                    submissions.append(route.request.post_data_json)
                    data = {'run_id': 'reanalysis-fixture', 'issue_id': 652654}
            elif route.request.url.endswith('/daily-brief/runs/reanalysis-fixture'):
                status = 'cancelled' if state['cancelled'] else 'pending'
                data = {'run': {'run_id': 'reanalysis-fixture', 'status': status},
                        'issues': [{'issue_id': 652654, 'subject': '3562-A16-normal版GSI的CtsStatsdAtomHostTestCases',
                                    'status': status}]}
            elif route.request.url.endswith('/daily-brief/issue-analyses?limit=30'):
                data = {'items': [{
                    'run': {'run_id': 'saved-652654', 'status': 'completed'},
                    'issues': [{'issue_id': 652654, 'subject': '3562-A16-normal版GSI的CtsStatsdAtomHostTestCases',
                                'status': 'completed'}],
                }]}
            else:
                data = {}
            route.fulfill(status=200, content_type='application/json', body=json.dumps({'success': True, 'data': data}))

        page.route('**/api/redmine-agent/**', respond)
        try:
            page.goto(f'{self.base_url}/redmine-agent', wait_until='domcontentloaded')
            page.evaluate("switchTab('daily-brief')")
            page.locator('#singleIssueAnalysisId').fill('652654')
            page.locator('#singleIssueAnalysisId').press('Enter')
            row = page.locator('article[data-single-issue-id="652654"]')
            expect(row).to_have_class(re.compile(r'\bis-indexed\b'))
            self.assertIn('3562-A16-normal版GSI', row.inner_text())
            self.assertEqual(submissions, [])
            self.assertEqual(page.locator('#singleIssueAnalysisStart').inner_text(), '开始分析')
            row.locator('[data-single-issue-reanalysis-mode]').select_option('full')
            row.get_by_text('重新分析', exact=True).click()
            expect(page.locator('#singleIssueAnalysisHistory')).to_contain_text('排队中')
            expect(page.locator('#singleIssueAnalysisHistory').get_by_text('停止分析', exact=True)).to_be_visible()
            expect(page.locator('#singleIssueAnalysisStart')).to_be_enabled()
            expect(page.locator('#singleIssueAnalysisStop')).to_have_count(0)
            self.assertEqual(submissions, [{'issue_id': 652654, 'analysis_mode': 'full'}])
            page.locator('#singleIssueAnalysisHistory').get_by_text('停止分析', exact=True).click()
            expect(page.locator('#singleIssueAnalysisHistory').get_by_text('重新分析', exact=True)).to_be_visible()
            self.assertEqual(len(cancellations), 1)
            self.assertTrue(cancellations[0].endswith('/daily-brief/runs/reanalysis-fixture/cancel'))
        finally:
            page.close()

    def test_partial_polling_response_preserves_saved_single_issue_content(self):
        page = self.new_page()
        page.route('**/api/redmine-agent/**', lambda route: route.fulfill(
            status=200, content_type='application/json', body='{"success":true,"data":{}}',
        ))
        try:
            page.goto(f'{self.base_url}/redmine-agent', wait_until='domcontentloaded')
            page.evaluate("""() => {
                singleIssueAnalysisHistory = [{run: {run_id: 'old-run', status: 'completed'}, issues: [{
                    issue_id: 652654, subject: '完整标题', status: 'completed',
                    result: {detailed_report: '保留的分析结论'}, ai_statistics: {execution_count: 1}
                }]}];
                upsertSingleIssueAnalysis({run: {run_id: 'new-run', status: 'pending'}, issues: [{
                    issue_id: 652654, status: 'pending'
                }]});
            }""")
            merged = page.evaluate("""() => singleIssueAnalysisHistory[0].issues[0]""")
            self.assertEqual(merged['subject'], '完整标题')
            self.assertEqual(merged['result']['detailed_report'], '保留的分析结论')
            self.assertEqual(merged['ai_statistics']['execution_count'], 1)
            self.assertEqual(merged['status'], 'pending')
        finally:
            page.close()

    def test_concurrent_single_issue_polls_keep_history_row_order_stable(self):
        page = self.new_page()
        page.route('**/api/redmine-agent/**', lambda route: route.fulfill(
            status=200, content_type='application/json', body='{"success":true,"data":{}}',
        ))
        try:
            page.goto(f'{self.base_url}/redmine-agent', wait_until='domcontentloaded')
            order = page.evaluate("""() => {
                singleIssueAnalysisHistory = [
                    {run: {run_id: 'run-652498', status: 'pending'}, issues: [{issue_id: 652498, status: 'pending'}]},
                    {run: {run_id: 'run-652654', status: 'pending'}, issues: [{issue_id: 652654, status: 'pending'}]},
                ];
                upsertSingleIssueAnalysis({run: {run_id: 'run-652498', status: 'analyzing'}, issues: [{issue_id: 652498, status: 'running'}]});
                upsertSingleIssueAnalysis({run: {run_id: 'run-652654', status: 'analyzing'}, issues: [{issue_id: 652654, status: 'running'}]});
                return singleIssueAnalysisHistory.map(item => item.issues[0].issue_id);
            }""")
            self.assertEqual(order, [652498, 652654])
        finally:
            page.close()

    def test_persisted_cancel_request_is_not_rendered_as_queued(self):
        page = self.new_page()
        page.route('**/api/redmine-agent/**', lambda route: route.fulfill(
            status=200, content_type='application/json', body='{"success":true,"data":{}}',
        ))
        try:
            page.goto(f'{self.base_url}/redmine-agent', wait_until='domcontentloaded')
            page.evaluate("""() => {
                singleIssueAnalysisHistory = [{
                    run: {run_id: 'stopping-run', status: 'pending', cancel_requested: true},
                    issues: [{issue_id: 652498, status: 'pending'}],
                }];
                renderSingleIssueAnalysisHistory();
            }""")
            expect(page.locator('#singleIssueAnalysisHistory')).to_contain_text('停止中')
            self.assertNotIn('排队中', page.locator('#singleIssueAnalysisHistory').inner_text())
        finally:
            page.close()

    def test_terminal_single_issue_cancel_is_not_kept_as_stopping(self):
        page = self.new_page()
        page.route('**/api/redmine-agent/**', lambda route: route.fulfill(
            status=200, content_type='application/json', body='{"success":true,"data":{}}',
        ))
        try:
            page.goto(f'{self.base_url}/redmine-agent', wait_until='domcontentloaded')
            page.evaluate("""() => {
                singleIssueAnalysisHistory = [{
                    run: {run_id: 'stopped-run', status: 'cancelled', cancel_requested: true},
                    issues: [{issue_id: 652654, status: 'cancelled'}],
                }];
                renderSingleIssueAnalysisHistory();
            }""")
            history = page.locator('#singleIssueAnalysisHistory')
            expect(history).to_contain_text('已停止')
            self.assertNotIn('停止中', history.inner_text())
        finally:
            page.close()

    def test_stopped_single_issue_modal_shows_the_previous_saved_report(self):
        page = self.new_page()
        page.route('**/api/redmine-agent/**', lambda route: route.fulfill(
            status=200, content_type='application/json', body='{"success":true,"data":{}}',
        ))
        try:
            page.goto(f'{self.base_url}/redmine-agent', wait_until='domcontentloaded')
            # 历史列表在 daily-brief 页签内（默认激活 stats），需先切换才可见。
            page.evaluate("switchTab('daily-brief')")
            page.evaluate("""() => {
                singleIssueAnalysisHistory = [{
                    run: {run_id: 'stopped-run', status: 'cancelled'},
                    issues: [{issue_id: 652654, status: 'cancelled', previous_analysis: {
                        finished_at: '2026-09-16T10:00:00',
                        result: {detailed_report: '# Previous conclusion\\n\\nRetained evidence'},
                    }}],
                }];
                renderSingleIssueAnalysisHistory();
            }""")
            page.locator('#singleIssueAnalysisHistory').get_by_text('查看分析', exact=True).click()
            modal = page.locator('.daily-brief-modal').last
            expect(modal).to_contain_text('最新一次分析已停止')
            expect(modal).to_contain_text('Retained evidence')
        finally:
            page.close()

    def test_active_single_issue_is_restored_after_page_reload(self):
        page = self.new_page()

        def respond(route):
            if route.request.url.endswith((
                '/daily-brief/active-issue',
                '/daily-brief/runs/restored-single',
            )):
                data = {
                    'run': {'run_id': 'restored-single', 'status': 'analyzing'},
                    'issues': [{'issue_id': 652654, 'status': 'running'}],
                }
            else:
                data = {}
            route.fulfill(status=200, content_type='application/json', body=json.dumps({'success': True, 'data': data}))

        page.route('**/api/redmine-agent/**', respond)
        try:
            page.goto(f'{self.base_url}/redmine-agent', wait_until='domcontentloaded')
            page.evaluate("switchTab('daily-brief')")
            expect(page.locator('#singleIssueAnalysisHistory')).to_contain_text('#652654')
            expect(page.locator('#singleIssueAnalysisHistory')).to_contain_text('分析中')
            expect(page.locator('#singleIssueAnalysisStop')).to_have_count(0)
            self.assertEqual(page.locator('#singleIssueAnalysisId').input_value(), '')
        finally:
            page.close()

    def test_stale_saved_single_issue_run_is_not_requested_after_reload(self):
        page = self.new_page()
        run_requests = []

        def respond(route):
            if route.request.url.endswith('/daily-brief/runs/missing-run'):
                run_requests.append(route.request.url)
                route.fulfill(
                    status=404, content_type='application/json',
                    body=json.dumps({'success': False, 'error': '分析任务不存在。'}),
                )
                return
            route.fulfill(
                status=200, content_type='application/json',
                body=json.dumps({'success': True, 'data': {}}),
            )

        page.route('**/api/redmine-agent/**', respond)
        try:
            page.goto(f'{self.base_url}/redmine-agent', wait_until='domcontentloaded')
            state = page.evaluate("""async () => {
                sessionStorage.setItem('gms-redmine-single-issue-run-id', 'missing-run');
                await restoreSingleIssueAnalysis();
                return {
                    runId: singleIssueAnalysisRunId,
                    storedRunId: sessionStorage.getItem('gms-redmine-single-issue-run-id'),
                    startDisabled: document.getElementById('singleIssueAnalysisStart').disabled,
                    stopCount: document.querySelectorAll('#singleIssueAnalysisStop').length,
                };
            }""")
            self.assertEqual(state['runId'], '')
            self.assertIsNone(state['storedRunId'])
            self.assertFalse(state['startDisabled'])
            self.assertEqual(state['stopCount'], 0)
            self.assertEqual(run_requests, [])
        finally:
            page.close()

    def test_single_issue_input_submits_only_one_issue_on_repeated_navigation(self):
        page = self.new_page()
        submissions = []

        def respond(route):
            request = route.request
            data = {}
            if request.method == 'POST':
                self.assertTrue(request.url.endswith('/daily-brief/analyze-issue'))
                submissions.append(request.post_data_json)
                data = {'run_id': 'single-fixture', 'issue_id': submissions[-1]['issue_id']}
            elif request.url.endswith('/daily-brief/runs/single-fixture'):
                issue_id = submissions[-1]['issue_id']
                data = {'run': {'run_id': 'single-fixture', 'status': 'completed'},
                        'issues': [{'issue_id': issue_id, 'status': 'completed',
                                    'result': {'detailed_report': '# 原始分析总结\n\n仅分析 #' + str(issue_id)}}]}
            route.fulfill(status=200, content_type='application/json', body=json.dumps({'success': True, 'data': data}))

        page.route('**/api/redmine-agent/**', respond)
        try:
            page.goto(f'{self.base_url}/redmine-agent', wait_until='domcontentloaded')
            page.wait_for_function("typeof analyzeSingleIssueFromInput === 'function'")
            for issue_id in (647338, 123):
                page.evaluate("switchTab('stats'); switchTab('daily-brief')")
                page.locator('#singleIssueAnalysisId').fill(str(issue_id))
                page.locator('#singleIssueAnalysisId').press('Enter')
                page.locator('#singleIssueAnalysisHistory').get_by_text('查看分析', exact=True).click()
                expect(page.locator('.daily-brief-modal').last).to_contain_text('仅分析 #' + str(issue_id))
                page.locator('.daily-brief-modal').last.get_by_text('关闭', exact=True).click()
                expect(page.locator('#singleIssueAnalysisStart')).to_be_enabled()
            self.assertEqual(submissions, [
                {'issue_id': 647338, 'analysis_mode': 'full'},
                {'issue_id': 123, 'analysis_mode': 'full'},
            ])
            page.set_viewport_size({'width': 1440, 'height': 900})
            titleStyles = page.evaluate("""() => {
                document.getElementById('dailyBriefCard').innerHTML = renderDailyBriefInner({
                    run: {run_id: 'cancelled-fixture', status: 'cancelled',
                        brief_date: '2026-09-15', finished_at: '2026-09-15T21:17:00'}, issues: []
                });
                return Array.from(document.querySelectorAll('.daily-brief-toolbar .daily-brief-title'))
                    .map(node => getComputedStyle(node).fontSize);
            }""")
            self.assertEqual(titleStyles, ['16px', '16px'])
            self.assertEqual(page.locator('#singleIssueAnalysisId').input_value(), '')
        finally:
            page.close()

    def test_single_issue_device_picker_selects_multiple_available_adb_devices(self):
        page = self.new_page()
        submissions = []

        def redmine(route):
            if route.request.method == 'POST':
                submissions.append(route.request.post_data_json)
                data = {'run_id': 'device-fixture', 'issue_id': 652654}
            elif route.request.url.endswith('/daily-brief/runs/device-fixture'):
                data = {'run': {'run_id': 'device-fixture', 'status': 'pending'},
                        'issues': [{'issue_id': 652654, 'status': 'pending'}]}
            else:
                data = {}
            route.fulfill(status=200, content_type='application/json', body=json.dumps({'success': True, 'data': data}))

        page.route('**/api/redmine-agent/**', redmine)
        page.route('**/api/devices/list?force_refresh=true', lambda route: route.fulfill(
            status=200, content_type='application/json', body=json.dumps([
                {'device_id': 'ADB-OWN', 'protocol': 'adb', 'status': 'online', 'locked': False},
                {'device_id': 'ADB-SECOND', 'protocol': 'adb', 'status': 'online', 'locked': False},
                {'device_id': 'ADB-THIRD', 'protocol': 'adb', 'status': 'online', 'locked': False},
                {'device_id': 'ADB-FOURTH', 'protocol': 'adb', 'status': 'online', 'locked': False},
                {'device_id': 'ADB-OTHER', 'protocol': 'adb', 'status': 'online', 'locked': True, 'locked_by_self': False},
                {'device_id': 'FASTBOOT', 'protocol': 'fastboot', 'status': 'fastboot', 'locked': False},
            ]),
        ))
        try:
            page.goto(f'{self.base_url}/redmine-agent', wait_until='domcontentloaded')
            page.evaluate("switchTab('daily-brief')")
            page.wait_for_function(
                "() => document.querySelectorAll('#singleIssueAnalysisDevice input[data-device-serial]').length === 4"
            )
            picker = page.locator('#singleIssueAnalysisDevice')
            expect(picker.locator('[data-device-options]')).to_be_hidden()
            picker.locator('[data-device-toggle]').click()
            expect(picker.locator('[data-device-options]')).to_be_visible()
            expect(picker).to_contain_text('ADB-OWN')
            expect(picker).to_contain_text('ADB-SECOND')
            self.assertNotIn('ADB-OTHER', picker.inner_text())
            expect(picker.locator('input[value="ADB-FOURTH"]')).to_be_visible()
            dimensions = page.evaluate("""() => {
                const picker = document.querySelector('#singleIssueAnalysisDevice');
                const options = picker.querySelector('[data-device-options]');
                return {pickerWidth: picker.getBoundingClientRect().width,
                    optionsWidth: options.getBoundingClientRect().width,
                    pickerLeft: picker.getBoundingClientRect().left,
                    optionsLeft: options.getBoundingClientRect().left};
            }""")
            self.assertEqual(dimensions['pickerWidth'], dimensions['optionsWidth'])
            self.assertEqual(dimensions['pickerLeft'], dimensions['optionsLeft'])
            top_layer = page.evaluate("""() => {
                const input = document.querySelector('#singleIssueAnalysisDevice input[value="ADB-FOURTH"]');
                const rect = input.getBoundingClientRect();
                const top = document.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2);
                return {visible: top === input || !!top.closest('#singleIssueAnalysisDevice'),
                    point: {x: rect.left + rect.width / 2, y: rect.top + rect.height / 2},
                    top: top && {tag: top.tagName, id: top.id, className: top.className}};
            }""")
            self.assertTrue(top_layer['visible'], top_layer)
            page.locator('#singleIssueAnalysisDevice input[data-device-serial][value="ADB-OWN"]').check()
            page.locator('#singleIssueAnalysisDevice input[data-device-serial][value="ADB-SECOND"]').check()
            expect(picker.locator('[data-device-label]')).to_have_text('已选 2 台设备')
            page.locator('#singleIssueAnalysisId').fill('652654')
            page.locator('#singleIssueAnalysisId').press('Enter')
            expect(page.locator('#singleIssueAnalysisHistory')).to_contain_text('#652654')
            self.assertEqual(submissions, [{
                'issue_id': 652654, 'analysis_mode': 'full',
                'device_serials': ['ADB-OWN', 'ADB-SECOND'],
            }])
        finally:
            page.close()

    def test_single_issue_sends_optional_analysis_hint(self):
        page = self.new_page()
        submissions = []

        def redmine(route):
            if route.request.method == 'POST':
                submissions.append(route.request.post_data_json)
                data = {'run_id': 'hint-fixture', 'issue_id': 652498}
            elif route.request.url.endswith('/daily-brief/runs/hint-fixture'):
                data = {'run': {'run_id': 'hint-fixture', 'status': 'completed'},
                        'issues': [{'issue_id': 652498, 'status': 'completed'}]}
            else:
                data = {}
            route.fulfill(status=200, content_type='application/json', body=json.dumps({'success': True, 'data': data}))

        page.route('**/api/redmine-agent/**', redmine)
        try:
            page.goto(f'{self.base_url}/redmine-agent', wait_until='domcontentloaded')
            page.evaluate("switchTab('daily-brief')")
            page.locator('#singleIssueAnalysisId').fill('652498')
            page.locator('#singleIssueAnalysisHintButton').click()
            hint_modal = page.locator('.single-issue-analysis-hint-modal')
            hint_modal.locator('textarea').fill('Patch is ineffective; verify the runtime value.')
            hint_modal.get_by_text('保存', exact=True).click()
            expect(page.locator('#singleIssueAnalysisHintButton')).to_contain_text('已保存')
            page.locator('#singleIssueAnalysisStart').click()
            expect(page.locator('#singleIssueAnalysisHistory')).to_contain_text('#652498')
            self.assertEqual(submissions, [{
                'issue_id': 652498,
                'analysis_mode': 'full',
                'analysis_hint': 'Patch is ineffective; verify the runtime value.',
            }])
        finally:
            page.close()

    def test_user_status_cards_filter_without_removed_select(self):
        page = self.new_page()
        try:
            self.goto_shell(page)
            page.wait_for_function("typeof setUsersStatusFilter === 'function'")
            result = page.evaluate("""() => {
                const fixtures = [
                    {client_id: 'idle-user', status: 'online', local_devices: {devices: []}},
                    {client_id: 'busy-user', status: 'testing', local_devices: {devices: []}}
                ];
                displayUsersList(fixtures);
                setUsersStatusFilter('testing');
                const filtered = document.getElementById('users-table-body').textContent;
                displayUsersList(fixtures);
                const refreshed = document.getElementById('users-table-body').textContent;
                setUsersStatusFilter('');
                return {filtered, refreshed, all: document.getElementById('users-table-body').textContent};
            }""")
            self.assertIn('busy-user', result['filtered'])
            self.assertNotIn('idle-user', result['filtered'])
            self.assertNotIn('idle-user', result['refreshed'])
            self.assertIn('idle-user', result['all'])
            self.assertIn('busy-user', result['all'])
        finally:
            page.close()

    def test_issue_ai_statistics_open_from_the_daily_brief_row(self):
        page = self.new_page()
        page.route("**/api/redmine-agent/**", lambda route: route.fulfill(
            status=200, content_type="application/json", body='{"success":true,"data":{}}',
        ))
        try:
            page.goto(f"{self.base_url}/redmine-agent", wait_until="domcontentloaded")
            page.wait_for_function("typeof renderDailyBriefInner === 'function'")
            page.evaluate("switchTab('daily-brief')")
            page.wait_for_load_state("networkidle")
            page.evaluate("""() => {
                dailyBriefCache = {
                    run: {run_id: 'statistics-fixture', status: 'completed', report_json: {}},
                    issues: [{issue_id: 101, status: 'completed', result: {}, ai_statistics: {
                        execution_count: 2,
                        tokens: {total_tokens: 6_140_285, input_tokens: 6_085_152, output_tokens: 55_133},
                        timing: {total_duration_ms: 497_000, average_duration_ms: 497_000},
                        gms_tool_call_count: 3,
                        models: [{model_name: 'glm-4.7', execution_count: 2, total_tokens: 6_140_285,
                                  input_tokens: 6_085_152, output_tokens: 55_133}],
                        gms_tools: [{tool_name: 'gms_rt_redmine_issue_fetch', call_count: 3,
                                     succeeded_count: 2, failed_count: 1}],
                        tool_improvement_recommendations: [{
                            tool_name: 'gms_rt_redmine_issue_fetch',
                            message: '1/3 次调用失败；优先补充失败码、参数校验和重试指引。'
                        }]
                    }}]
                };
                document.getElementById('dailyBriefCard').innerHTML = renderDailyBriefInner(dailyBriefCache);
            }""")
            page.locator('#dailyBriefCard').get_by_text('AI 统计', exact=True).click()
            statistics = page.locator('.daily-brief-statistics-modal').last
            expect(statistics).to_be_visible()
            text = statistics.inner_text()
            self.assertIn('总 Tokens', text)
            self.assertIn('总耗时', text)
            self.assertIn('平均耗时', text)
            self.assertIn('8 分 17 秒', text)
            self.assertIn('glm-4.7', text)
            self.assertIn('gms_rt_redmine_issue_fetch', text)
            self.assertIn('失败码', text)
            self.assertEqual(statistics.locator('img').count(), 0)
            self.assertGreaterEqual(statistics.bounding_box()['width'], 1_000)
            self.assertEqual(
                statistics.locator('.daily-brief-stat-table td:nth-child(3)').first.evaluate(
                    '(node) => getComputedStyle(node).whiteSpace'
                ),
                'nowrap',
            )
            self.assertLess(statistics.bounding_box()['height'], 800)
        finally:
            page.close()

    def test_daily_brief_rows_match_single_issue_analysis_buttons(self):
        """晨报行与「Redmine 单号分析」按钮集对齐：重新分析 ↔ 停止分析。"""
        page = self.new_page()
        state = {"run_status": "completed", "issue101": "completed", "issue102": "running"}
        posts = []

        def respond(route):
            url = route.request.url
            if route.request.method == "POST":
                posts.append(url)
                if url.endswith("/cancel"):
                    state["run_status"] = "cancelled"
                    state["issue102"] = "cancelled"
                else:
                    state["run_status"] = "pending"
                    state["issue101"] = "pending"
                route.fulfill(status=200, content_type="application/json",
                              body=json.dumps({"success": True, "data": {
                                  "run_id": "brief-1", "issue_id": 101, "status": "pending"}}))
                return
            data = {
                "run": {"run_id": "brief-1", "brief_date": "2026-09-15",
                        "status": state["run_status"], "report_json": {},
                        "waiting_my_reply_count": 2, "no_reply_3_days_count": 0},
                "issues": [
                    {"issue_id": 101, "subject": "已完成项", "status": state["issue101"], "result": {}},
                    {"issue_id": 102, "subject": "进行中项", "status": state["issue102"], "result": {}},
                ],
            }
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({"success": True, "data": data}))

        page.route("**/api/redmine-agent/**", respond)
        try:
            page.goto(f"{self.base_url}/redmine-agent", wait_until="domcontentloaded")
            page.evaluate("switchTab('daily-brief')")
            page.wait_for_load_state("networkidle")
            card = page.locator("#dailyBriefCard")
            row101 = card.locator(".daily-brief-row", has_text="#101")
            row102 = card.locator(".daily-brief-row", has_text="#102")
            # 基线：完成行有重新分析、运行行有停止分析；公共按钮集与单号分析一致。
            for label in ("查看分析", "打开 Redmine", "AI 统计"):
                expect(row101.get_by_text(label, exact=True)).to_be_visible()
            expect(row101.get_by_text("重新分析", exact=True)).to_be_visible()
            expect(row102.get_by_text("停止分析", exact=True)).to_be_visible()
            # 行内重新分析：POST run 精确入口，行翻转为排队中 + 停止分析。
            row101.get_by_text("重新分析", exact=True).click()
            expect(row101.get_by_text("停止分析", exact=True)).to_be_visible()
            self.assertTrue(any(
                url.endswith("/daily-brief/runs/brief-1/issues/101/reanalyze") for url in posts))
            # 行内停止分析：POST run 级取消；收敛为已停止后行回到重新分析。
            row102.get_by_text("停止分析", exact=True).click()
            self.assertTrue(any(url.endswith("/daily-brief/runs/brief-1/cancel") for url in posts))
            expect(row102.get_by_text("重新分析", exact=True)).to_be_visible()
        finally:
            page.close()

    def test_daily_brief_row_full_reanalysis_creates_standalone_run(self):
        """晨报行选「全量」：走 analyze-issue 新建独立 run；晨报行与该独立
        run 状态联动（分析中 + 行级停止指向独立 run）。"""
        page = self.new_page()
        posts = []

        def respond(route):
            url = route.request.url
            if route.request.method == "POST" and url.endswith("/daily-brief/analyze-issue"):
                posts.append(route.request.post_data_json)
                route.fulfill(status=200, content_type="application/json",
                              body=json.dumps({"success": True, "data": {
                                  "run_id": "full-1", "issue_id": 101,
                                  "status": "pending", "queued": True}}))
                return
            if url.endswith("/daily-brief/runs/full-1"):
                route.fulfill(status=200, content_type="application/json",
                              body=json.dumps({"success": True, "data": {
                                  "run": {"run_id": "full-1", "status": "analyzing",
                                          "mode": "issue:101:full:x"},
                                  "issues": [{"issue_id": 101, "status": "running"}]}}))
                return
            data = {
                "run": {"run_id": "brief-1", "brief_date": "2026-09-15", "status": "completed",
                        "report_json": {}, "device_serial": "ADB-OWN", "analysis_hint": ""},
                "issues": [{"issue_id": 101, "subject": "已完成项", "status": "completed",
                            "result": {}}],
            }
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({"success": True, "data": data}))

        page.route("**/api/redmine-agent/**", respond)
        try:
            page.goto(f"{self.base_url}/redmine-agent", wait_until="domcontentloaded")
            page.evaluate("switchTab('daily-brief')")
            page.wait_for_load_state("networkidle")
            card = page.locator("#dailyBriefCard")
            row = card.locator(".daily-brief-row", has_text="#101")
            mode = row.locator("select[data-daily-brief-reanalysis-mode]")
            expect(mode).to_be_visible()
            expect(mode).to_have_value("incremental")
            mode.select_option("full")
            row.get_by_text("重新分析", exact=True).click()
            expect(page.locator("#singleIssueAnalysisHistory")).to_contain_text("#101")
            expect(page.locator("#singleIssueAnalysisHistory")).to_contain_text("分析中")
            # 全量请求带批量 run 的取证上下文（设备）；独立 run 运行中时
            # 晨报行联动显示分析中，行级停止精确指向该独立 run。
            self.assertEqual(posts, [{"issue_id": 101, "analysis_mode": "full",
                                      "device_serial": "ADB-OWN"}])
            expect(row).to_contain_text("分析中")
            expect(row.locator('[data-daily-brief-overlay-stop="full-1"]')).to_have_count(1)
        finally:
            page.close()

    def test_daily_brief_section_toolbars_stay_visible_while_scrolling(self):
        """滚动晨报 tab：只有当前区块的工具栏吸附在页头下缘（单条常驻）。"""
        page = self.new_page()
        page.route('**/api/redmine-agent/**', lambda route: route.fulfill(
            status=200, content_type='application/json', body='{"success":true,"data":{}}',
        ))
        try:
            page.goto(f'{self.base_url}/redmine-agent', wait_until='domcontentloaded')
            page.set_viewport_size({'width': 1200, 'height': 500})
            page.evaluate("switchTab('daily-brief')")
            page.wait_for_function("typeof syncDailyBriefStickyTop === 'function'")
            page.evaluate("""() => {
                singleIssueAnalysisHistory = Array.from({length: 12}, (_, index) => ({
                    run: {run_id: 'history-' + index, status: 'completed', started_at: '2026-09-16T10:00:00'},
                    issues: [{issue_id: 650000 + index, subject: 'Issue title ' + index, status: 'completed'}]
                }));
                renderSingleIssueAnalysisHistory();
                // 晨报摘要区给足真实内容：工具栏下方还有多行行卡片，才能滚到吸附点。
                dailyBriefCache = {
                    run: {run_id: 'brief-1', status: 'completed', report_json: {}},
                    issues: Array.from({length: 8}, (_, index) => ({
                        issue_id: 653000 + index, subject: '待回复 ' + index,
                        status: 'completed', result: {},
                    })),
                };
                document.getElementById('dailyBriefCard').innerHTML = renderDailyBriefInner(dailyBriefCache);
                syncDailyBriefStickyTop();
            }""")
            header_h = page.evaluate(
                "Math.ceil(document.querySelector('body > header').getBoundingClientRect().height)")
            single_bar = page.locator(
                '#tab-daily-brief .daily-brief-single-section .daily-brief-toolbar')
            summary_bar = page.locator('#tab-daily-brief .daily-brief-summary .daily-brief-toolbar')
            page.evaluate("window.scrollTo(0, 999999)")
            page.wait_for_timeout(120)
            # 深滚到晨报区：摘要工具栏吸附在页头下缘；「单号分析」工具栏
            # 随自己的区块滚出视口（top 为负）——同时只有一条工具栏驻留。
            self.assertAlmostEqual(
                summary_bar.evaluate('(node) => node.getBoundingClientRect().top'),
                header_h, delta=1)
            self.assertLess(
                single_bar.evaluate('(node) => node.getBoundingClientRect().top'),
                header_h - 10)
            # 回到顶部：两条工具栏都在自然位置（未被吸附，卡片上沿有间距）。
            page.evaluate("window.scrollTo(0, 0)")
            page.wait_for_timeout(120)
            self.assertGreater(
                single_bar.evaluate('(node) => node.getBoundingClientRect().top'),
                header_h + 5)
            self.assertGreater(
                summary_bar.evaluate('(node) => node.getBoundingClientRect().top'),
                header_h + 10)
            # 卡片是 clip 裁切（不产生滚动容器），工具栏 sticky 才能生效。
            self.assertEqual(
                page.locator('.daily-brief-card').evaluate(
                    '(node) => getComputedStyle(node).overflow'),
                'clip')
        finally:
            page.close()

    def test_dynamic_category_is_rendered_as_text_on_repeated_updates(self):
        page = self.new_page()
        try:
            self.goto_shell(page)
            page.wait_for_function("typeof updateCategorySelect === 'function'")
            payload = '\"><img src=x onerror="window.categoryInjected=true">'
            result = page.evaluate("""name => {
                categorizedTools = { [name]: [] };
                currentCategory = 'all';
                updateCategorySelect();
                updateCategorySelect();
                const select = document.getElementById('tool-category');
                return { count: select.options.length, value: select.options[0].value,
                    text: select.options[0].textContent, htmlNodes: select.querySelectorAll('img').length,
                    injected: Boolean(window.categoryInjected) };
            }""", payload)
            self.assertEqual(result["count"], 1)
            self.assertEqual(result["value"], payload)
            self.assertIn(payload, result["text"])
            self.assertEqual(result["htmlNodes"], 0)
            self.assertFalse(result["injected"])
        finally:
            page.close()

    def test_long_brief_modal_actions_reachable_on_small_screens(self):
        page = self.new_page()
        page.route("**/api/redmine-agent/**", lambda route: route.fulfill(
            status=200, content_type="application/json", body='{"success":true,"data":{}}',
        ))
        try:
            page.goto(f"{self.base_url}/redmine-agent", wait_until="domcontentloaded")
            page.wait_for_function("typeof renderDailyBriefInner === 'function'")
            page.evaluate("switchTab('daily-brief')")
            page.wait_for_load_state("networkidle")
            page.evaluate("""() => {
                dailyBriefCache = {
                    run: { run_id: 'fixture', brief_date: '2026-09-15', status: 'completed', report_json: {} },
                    issues: [{ issue_id: 101, subject: 'LongSubject'.repeat(80), status: 'completed',
                        result: {problem_summary: '需要补充日志', suggested_solution: '复现并上传 bugreport',
                            result_format: 'kkagent_markdown', evidence_gate: {analysis_mode: 'diagnostic'},
                            detailed_report: '长报告'.repeat(700) + '\\n```\\n' + 'log'.repeat(500) + '\\n```'} }]
                };
                document.getElementById('dailyBriefCard').innerHTML = renderDailyBriefInner(dailyBriefCache);
            }""")
            for width in (360, 390, 768, 1440):
                page.set_viewport_size({"width": width, "height": 800})
                page.locator('#dailyBriefCard [data-click="showDailyBriefIssue"]').click()
                modal = page.locator('[id^="singleIssueAnalysisModal-"].show')
                expect(modal).to_be_visible()
                if width >= 1000:
                    header_bottom = page.locator('body > header').bounding_box()['y'] + page.locator('body > header').bounding_box()['height']
                    reader_box = modal.locator('.daily-brief-modal').bounding_box()
                    self.assertAlmostEqual(reader_box['x'], 8, delta=1)
                    self.assertAlmostEqual(reader_box['y'], header_bottom, delta=1)
                    self.assertAlmostEqual(reader_box['width'], width - 16, delta=1)
                    self.assertAlmostEqual(reader_box['height'], 800 - header_bottom - 6, delta=1)
                self.assertEqual(modal.locator('.daily-brief-section-md summary').count(), 0)
                self.assertEqual(modal.locator('details').count(), 0)
                self.assertEqual(modal.locator('[data-daily-brief-save-case]').count(), 0)
                self.assertNotIn('结构化明细', modal.inner_text())
                title = modal.locator('.daily-brief-modal-title')
                self.assertEqual(title.evaluate('(node) => getComputedStyle(node).whiteSpace'), 'nowrap')
                self.assertEqual(title.evaluate('(node) => getComputedStyle(node).flexDirection'), 'row')
                self.assertEqual(
                    modal.locator('.daily-brief-modal-subject').evaluate(
                        '(node) => getComputedStyle(node).whiteSpace'
                    ),
                    'nowrap',
                )
                close = modal.locator('.modal-close')
                self.assertEqual(
                    close.evaluate('(node) => getComputedStyle(node).fontSize'),
                    '13px' if width < 760 else '14px',
                )
                footer_button = modal.locator('.daily-brief-modal-footer button').first
                self.assertEqual(
                    footer_button.evaluate('(node) => getComputedStyle(node).height'),
                    '24px' if width < 760 else '26px',
                )
                action = modal.locator('[data-click="openSingleIssueAnalysisTimeline"]')
                for control in (close, action):
                    control.scroll_into_view_if_needed()
                    box = control.bounding_box()
                    self.assertGreaterEqual(box["x"], 0)
                    self.assertLessEqual(box["x"] + box["width"], width + 1)
                    self.assertLessEqual(box["y"] + box["height"], 801)
                close.click()
                expect(modal).to_have_count(0)
        finally:
            page.close()

    def test_running_issue_view_opens_live_timeline_and_switches_to_report(self):
        """运行中的分析：时间线增量轮询 → 终态后原地切换最终报告。"""
        page = self.new_page()
        state = {
            "sequence": 0,
            "events": [
                {"sequence": 1, "event_type": "analysis_started", "stage": "", "tool_name": "",
                 "status": "", "summary": "开始分析 #652654", "duration_ms": 0, "created_at": "2026-09-16T10:00:00"},
                {"sequence": 2, "event_type": "tool_started", "stage": "preflight",
                 "tool_name": "gms_rt_redmine_issue_fetch", "status": "",
                 "summary": "gms_rt_redmine_issue_fetch(issue_id=652654)", "duration_ms": 0,
                 "created_at": "2026-09-16T10:00:05"},
            ],
            "run_status": "analyzing",
        }

        def respond(route):
            url = route.request.url
            if "/events" in url:
                after = int(url.split("after_sequence=")[1].split("&")[0])
                if state["sequence"] < 2:
                    # 前两轮：仍在分析，时间线先渲染并完成实时态断言；终态
                    # 留给第三轮，避免报告原地切换吃掉断言窗口。
                    state["sequence"] += 1
                else:
                    # 第三轮轮询：推进到终态，触发原地切换。
                    state["events"] = state["events"] + [
                        {"sequence": 3, "event_type": "tool_completed", "stage": "preflight",
                         "tool_name": "gms_rt_redmine_issue_fetch", "status": "success",
                         "summary": "gms_rt_redmine_issue_fetch(issue_id=652654)", "duration_ms": 841,
                         "created_at": "2026-09-16T10:00:06"},
                        {"sequence": 4, "event_type": "analysis_completed", "stage": "",
                         "tool_name": "", "status": "success", "summary": "分析完成", "duration_ms": 0,
                         "created_at": "2026-09-16T10:05:00"},
                    ]
                    state["run_status"] = "completed"
                payload = {
                    "events": [event for event in state["events"] if event["sequence"] > after],
                    "next_sequence": state["events"][-1]["sequence"],
                    "run_status": state["run_status"],
                    "terminal": state["run_status"] in ("completed", "failed", "cancelled", "partial"),
                }
                route.fulfill(status=200, content_type="application/json",
                              body=json.dumps({"success": True, "data": payload}))
                return
            if url.endswith("/daily-brief/runs/timeline-fixture"):
                data = {"run": {"run_id": "timeline-fixture", "status": "completed", "mode": "issue:652654:full:x"},
                        "issues": [{"issue_id": 652654, "subject": "自动亮度异常", "status": "completed",
                                    "result": {"detailed_report": "# 最终结论\n\n已修复"}}]}
                route.fulfill(status=200, content_type="application/json",
                              body=json.dumps({"success": True, "data": data}))
                return
            if url.endswith("/daily-brief/issue-analyses?limit=30"):
                route.fulfill(status=200, content_type="application/json", body='{"success":true,"data":{"items":[]}}')
                return
            route.fulfill(status=200, content_type="application/json", body='{"success":true,"data":{}}')

        page.route("**/api/redmine-agent/**", respond)
        try:
            page.goto(f"{self.base_url}/redmine-agent", wait_until="domcontentloaded")
            page.wait_for_function("typeof showAnalysisTimelineModal === 'function'")
            page.evaluate("switchTab('daily-brief')")
            page.evaluate(
                "showAnalysisTimelineModal('timeline-fixture', 652654, {subject: '自动亮度异常'})"
            )
            timeline = page.locator('[id^="analysisTimelineModal-"] .analysis-timeline')
            expect(timeline).to_contain_text("读取 Redmine 工单详情 issue_id=652654")
            expect(timeline.locator(".analysis-timeline-tool").first).to_have_text("gms_rt_redmine_issue_fetch")
            # 终态到达后：时间线原地切换为最终报告（不关弹框重开），轮询停止，
            # 停止按钮随收尾移除；「查看报告 / 查看过程」可互相切换。
            # 终态在第三轮轮询（约 5s）才注入，给 10s 窗口避免贴边抖动。
            expect(page.locator(".analysis-timeline-final")).to_contain_text("已修复", timeout=10_000)
            expect(page.locator('[data-analysis-timeline-stop]')).to_have_count(0)
            self.assertIsNone(page.evaluate("analysisTimelineState.timer"))
            self.assertTrue(page.evaluate("analysisTimelineState.terminal"))
            # 用 .modal 前缀限定根节点，避免 [id^=] 同时命中 -title/-timeline/-meta 子元素。
            modal = page.locator('.modal[id^="analysisTimelineModal-"]')
            # 终态默认报告视图，单切换按钮显示「查看过程」，关闭按钮固定最右。
            toggle = modal.locator('[data-analysis-timeline-toggle]')
            expect(toggle).to_be_visible()
            self.assertEqual(toggle.inner_text(), '查看过程')
            # 报告视图标题是「分析总结」，不带「已结束」徽标（徽标只属过程视图）。
            report_title = modal.locator('.modal-title').inner_text()
            self.assertIn('分析总结', report_title)
            self.assertNotIn('已结束', report_title)
            footer_buttons = modal.locator('.modal-buttons > button')
            self.assertEqual(
                footer_buttons.last.evaluate('(node) => node.textContent'), '关闭')
            toggle.click()
            expect(page.locator('[id^="analysisTimelineModal-"] .analysis-timeline')).to_contain_text('分析完成')
            expect(toggle).to_have_text('查看报告')
            # 切回过程视图：标题恢复「执行过程 + 徽标」。
            self.assertIn('执行过程', modal.locator('.modal-title').inner_text())
            toggle.click()
            expect(page.locator(".analysis-timeline-final")).to_contain_text("已修复")
            expect(toggle).to_have_text('查看过程')
            self.assertIn('分析总结', modal.locator('.modal-title').inner_text())
        finally:
            page.close()

    def test_history_timeline_replays_completed_run_without_polling(self):
        """历史回看：终态 run 完整时间线 + 终态徽标，不轮询，可切到报告。"""
        page = self.new_page()
        event_requests = []

        def respond(route):
            url = route.request.url
            if "/events" in url:
                event_requests.append(url)
                events = [
                    {"sequence": 1, "event_type": "analysis_started", "stage": "", "tool_name": "",
                     "status": "", "summary": "开始分析 #652654", "duration_ms": 0,
                     "created_at": "2026-09-16T10:00:00"},
                    {"sequence": 2, "event_type": "tool_started", "stage": "preflight",
                     "tool_name": "gms_rt_redmine_issue_fetch", "status": "",
                     "summary": "gms_rt_redmine_issue_fetch(issue_id=652654)", "duration_ms": 0,
                     "created_at": "2026-09-16T10:00:05"},
                    {"sequence": 3, "event_type": "analysis_completed", "stage": "", "tool_name": "",
                     "status": "success", "summary": "分析完成", "duration_ms": 0,
                     "created_at": "2026-09-16T10:05:00"},
                ]
                after = int(url.split("after_sequence=")[1].split("&")[0])
                payload = {
                    "events": [event for event in events if event["sequence"] > after],
                    "next_sequence": 3,
                    "run_status": "completed",
                    "terminal": True,
                }
                route.fulfill(status=200, content_type="application/json",
                              body=json.dumps({"success": True, "data": payload}))
                return
            if url.endswith("/daily-brief/runs/history-fixture"):
                data = {"run": {"run_id": "history-fixture", "status": "completed",
                                "mode": "issue:652654:full:x", "finished_at": "2026-09-16T10:05:00"},
                        "issues": [{"issue_id": 652654, "subject": "自动亮度异常", "status": "completed",
                                    "result": {"detailed_report": "# 历史结论\n\n已回看"}}]}
                route.fulfill(status=200, content_type="application/json",
                              body=json.dumps({"success": True, "data": data}))
                return
            route.fulfill(status=200, content_type="application/json", body='{"success":true,"data":{}}')

        page.route("**/api/redmine-agent/**", respond)
        try:
            page.goto(f"{self.base_url}/redmine-agent", wait_until="domcontentloaded")
            page.wait_for_function("typeof showAnalysisTimelineModal === 'function'")
            page.evaluate("switchTab('daily-brief')")
            page.evaluate(
                "showAnalysisTimelineModal('history-fixture', 652654, "
                "{subject: '自动亮度异常', history: true})"
            )
            modal = page.locator('.modal[id^="analysisTimelineModal-"]')
            timeline = modal.locator('.analysis-timeline')
            expect(timeline).to_contain_text("读取 Redmine 工单详情 issue_id=652654")
            expect(timeline.locator(".analysis-timeline-tool").first).to_have_text("gms_rt_redmine_issue_fetch")
            expect(modal.locator(".analysis-timeline-stage")).to_contain_text("Controller 证据预采集")
            expect(timeline).to_contain_text("分析完成")
            # 历史模式：标题带终态徽标，无停止按钮，默认停留完整时间线。
            self.assertIn("已结束", modal.locator(".modal-title").inner_text())
            self.assertEqual(modal.locator('[data-analysis-timeline-stop]').count(), 0)
            toggle = modal.locator('[data-analysis-timeline-toggle]')
            expect(toggle).to_be_visible()
            self.assertEqual(toggle.inner_text(), "查看报告")
            # 完整时间线一次性取完：只有一轮 after_sequence=0 请求，之后无轮询。
            page.wait_for_timeout(3200)
            self.assertEqual(len(event_requests), 1)
            self.assertIn("after_sequence=0", event_requests[0])
            # 切到报告再切回过程：同一个按钮只换文案，位置不跳动；
            # 关闭按钮始终固定在 footer 最右。
            footer_close = modal.locator('.modal-buttons [data-analysis-timeline-close]')
            toggle_box_before = toggle.bounding_box()
            self.assertGreater(
                footer_close.bounding_box()['x'], toggle_box_before['x'] + 10,
                '关闭按钮应始终位于切换按钮右侧')
            toggle.click()
            expect(page.locator(".analysis-timeline-final")).to_contain_text("已回看")
            self.assertEqual(toggle.inner_text(), "查看过程")
            # 标题跟随视图：报告视图「分析总结」，切回过程恢复「执行过程」+ 徽标。
            self.assertIn("分析总结", modal.locator(".modal-title").inner_text())
            self.assertNotIn("执行过程", modal.locator(".modal-title").inner_text())
            toggle_box_after = toggle.bounding_box()
            # 只换文案不换位置：x/y 允许亚像素抖动。
            self.assertAlmostEqual(toggle_box_after['x'], toggle_box_before['x'], delta=1)
            self.assertAlmostEqual(toggle_box_after['y'], toggle_box_before['y'], delta=1)
            toggle.click()
            expect(timeline).to_contain_text("分析完成")
            self.assertEqual(toggle.inner_text(), "查看报告")
            self.assertIn("执行过程", modal.locator(".modal-title").inner_text())
        finally:
            page.close()

    def test_report_modal_offers_execution_history_entry_within_retention(self):
        """报告弹框入口：30 天内终态 run 可回看过程，超期按钮禁用。"""
        page = self.new_page()
        recent_finished = (datetime.now() - timedelta(days=1)).isoformat(timespec="seconds")
        expired_finished = (datetime.now() - timedelta(days=40)).isoformat(timespec="seconds")

        def respond(route):
            url = route.request.url
            if url.endswith("/daily-brief/issue-analyses?limit=30"):
                data = {"items": [
                    {"run": {"run_id": "saved-history-1", "status": "completed",
                             "finished_at": recent_finished},
                     "issues": [{"issue_id": 652001, "subject": "近期完成", "status": "completed",
                                 "result": {"detailed_report": "近期报告"}}]},
                    {"run": {"run_id": "saved-history-2", "status": "completed",
                             "finished_at": expired_finished},
                     "issues": [{"issue_id": 652002, "subject": "早已过期", "status": "completed",
                                 "result": {"detailed_report": "过期报告"}}]},
                ]}
                route.fulfill(status=200, content_type="application/json",
                              body=json.dumps({"success": True, "data": data}))
                return
            if "/events" in url:
                payload = {
                    "events": [{"sequence": 1, "event_type": "analysis_completed", "stage": "",
                                "tool_name": "", "status": "success", "summary": "分析完成",
                                "duration_ms": 0, "created_at": recent_finished}],
                    "next_sequence": 1, "run_status": "completed", "terminal": True,
                }
                route.fulfill(status=200, content_type="application/json",
                              body=json.dumps({"success": True, "data": payload}))
                return
            if url.endswith("/daily-brief/runs/saved-history-1"):
                data = {"run": {"run_id": "saved-history-1", "status": "completed",
                                "mode": "issue:652001:full:x", "finished_at": recent_finished},
                        "issues": [{"issue_id": 652001, "subject": "近期完成", "status": "completed",
                                    "result": {"detailed_report": "近期报告"}}]}
                route.fulfill(status=200, content_type="application/json",
                              body=json.dumps({"success": True, "data": data}))
                return
            route.fulfill(status=200, content_type="application/json", body='{"success":true,"data":{}}')

        page.route("**/api/redmine-agent/**", respond)
        try:
            page.goto(f"{self.base_url}/redmine-agent", wait_until="domcontentloaded")
            page.evaluate("switchTab('daily-brief')")
            history = page.locator('#singleIssueAnalysisHistory')
            expect(history).to_contain_text('#652001')
            # 30 天内：报告弹框上的「执行过程」可点，进入历史时间线（不闪切报告）。
            page.locator('article[data-single-issue-id="652001"]').get_by_text('查看分析', exact=True).click()
            modal = page.locator('.daily-brief-modal').last
            expect(modal).to_contain_text('近期报告')
            modal.get_by_text('执行过程', exact=True).click()
            timeline = page.locator('.modal[id^="analysisTimelineModal-"]')
            expect(timeline.locator('.analysis-timeline')).to_contain_text('分析完成')
            self.assertIn('已结束', timeline.locator('.modal-title').inner_text())
            self.assertEqual(timeline.locator('.analysis-timeline-final').count(), 0)
            # 报告弹框被时间线弹框替换而非叠加：只剩一个弹框，关一次即可。
            expect(page.locator('.modal.show')).to_have_count(1)
            timeline.locator('.modal-close').click()
            expect(page.locator('.modal.show')).to_have_count(0)
            # 超期（>30 天）：按钮禁用并提示已清理。
            page.locator('article[data-single-issue-id="652002"]').get_by_text('查看分析', exact=True).click()
            expired_modal = page.locator('.daily-brief-modal').last
            expect(expired_modal).to_contain_text('过期报告')
            expired_button = expired_modal.get_by_text('执行过程', exact=True)
            expect(expired_button).to_be_disabled()
        finally:
            page.close()

    def test_reader_modal_keeps_page_header_pinned_on_scrolled_page(self):
        """滚动后打开阅读器弹框：页签栏固定在视口顶部。

        回归：html.modal-open 的 overflow:hidden 会让 sticky 页头失锁滚出
        视口，弹框又按页头高度下移，顶部便露出被裁切的正文（弹框期间页头
        position:fixed 兜底）。
        """
        page = self.new_page()
        page.route('**/api/redmine-agent/**', lambda route: route.fulfill(
            status=200, content_type='application/json', body='{"success":true,"data":{}}',
        ))
        try:
            page.goto(f'{self.base_url}/redmine-agent', wait_until='domcontentloaded')
            page.set_viewport_size({'width': 1200, 'height': 500})
            page.evaluate("switchTab('daily-brief')")
            page.evaluate("""() => {
                singleIssueAnalysisHistory = Array.from({length: 9}, (_, index) => ({
                    run: {run_id: 'history-' + index, status: 'completed', started_at: '2026-09-16T10:00:00'},
                    issues: [{issue_id: 650000 + index, subject: 'Issue title ' + index, status: 'completed'}]
                }));
                renderSingleIssueAnalysisHistory();
            }""")
            page.evaluate("window.scrollTo(0, 400)")
            self.assertGreater(
                page.evaluate("window.scrollY"), 0, '前置条件：页面应处于滚动状态')
            page.locator('#singleIssueAnalysisHistory article').first.get_by_text(
                '查看分析', exact=True).click()
            modal = page.locator('.daily-brief-analysis-overlay').last
            expect(modal).to_be_visible()
            geometry = page.evaluate("""() => {
                const header = document.querySelector('body > header');
                const reader = document.querySelector('.daily-brief-analysis-overlay .daily-brief-modal');
                return {headerTop: header.getBoundingClientRect().top,
                        headerBottom: header.getBoundingClientRect().bottom,
                        readerTop: reader.getBoundingClientRect().top};
            }""")
            # 页签栏固定在顶部，弹框从页头下缘开始，顶部不露正文。
            self.assertAlmostEqual(geometry['headerTop'], 0, delta=1)
            self.assertAlmostEqual(geometry['readerTop'], geometry['headerBottom'], delta=1)
            page.evaluate("""() => window.EmbeddedModalController.remove(
                document.querySelector('.daily-brief-analysis-overlay').id)""")
            expect(page.locator('.daily-brief-analysis-overlay')).to_have_count(0)
            self.assertEqual(
                page.evaluate("getComputedStyle(document.documentElement).overflowY"), 'scroll')
            self.assertAlmostEqual(
                page.evaluate("document.querySelector('body > header').getBoundingClientRect().top"),
                0, delta=1)
        finally:
            page.close()

    def test_timeline_polling_stops_after_modal_close(self):
        """关闭弹框后：abort 在途请求、自检弹框已移除并停止轮询调度。

        清理遵循页面既有的 isConnected 检查模式：轮询循环在下一拍发现
        弹框节点被移除后自行退出（ModalManager 仍是显示控制的唯一所有者）。
        """
        page = self.new_page()
        release_route = {"handler": None}
        events_after_close = {"count": 0}

        def respond(route):
            url = route.request.url
            if "/events" in url:
                # 挂起首个事件请求，模拟慢响应：关闭弹框时应被 abort。
                if release_route["handler"] is None:
                    release_route["handler"] = route
                else:
                    events_after_close["count"] += 1
                    route.fulfill(status=200, content_type="application/json",
                                  body='{"success":true,"data":{"events":[],"next_sequence":0,'
                                       '"run_status":"analyzing","terminal":false}}')
                return
            route.fulfill(status=200, content_type="application/json", body='{"success":true,"data":{}}')

        page.route("**/api/redmine-agent/**", respond)
        try:
            page.goto(f"{self.base_url}/redmine-agent", wait_until="domcontentloaded")
            page.wait_for_function("typeof showAnalysisTimelineModal === 'function'")
            page.evaluate("switchTab('daily-brief')")
            page.evaluate("showAnalysisTimelineModal('timeline-fixture', 652654, {})")
            page.wait_for_timeout(300)
            self.assertIsNotNone(release_route["handler"])
            modal_id = page.evaluate("analysisTimelineState.modalId")
            page.evaluate(f"removeDynamicModal({json.dumps(modal_id)})")
            # 关闭后放行挂起请求：abort 语义 → 轮询发现弹框已移除并退出。
            release_route["handler"].abort("connectionreset")
            page.wait_for_function("analysisTimelineState.runId === ''")
            self.assertIsNone(page.evaluate("analysisTimelineState.timer"))
            page.wait_for_timeout(3200)
            self.assertEqual(events_after_close["count"], 0)
        finally:
            page.close()
