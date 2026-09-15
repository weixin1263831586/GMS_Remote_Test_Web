"""Browser regressions for untrusted categories and long brief content."""

import json

from tests.test_runtime_ui_smoke import RuntimeUiHarness, expect


class DailyBriefReviewUiTests(RuntimeUiHarness):
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
                expect(page.locator('#singleIssueAnalysisResult')).to_contain_text('仅分析 #' + str(issue_id))
                expect(page.locator('#singleIssueAnalysisStart')).to_be_enabled()
            self.assertEqual(submissions, [{'issue_id': 647338}, {'issue_id': 123}])
            page.set_viewport_size({'width': 1440, 'height': 900})
            centers = page.evaluate("""() => {
                document.getElementById('dailyBriefCard').innerHTML = renderDailyBriefInner({
                    run: {run_id: 'cancelled-fixture', status: 'cancelled',
                        brief_date: '2026-09-15', finished_at: '2026-09-15T21:17:00'}, issues: []
                });
                return ['.daily-brief-toolbar .daily-brief-title', '#singleIssueAnalysisId',
                    '#singleIssueAnalysisStart', '#dailyBriefRunControls .daily-brief-status',
                    '#dailyBriefRunControls button'].map(selector => {
                        const box = document.querySelector(selector).getBoundingClientRect();
                        return box.y + box.height / 2;
                    });
            }""")
            self.assertLess(max(centers) - min(centers), 2)
            self.assertEqual(page.locator('#singleIssueAnalysisId').input_value(), '123')
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
