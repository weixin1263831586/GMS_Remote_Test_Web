"""Project-wide questions are schema-driven; blocked confirmation remains visible."""

import json

from tests.runtime_ui.harness import RuntimeUiHarness, expect


class RuntimeAgentQuestionTests(RuntimeUiHarness):
    def test_native_report_action_opens_the_exact_worker_suite_and_directory_on_repeated_navigation(self):
        page = self.new_page()
        try:
            self.goto_shell(page)
            page.evaluate("switchPage('agent', null)")
            page.evaluate("""() => {
                window.nativeBrowseVisits = [];
                window.loadSuiteWorkerSelector = async () => {
                    const select = document.getElementById('suite-worker-select');
                    select.innerHTML = '<option value="worker-a">A</option><option value="worker-b">B</option>';
                    select.value = 'worker-b';
                    select.disabled = false;
                };
                window.loadSuitesForBrowserWorker = async () => {
                    const worker = document.getElementById('suite-worker-select').value;
                    _browserSuitesWorkerId = worker;
                    _browserSuitesCache = [{worker_id: worker, test_type: 'gts', version: '14_r4',
                        tools_path: worker === 'worker-a' ? '/suites/GTS & #1/tools' : '/remote/GTS/tools'}];
                    return _browserSuitesCache;
                };
                window.loadSuiteBrowserDirectory = async path => {
                    nativeBrowseVisits.push({worker: document.getElementById('suite-worker-select').value,
                        suite: state.suiteBrowser.selectedSuitePath, path});
                };
            }""")
            targets = [
                {"worker_id": "worker-a", "suite_path": "/suites/GTS & #1/tools", "path": "results/GTSRUN~1"},
                {"worker_id": "worker-b", "suite_path": "/remote/GTS/tools", "path": "results/2026.11.13_14.22.45.723_8397"},
            ]
            for count, target in enumerate(targets, 1):
                page.evaluate("switchPage('agent', null)")
                page.evaluate("""params => renderAgentMessages({status: 'idle', messages: [{
                    role: 'assistant', content: '已找到原生结果', data: {quick_actions: [{
                        label: '查看原生报告目录', page: 'test-suites', params
                    }]}
                }]})""", target)
                button = page.get_by_role("button", name="查看原生报告目录", exact=True)
                expect(button).to_be_visible()
                expect(page.locator('#agent-chat-messages button[data-a0="reports"]')).to_have_count(0)
                button.click()
                page.wait_for_function(f"nativeBrowseVisits.length === {count}")
                expect(page.locator('#page-test-suites')).to_be_visible()
                visit = page.evaluate("nativeBrowseVisits[nativeBrowseVisits.length - 1]")
                self.assertEqual(visit, {"worker": target["worker_id"], "suite": target["suite_path"], "path": target["path"]})
                self.assertEqual(page.evaluate("getSuiteBrowserRouteParams().workerId"), target["worker_id"])
        finally:
            page.close()

    def test_background_planning_polls_until_resource_choices_are_ready(self):
        page = self.new_page()
        planning = {'session_id': 'model-background', 'status': 'thinking', 'messages': [], 'steps': [],
                    'agent_backend': {'mode': 'kkagent', 'state': 'connecting'}}
        ready = {**planning, 'status': 'idle', 'agent_backend': {'mode': 'kkagent', 'state': 'ready'},
                 'agent_question': {'id': 'background-question'}, 'messages': [{
                     'id': 'background-question', 'role': 'assistant', 'kind': 'question', 'content': '请选择报告',
                     'data': {'question': {'fields': [{'name': 'report', 'label': '报告', 'options': [
                         {'value': 'report-1', 'label': '最近失败报告'},
                     ]}]}},
                 }]}
        try:
            page.route('**/api/agent/chat', lambda route: route.fulfill(status=200, content_type='application/json',
                       body=json.dumps({'success': True, 'data': {'session': planning}})))
            page.route('**/api/agent/sessions/model-background', lambda route: route.fulfill(status=200,
                       content_type='application/json', body=json.dumps({'success': True, 'data': {'session': ready}})))
            self.goto_shell(page)
            page.evaluate("switchPage('agent', null)")
            page.locator('#agent-input').fill('分析最近失败的报告')
            page.locator('#agent-input').press('Enter')
            expect(page.locator('#agent-status')).to_have_text('思考中')
            expect(page.locator('#agent-question-background-question')).to_be_visible()
            expect(page.locator('#agent-engine-status')).to_have_text('对话引擎：kkagent 已连接')
        finally:
            page.close()

    def test_blocked_plan_has_a_visible_disabled_confirmation_and_reason(self):
        page = self.new_page()
        try:
            self.goto_shell(page)
            page.evaluate("switchPage('agent', null)")
            page.evaluate("""() => renderAgentMessages({status: 'planning', messages: [{
                role: 'assistant', kind: 'plan', content: '工作流计划', data: {plan: {
                    type: 'workflow_draft', preflight: {ready: false, blocks: [{stage: 'device', reason: '设备被 gms 占用'}]}
                }}
            }]})""")
            button = page.locator('#agent-chat-messages button[data-click="confirmAgentPlan"]')
            expect(button).to_have_text('确认执行')
            expect(button).to_be_visible()
            expect(button).to_be_disabled()
            expect(page.locator('#agent-chat-messages')).to_contain_text('设备被 gms 占用')
            page.evaluate("""() => renderAgentMessages({status: 'planning', messages: [{
                role: 'assistant', kind: 'plan', content: '预检通过', data: {plan: {
                    type: 'workflow_draft', preflight: {ready: true, blocks: []}
                }}
            }]})""")
            expect(button).to_be_enabled()
        finally:
            page.close()

    def test_generic_resource_choices_survive_rerender_and_submit_structured_values(self):
        page = self.new_page()
        try:
            self.goto_shell(page)
            page.evaluate("switchPage('agent', null)")
            page.evaluate("""() => {
                window.questionSession = {status: 'idle', agent_question: {id: 'q-1'}, messages: [{
                    id: 'q-1', role: 'assistant', kind: 'question', content: '选择资源', data: {question: {fields: [
                        {name: 'device', label: '设备', required: true, options: [{value: 'DEV-1', label: '<img src=x onerror=alert(1)>'}]},
                        {name: 'report', label: '报告', required: true, options: [{value: 'REPORT-1', label: '昨天报告'}]},
                    ]}}
                }]};
                renderAgentMessages(questionSession);
                window.sendAgentAction = async (action, params) => {window.choiceSubmission = {action, ...JSON.parse(params)};};
            }""")
            form = page.locator('#agent-question-q-1')
            form.locator('[data-agent-choice="device"]').select_option('DEV-1')
            form.locator('[data-agent-choice="report"]').select_option('REPORT-1')
            page.evaluate('renderAgentMessages(questionSession)')
            expect(form.locator('[data-agent-choice="device"]')).to_have_value('DEV-1')
            expect(form.locator('[data-agent-choice="report"]')).to_have_value('REPORT-1')
            expect(form.locator('img')).to_have_count(0)
            form.locator('button').click()
            assert page.evaluate('choiceSubmission') == {
                'action': 'agent_choices', 'question_id': 'q-1', 'values': {'device': 'DEV-1', 'report': 'REPORT-1'},
            }
            page.evaluate("questionSession.agent_question = {id: 'q-2'}; renderAgentMessages(questionSession)")
            expect(form.locator('button')).to_be_disabled()
        finally:
            page.close()

    def test_engine_badge_reports_actual_backend_and_fallback(self):
        page = self.new_page()
        try:
            self.goto_shell(page)
            page.evaluate("switchPage('agent', null)")
            page.evaluate("renderAgentSession({status: 'idle', messages: [], steps: [], agent_backend: {mode: 'kkagent', state: 'ready'}})")
            expect(page.locator('#agent-engine-status')).to_have_text('对话引擎：kkagent 已连接')
            page.evaluate("renderAgentSession({status: 'idle', messages: [], steps: [], agent_backend: {mode: 'rules', detail: '模型暂不可用'}})")
            expect(page.locator('#agent-engine-status')).to_have_text('对话引擎：规则兜底')
            expect(page.locator('#agent-engine-status')).to_have_attribute('title', '模型暂不可用')
        finally:
            page.close()

    def test_planning_wait_and_model_failure_are_visible(self):
        page = self.new_page()
        try:
            self.goto_shell(page)
            page.evaluate("switchPage('agent', null)")
            page.evaluate("""() => renderAgentSession({status: 'thinking', messages: [], steps: [{
                title: 'Agent 规划', status: 'running', detail: '正在等待模型响应',
                data: {kind: 'agent_model_wait', started_at: new Date(Date.now() - 15000).toISOString()}
            }], agent_backend: {mode: 'kkagent', state: 'retrying'}})""")
            expect(page.locator('#agent-steps')).to_contain_text('已等待 15 秒')
            expect(page.locator('#agent-engine-status')).to_have_text('对话引擎：kkagent 重试中')
            page.evaluate("renderAgentSession({status: 'idle', messages: [], steps: [], agent_backend: {mode: 'kkagent', state: 'timeout', detail: '模型回合超时'}})")
            expect(page.locator('#agent-engine-status')).to_have_text('对话引擎：kkagent 响应超时')
            expect(page.locator('#agent-engine-status')).to_have_attribute('title', '模型回合超时')
        finally:
            page.close()

    def test_retry_button_submits_checkpoint_and_disables_stale_actions(self):
        page = self.new_page()
        try:
            self.goto_shell(page)
            page.evaluate("switchPage('agent', null)")
            page.evaluate("""() => {
                window.retrySession = {status: 'idle', agent_retry: {id: 'retry-current'}, messages: [{
                    role: 'assistant', content: '模型响应超时', data: {quick_actions: [{
                        label: '重试本轮规划', action: 'agent_retry', params: {retry_id: 'retry-current'}
                    }]}
                }]};
                renderAgentMessages(retrySession);
                window.sendAgentAction = async (action, params) => {window.retrySubmission = {action, ...JSON.parse(params)};};
            }""")
            button = page.locator('#agent-chat-messages button[data-a0="agent_retry"]')
            expect(button).to_be_enabled()
            button.click()
            assert page.evaluate('retrySubmission') == {'action': 'agent_retry', 'retry_id': 'retry-current'}
            page.evaluate("retrySession.status = 'thinking'; renderAgentMessages(retrySession)")
            expect(button).to_be_disabled()
            page.evaluate("retrySession.status = 'idle'; retrySession.agent_retry = {id: 'new-retry'}; renderAgentMessages(retrySession)")
            expect(button).to_be_disabled()
        finally:
            page.close()

    def test_human_authorization_continues_the_checkpoint_only_after_grant(self):
        page = self.new_page()
        submissions = []

        def chat(route):
            submissions.append(route.request.post_data_json)
            route.fulfill(status=200, content_type='application/json', body=json.dumps({
                'success': True, 'data': {'session': {'session_id': 'auth-flow', 'status': 'idle', 'messages': [], 'steps': []}},
            }))

        try:
            page.route('**/api/agent/chat', chat)
            self.goto_shell(page)
            page.evaluate("switchPage('agent', null)")
            page.evaluate("""() => {
                window.elevationCalls = 0;
                window.requestElevatedAccess = () => {
                    elevationCalls += 1;
                    return new Promise(resolve => {window.finishElevation = resolve;});
                };
                renderAgentSession({session_id: 'auth-flow', status: 'awaiting_authorization', steps: [],
                    agent_backend: {mode: 'kkagent', state: 'awaiting_authorization'},
                    agent_retry: {id: 'auth-retry'}, messages: [{role: 'assistant', content: '需要管理员临时授权',
                        data: {quick_actions: [{label: '管理员授权后继续', action: 'agent_elevate_retry', params: {retry_id: 'auth-retry'}}]}
                    }]});
            }""")
            expect(page.locator('#agent-status')).to_have_text('等待授权')
            expect(page.locator('#agent-engine-status')).to_have_text('对话引擎：kkagent 等待授权')
            button = page.locator('button[data-a0="agent_elevate_retry"]')
            button.click()
            button.click()
            assert page.evaluate('elevationCalls') == 1
            assert submissions == []
            page.evaluate('finishElevation(false)')
            assert submissions == []
            button.click()
            page.evaluate('finishElevation(true)')
            expect(button).to_have_count(0)
            assert len(submissions) == 1
            assert submissions[0]['session_id'] == 'auth-flow'
            assert submissions[0]['action'] == 'agent_retry'
            assert submissions[0]['params'] == {'retry_id': 'auth-retry'}
            assert 'password' not in json.dumps(submissions)
        finally:
            page.close()

    def test_authorization_does_not_resume_a_replaced_session_or_workspace(self):
        page = self.new_page()
        submissions = []
        try:
            page.route('**/api/agent/chat', lambda route: (submissions.append(route.request.post_data_json), route.abort()))
            self.goto_shell(page)
            page.evaluate("switchPage('agent', null)")
            for change in ['session', 'workspace']:
                with self.subTest(change=change):
                    page.evaluate("""() => {
                        window.testWorker = 'worker-1';
                        window.getAgentWorkspaceContext = () => ({worker_id: testWorker});
                        window.requestElevatedAccess = () => new Promise(resolve => {window.finishElevation = resolve;});
                        renderAgentSession({session_id: 'auth-flow', status: 'awaiting_authorization', steps: [],
                            agent_retry: {id: 'auth-retry'}, messages: [{role: 'assistant', content: '授权', data: {quick_actions: [{
                                label: '管理员授权后继续', action: 'agent_elevate_retry', params: {retry_id: 'auth-retry'}
                            }]}}]});
                    }""")
                    page.locator('button[data-a0="agent_elevate_retry"]').click()
                    page.evaluate("newAgentSession()" if change == 'session' else "testWorker = 'worker-2'")
                    page.evaluate('finishElevation(true)')
                    assert submissions == []
        finally:
            page.close()


    def test_gms_policy_read_requires_elevation_and_retries_only_expired_grant(self):
        page = self.new_page()
        requests = []
        try:
            self.goto_shell(page)
            page.evaluate("switchPage('agent', null)")
            page.evaluate("""() => {
                window.policyAuthCalls = 0;
                window.policyAuthGranted = false;
                window.requestElevatedAccess = async () => {
                    window.policyAuthCalls++;
                    return window.policyAuthGranted;
                };
                renderAgentMessages({status: 'idle', messages: [{
                    id: 'policy', role: 'assistant', content: '政策查询',
                    data: {gms_policy_lookup: 'section:Low RAM'}
                }]});
            }""")

            def reply(route):
                requests.append(route.request.url)
                if len(requests) == 1:
                    route.fulfill(status=403, content_type='application/json',
                                  body=json.dumps({'detail': {'elevation_required': True}}))
                else:
                    route.fulfill(status=200, content_type='application/json',
                                  body=json.dumps({'success': True, 'data': {
                                      'status': 'matched', 'sections': [{'title': 'Fixture Low RAM',
                                          'text': 'Example policy', 'table_rows': []}]
                                  }}))

            page.route('**/api/gms-update-center/requirements/clauses?*', reply)
            button = page.get_by_role('button', name='读取 GMS 政策原文', exact=True)
            button.click()
            expect(page.locator('.agent-policy-evidence')).to_contain_text('未完成管理员验证')
            self.assertEqual(requests, [])
            page.evaluate('window.policyAuthGranted = true')
            button.click()
            expect(page.locator('.agent-policy-evidence')).to_contain_text('Example policy')
            self.assertEqual(len(requests), 2)
            self.assertEqual(page.evaluate('window.policyAuthCalls'), 3)
        finally:
            page.close()

    def test_gms_policy_non_elevation_forbidden_is_not_retried(self):
        page = self.new_page()
        requests = []
        try:
            self.goto_shell(page)
            page.evaluate("switchPage('agent', null)")
            page.evaluate("""() => {
                window.requestElevatedAccess = async () => true;
                renderAgentMessages({status: 'idle', messages: [{
                    id: 'policy', role: 'assistant', content: '政策查询',
                    data: {gms_policy_lookup: 'section:Low RAM'}
                }]});
            }""")

            def forbidden(route):
                requests.append(route.request.url)
                route.fulfill(status=403, content_type='application/json',
                              body=json.dumps({'detail': {'agent_forbidden': True}}))

            page.route('**/api/gms-update-center/requirements/clauses?*', forbidden)
            page.get_by_role('button', name='读取 GMS 政策原文', exact=True).click()
            expect(page.locator('.agent-policy-evidence')).to_contain_text('当前身份无权读取')
            self.assertEqual(len(requests), 1)
        finally:
            page.close()


    def test_admin_role_authority_reads_policy_without_second_password_prompt(self):
        page = self.new_page()
        try:
            self.goto_shell(page)
            page.evaluate("switchPage('agent', null)")
            page.route('**/api/auth/status', lambda route: route.fulfill(
                status=200, content_type='application/json', body=json.dumps({
                    'authenticated': True, 'user': {'role': 'admin'},
                    'elevated': True, 'elevated_until': None,
                })))
            page.route('**/api/gms-update-center/requirements/clauses?*', lambda route: route.fulfill(
                status=200, content_type='application/json', body=json.dumps({
                    'success': True, 'data': {'status': 'matched', 'sections': [{
                        'title': 'Policy fixture', 'text': 'Example policy', 'table_rows': [],
                    }]},
                })))
            page.evaluate("""() => {
                state.currentUser = {role: 'admin'};
                state.elevated = false;
                renderAgentMessages({status: 'idle', messages: [{
                    id: 'policy', role: 'assistant', content: '政策查询',
                    data: {gms_policy_lookup: 'section:Low RAM'}
                }]});
            }""")
            page.get_by_role('button', name='读取 GMS 政策原文', exact=True).click()
            expect(page.locator('.agent-policy-evidence')).to_contain_text('Example policy')
            expect(page.locator('#elevate-modal')).not_to_be_visible()
            self.assertTrue(page.evaluate('state.elevated'))
            self.assertIsNone(page.evaluate('state.elevatedUntil'))
        finally:
            page.close()
