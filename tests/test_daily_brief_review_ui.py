"""Browser regressions for untrusted categories and long brief content."""

import json
import re

from tests.test_runtime_ui_smoke import RuntimeUiHarness, expect


class DailyBriefReviewUiTests(RuntimeUiHarness):
    def test_single_issue_history_search_and_pagination(self):
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
            page.locator('#singleIssueAnalysisId').fill('title 3')
            expect(history).to_contain_text('#650003')
            self.assertNotIn('#650008', history.inner_text())
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
            expect(page.locator('#singleIssueAnalysisStop')).to_be_hidden()
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
            expect(page.locator('#singleIssueAnalysisStop')).to_be_visible()
            self.assertEqual(page.locator('#singleIssueAnalysisId').input_value(), '652654')
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
            self.assertEqual(page.locator('#singleIssueAnalysisId').input_value(), '123')
        finally:
            page.close()

    def test_single_issue_device_picker_only_offers_available_adb_devices(self):
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
                {'device_id': 'ADB-OTHER', 'protocol': 'adb', 'status': 'online', 'locked': True, 'locked_by_self': False},
                {'device_id': 'FASTBOOT', 'protocol': 'fastboot', 'status': 'fastboot', 'locked': False},
            ]),
        ))
        try:
            page.goto(f'{self.base_url}/redmine-agent', wait_until='domcontentloaded')
            page.evaluate("switchTab('daily-brief')")
            page.locator('#singleIssueAnalysisDevice').focus()
            expect(page.locator('#singleIssueAnalysisDevice')).to_contain_text('ADB-OWN')
            self.assertNotIn('ADB-OTHER', page.locator('#singleIssueAnalysisDevice').inner_text())
            page.locator('#singleIssueAnalysisDevice').select_option('ADB-OWN')
            page.locator('#singleIssueAnalysisId').fill('652654')
            page.locator('#singleIssueAnalysisId').press('Enter')
            expect(page.locator('#singleIssueAnalysisHistory')).to_contain_text('#652654')
            self.assertEqual(submissions, [{
                'issue_id': 652654, 'analysis_mode': 'full', 'device_serial': 'ADB-OWN',
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
            for width in (360, 390, 768):
                page.set_viewport_size({"width": width, "height": 800})
                page.locator('#dailyBriefCard [data-click="showDailyBriefIssue"]').click()
                modal = page.locator('[id^="dailyBriefIssueModal-"].show')
                expect(modal).to_be_visible()
                self.assertEqual(modal.locator('.daily-brief-section-md summary').count(), 0)
                self.assertEqual(modal.locator('[data-daily-brief-save-case]').count(), 0)
                self.assertNotIn('结构化明细', modal.inner_text())
                self.assertFalse(modal.locator('details').first.evaluate('(node) => node.open'))
                close = modal.locator('.modal-close')
                action = modal.locator('[data-daily-brief-reanalyze]')
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
