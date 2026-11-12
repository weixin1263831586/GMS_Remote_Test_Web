"""A Redmine background analysis returns progress and evidence to the same chat."""

import json

from tests.runtime_ui.harness import RuntimeUiHarness, expect


class RuntimeAgentRedmineTests(RuntimeUiHarness):
    def test_analysis_progress_and_report_render_in_the_original_session(self):
        page = self.new_page()
        session_id = 'redmine-analysis-session'
        issued = []
        polls = []
        timestamp = '2026-11-12T09:00:00Z'
        message = '分析 https://redmine.example.test/issues/661332'
        report = '工单 #661332 分析完成：kernel_vendor.log 已作为普通日志取证。'

        def session(complete=False, running=False):
            return {
                'session_id': session_id, 'status': 'idle' if complete else 'monitoring',
                'agent_backend': {'mode': 'kkagent', 'state': 'ready'},
                'active_run': None if complete else {'redmine_run_id': 'run-1', 'redmine_issue_id': 661332},
                'messages': [
                    {'id': 'user-1', 'role': 'user', 'content': message, 'created_at': timestamp},
                    {'id': 'queued-1', 'role': 'assistant', 'content': '已提交 Redmine #661332 分析，正在跟踪任务进度。',
                     'created_at': timestamp},
                ] + ([{'id': 'report-1', 'role': 'assistant', 'created_at': timestamp,
                       'content': report + '\n<svg onload="window.redmineInjected=true"></svg>',
                       'data': {'redmine_run_id': 'run-1', 'redmine_issue_id': 661332}}] if complete else []),
                'steps': [{'id': 'analysis-step', 'title': 'Redmine #661332 分析',
                           'status': 'done' if complete else 'running',
                           'detail': '分析完成' if complete else '正在取证和分析' if running else '排队中',
                           'created_at': timestamp, 'data': {'redmine_run_id': 'run-1'}}],
            }

        def chat(route):
            issued.append(route.request.post_data_json)
            route.fulfill(content_type='application/json', body=json.dumps({'success': True, 'data': {'session': session()}}))

        def poll(route):
            polls.append(route.request.url)
            route.fulfill(content_type='application/json', body=json.dumps({
                'success': True, 'data': {'session': session(complete=len(polls) > 1, running=True)},
            }))

        try:
            page.route('**/api/agent/chat', chat)
            page.route(f'**/api/agent/sessions/{session_id}', poll)
            page.route('**/api/agent/sessions', lambda route: route.fulfill(
                content_type='application/json', body='{"success":true,"data":{"sessions":[]}}'))
            self.goto_shell(page)
            page.evaluate("switchPage('agent', null)")
            page.locator('#agent-input').fill(message)
            page.locator('#page-agent').get_by_role('button', name='发送', exact=True).click()
            expect(page.locator('#agent-steps')).to_contain_text('排队中')
            expect(page.locator('#agent-steps')).to_contain_text('正在取证和分析')
            expect(page.locator('#agent-chat-messages')).to_contain_text(report)
            expect(page.locator('.agent-step[data-status="done"]')).to_have_count(1)
            expect(page.locator('#agent-chat-messages [onload]')).to_have_count(0)
            self.assertFalse(page.evaluate('Boolean(window.redmineInjected)'))
            self.assertEqual(len(issued), 1)
            self.assertEqual(issued[0]['message'], message)
            self.assertTrue(all(url.endswith(session_id) for url in polls))
            expect(page.locator('#agent-chat-messages')).not_to_contain_text('Missing issue_url')
        finally:
            page.close()
