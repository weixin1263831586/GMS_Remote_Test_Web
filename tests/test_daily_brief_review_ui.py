"""Browser regressions for untrusted categories and long brief content."""

from tests.test_runtime_ui_smoke import RuntimeUiHarness, expect


class DailyBriefReviewUiTests(RuntimeUiHarness):
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
                            detailed_report: '长报告'.repeat(700) + '\\n```\\n' + 'log'.repeat(500) + '\\n```'} }]
                };
                document.getElementById('dailyBriefCard').innerHTML = renderDailyBriefInner(dailyBriefCache);
            }""")
            for width in (360, 390, 768):
                page.set_viewport_size({"width": width, "height": 800})
                page.locator('#dailyBriefCard [data-click="showDailyBriefIssue"]').click()
                modal = page.locator('[id^="dailyBriefIssueModal-"].show')
                expect(modal).to_be_visible()
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
