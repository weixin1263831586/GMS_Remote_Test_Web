import json
import re
from urllib.parse import urlparse

from playwright.sync_api import expect

from tests import test_runtime_ui_smoke as runtime_ui_smoke


INVENTORY_SCRIPT = """
(pageName) => {
  const cssPath = (element) => {
    if (element.id) return '#' + CSS.escape(element.id);
    const parts = [];
    let node = element;
    while (node && node.nodeType === Node.ELEMENT_NODE && parts.length < 5) {
      let selector = node.nodeName.toLowerCase();
      if (node.classList.length) {
        selector += '.' + Array.from(node.classList).slice(0, 2).map(CSS.escape).join('.');
      }
      const parent = node.parentElement;
      if (parent) {
        const siblings = Array.from(parent.children).filter(child => child.nodeName === node.nodeName);
        if (siblings.length > 1) selector += ':nth-of-type(' + (siblings.indexOf(node) + 1) + ')';
      }
      parts.unshift(selector);
      node = parent;
    }
    return parts.join(' > ');
  };
  const isVisible = (element) => {
    const style = getComputedStyle(element);
    const rect = element.getBoundingClientRect();
    return style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
  };
  return {
    page: pageName,
    buttons: Array.from(document.querySelectorAll('button,[role="button"],input[type="button"],input[type="submit"]'))
      .filter(isVisible)
      .map((element, index) => ({
        index,
        id: element.id || '',
        text: (element.innerText || element.value || element.getAttribute('aria-label') || element.title || '').trim().replace(/\\s+/g, ' '),
        selector: cssPath(element),
        onclick: element.getAttribute('onclick') || '',
        disabled: Boolean(element.disabled || element.getAttribute('aria-disabled') === 'true'),
      })),
    modals: Array.from(document.querySelectorAll('.modal[id]'))
      .map(element => ({ id: element.id, selector: cssPath(element) })),
  };
}
"""


CLICK_SCRIPT = """
(button) => {
  let element = document.querySelector(button.selector);
  if (!element && button.onclick) {
    element = Array.from(document.querySelectorAll('button,[role="button"],input[type="button"],input[type="submit"]'))
      .find(candidate => candidate.getAttribute('onclick') === button.onclick);
  }
  if (!element) return {clicked: false, reason: 'missing'};
  element.scrollIntoView({block: 'center', inline: 'center'});
  element.click();
  return {clicked: true};
}
"""


ALL_PAGES = [
    'test',
    'desktop',
    'terminal',
    'users',
    'devices',
    'devices-console',
    'reports',
    'report-analysis',
    'apk-analysis',
    'test-suites',
    'api-docs',
    'architecture',
    'websites',
    'tools',
    'security-audit',
    'gms-assistant',
    'automation',
    'cluster',
    'redmine-agent',
    'gerrit-dashboard',
    'agent',
    'notes',
]


SKIP_PATTERNS = [
    re.compile(pattern)
    for pattern in [
        r'\.click\(\)',
        r'window\.open',
        r'download|downloadTestLog|downloadSkillsZip|downloadApkSource|downloadTestSuite',
        r'真实 API Tick',
        r'连接VPN|connectVpn',
        r'部署脚本|copyDeployCommand',
        r'上传|选择报告|选择 APK|选择文件|打开文件',
        r'保存日志|导出',
        r'删除|移除|delete|remove|🗑️',
    ]
]


class AllControlsE2ETests(runtime_ui_smoke.RuntimeUiHarness):
    maxDiff = None

    def new_page(self):
        page = super().new_page()
        page.add_init_script(
            """
            window.__e2eUnhandledRejections = [];
            window.addEventListener('unhandledrejection', event => {
              window.__e2eUnhandledRejections.push(String(event.reason && (event.reason.stack || event.reason.message) || event.reason));
            });
            navigator.clipboard = navigator.clipboard || {};
            navigator.clipboard.writeText = async () => {};
            navigator.clipboard.readText = async () => '';
            window.open = () => ({ closed: false, focus() {} });
            """
        )
        return page

    def install_api_mocks(self, page, requests):
        def fulfill_json(route, payload):
            route.fulfill(status=200, content_type='application/json', body=json.dumps(payload))

        page.route(
            re.compile(r'.*/public/agents/[^/]+/chat(?:\?.*)?$'),
            lambda route: route.fulfill(
                status=200,
                content_type='text/html',
                body='<!doctype html><html><body><main>Mock assistant</main></body></html>',
            ),
        )

        def handle_api(route):
            request = route.request
            parsed = urlparse(request.url)
            path = parsed.path
            body = None
            if request.post_data:
                try:
                    body = request.post_data_json
                except Exception:
                    body = request.post_data
            requests.append({'method': request.method, 'path': path, 'body': body})

            if path.endswith('/api/auth/status'):
                fulfill_json(
                    route,
                    {
                        'authenticated': True,
                        'auth_required': True,
                        'setup_required': False,
                        'elevated': True,
                        'user': {
                            'id': 'ui-admin',
                            'username': 'ui-admin',
                            'display_name': 'UI Smoke Admin',
                            'role': 'admin',
                            'permissions': ['*'],
                        },
                    },
                )
            elif path.endswith('/api/users/current'):
                fulfill_json(
                    route,
                    {
                        'success': True,
                        'client_id': 'ui-admin',
                        'username': 'ui-admin',
                        'role': 'admin',
                    },
                )
            elif path.endswith('/api/system/health'):
                fulfill_json(route, {'success': True, 'status': 'healthy'})
            elif path.endswith('/api/system/docs'):
                fulfill_json(route, {'success': True, 'apis': [], 'total': 0})
            elif path.endswith('/api/devices/list'):
                fulfill_json(route, [{'device_id': 'E2E1', 'status': 'online', 'locked': False}])
            elif path.endswith('/api/devices/management'):
                fulfill_json(route, {'success': True, 'devices': [], 'device_list': []})
            elif path.endswith('/api/desktop/vnc/status'):
                fulfill_json(route, {'success': True, 'running': True})
            elif path.endswith('/api/desktop/novnc/access'):
                fulfill_json(route, {'success': True, 'url': 'about:blank'})
            elif path.endswith('/api/cluster/hosts'):
                fulfill_json(
                    route,
                    {
                        'success': True,
                        'hosts': [{
                            'worker_id': 'ats-worker-controller',
                            'name': 'Controller / Local Worker',
                            'address': '127.0.0.1',
                            'ssh_user': 'ui-admin',
                            'status': 'online',
                        }],
                    },
                )
            elif path.endswith('/api/test/suites'):
                fulfill_json(route, {'success': True, 'suites': [], 'items': []})
            elif path.endswith('/api/reports/list') or path.endswith('/api/reports'):
                fulfill_json(route, {'success': True, 'reports': [], 'items': [], 'total': 0})
            elif '/statistics' in path:
                fulfill_json(route, {'success': True, 'data': {'summary': {}, 'users': [], 'trends': {}, 'items': []}})
            elif path.endswith('/api/websites/load') or path.endswith('/api/websites/sync'):
                fulfill_json(route, {'success': True, 'tools': {}, 'last_updated': None})
            elif path.endswith('/api/automation/runs'):
                fulfill_json(route, {'success': True, 'data': {'items': [], 'events': []}, 'items': []})
            else:
                fulfill_json(
                    route,
                    {
                        'success': True,
                        'message': 'e2e mocked',
                        'data': {},
                        'items': [],
                        'tasks': [],
                        'task_id': 'e2e-task',
                        'status': 'completed',
                    },
                )

        page.route(re.compile(r'.*/api(?:/.*)?(?:\?.*)?$'), handle_api)

    def capture_errors(self, page):
        captured = {'page_errors': [], 'console_errors': [], 'http_errors': []}
        page.on('pageerror', lambda exc: captured['page_errors'].append(str(exc)))
        page.on(
            'console',
            lambda msg: captured['console_errors'].append(
                {'text': msg.text, 'location': msg.location}
            )
            if msg.type == 'error' and 'favicon' not in msg.text.lower()
            else None,
        )
        page.on(
            'response',
            lambda response: captured['http_errors'].append(
                {'status': response.status, 'url': response.url}
            )
            if response.status >= 400
            else None,
        )
        return captured

    def inventory_document(self, document, page_name):
        return document.evaluate(INVENTORY_SCRIPT, page_name)

    def assert_clean_browser(self, page, captured):
        unhandled = page.evaluate('window.__e2eUnhandledRejections || []')
        self.assertEqual(captured['page_errors'], [])
        self.assertEqual(captured['console_errors'], [])
        self.assertEqual(captured['http_errors'], [])
        self.assertEqual(unhandled, [])

    def wait_for_shell_scripts(self, page):
        page.wait_for_function(
            """
            () => typeof window.apiCall === 'function'
              && typeof window.getClientIdentityHeaders === 'function'
              && typeof window.switchPage === 'function'
            """
        )

    def should_skip_button(self, button):
        if button['disabled']:
            return 'disabled_by_current_behavior'
        subject = ' '.join([button.get('text') or '', button.get('onclick') or '', button.get('selector') or ''])
        for pattern in SKIP_PATTERNS:
            if pattern.search(subject):
                return 'skipped_by_current_behavior'
        return ''

    def click_inventory_buttons(self, document, inventory):
        results = []
        for button in inventory['buttons']:
            skip_reason = self.should_skip_button(button)
            if skip_reason:
                results.append({**button, 'status': skip_reason})
                continue
            clicked = document.evaluate(CLICK_SCRIPT, button)
            document.wait_for_timeout(150)
            document.evaluate(
                """
                () => {
                  document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape'}));
                  if (window.ModalManager && typeof window.ModalManager.closeTopmost === 'function') {
                    window.ModalManager.closeTopmost();
                  }
                }
                """
            )
            visible_modals = document.locator('.modal.show').count()
            if visible_modals:
                # Leak diagnosis: identify exactly which dialog(s) survived
                # the Escape + closeTopmost cleanup, with computed display
                # state so a stale class vs. real visibility is obvious.
                leak_details = document.evaluate(
                    """
                    () => [...document.querySelectorAll('.modal.show')].map(
                        x => ({
                            id: x.id || '(no id)',
                            className: x.className,
                            display: getComputedStyle(x).display,
                            ariaHidden: x.getAttribute('aria-hidden'),
                        })
                    )
                    """
                )
                raise AssertionError(
                    f"{inventory['page']} button left {visible_modals} "
                    f"modal(s) open: {button}; leaking modals: {leak_details}"
                )
            results.append({**button, 'status': 'clicked' if clicked.get('clicked') else clicked.get('reason', 'not_clicked')})
        return results

    def test_machine_readable_control_inventory_covers_shell_and_embedded_pages(self):
        page = self.new_page()
        requests = []
        self.install_api_mocks(page, requests)
        try:
            self.goto_shell(page)
            self.wait_for_shell_scripts(page)
            inventories = [self.inventory_document(page, 'shell')]

            for page_name in ALL_PAGES:
                # Calling the application's navigation entrypoint avoids a flaky
                # actionability wait when the collapsible sidebar is animating.
                page.evaluate("name => window.switchPage(name)", page_name)
                expect(page.locator(f'#page-{page_name}')).to_have_class(re.compile(r'active'))
                inventories.append(self.inventory_document(page, page_name))

            for frame_selector, page_name in [
                ('#devices-console-frame', 'devices-console-frame'),
                ('#cluster-frame', 'cluster-frame'),
                ('#redmine-agent-frame', 'redmine-agent-frame'),
                ('#gerrit-dashboard-frame', 'gerrit-dashboard-frame'),
            ]:
                frame = self.frame_for(page, frame_selector)
                frame.wait_for_function("document.querySelectorAll('button').length > 0")
                inventories.append(self.inventory_document(frame, page_name))

            automation_page = self.new_page()
            self.install_api_mocks(automation_page, [])
            automation_page.goto(f'{self.base_url}/automation', wait_until='domcontentloaded')
            automation_page.wait_for_function("document.querySelectorAll('button').length > 0")
            inventories.append(self.inventory_document(automation_page, 'automation-standalone'))
            automation_page.close()

            self.assertGreaterEqual(len(inventories), len(ALL_PAGES) + 3)
            self.assertGreater(sum(len(item['buttons']) for item in inventories), 80)
            self.assertGreater(sum(len(item['modals']) for item in inventories), 10)
            for inventory in inventories:
                self.assertIn('page', inventory)
                self.assertIsInstance(inventory['buttons'], list)
                self.assertIsInstance(inventory['modals'], list)
        finally:
            page.close()
    def test_visible_safe_controls_click_without_browser_errors(self):
        page = self.new_page()
        elevated = page.request.post(
            f'{self.base_url}/api/auth/elevate',
            data={'username': 'ui-admin', 'password': 'UiSmokeAdmin-2026!'},
        )
        self.assertTrue(elevated.ok, elevated.text())
        requests = []
        captured = self.capture_errors(page)
        self.install_api_mocks(page, requests)
        click_results = []
        try:
            self.goto_shell(page)
            self.wait_for_shell_scripts(page)
            page.evaluate(
                """
                () => {
                  window.showConfirmDialog = async () => true;
                  window.initAndStartVnc = async () => true;
                  if (window.state) {
                    state.devices = [{device_id: 'E2E1'}];
                    state.selectedDevices = new Set(['E2E1']);
                    state.usbipConnected = false;
                    state.adbForwardRunning = false;
                  }
                }
                """
            )

            for page_name in ALL_PAGES:
                with self.subTest(page=page_name):
                    page.locator(f'.sidebar-item[data-page="{page_name}"]').click()
                    expect(page.locator(f'#page-{page_name}')).to_have_class(re.compile(r'active'))
                    inventory = self.inventory_document(page, page_name)
                    click_results.extend(self.click_inventory_buttons(page, inventory))
                    self.assert_clean_browser(page, captured)

            for frame_selector, page_name in [
                ('#devices-console-frame', 'devices-console-frame'),
                ('#cluster-frame', 'cluster-frame'),
                ('#redmine-agent-frame', 'redmine-agent-frame'),
                ('#gerrit-dashboard-frame', 'gerrit-dashboard-frame'),
            ]:
                with self.subTest(page=page_name):
                    frame = self.frame_for(page, frame_selector)
                    frame.wait_for_function("document.querySelectorAll('button').length > 0")
                    inventory = self.inventory_document(frame, page_name)
                    click_results.extend(self.click_inventory_buttons(frame, inventory))
                    self.assert_clean_browser(page, captured)

            clicked = [item for item in click_results if item['status'] == 'clicked']
            skipped = [item for item in click_results if item['status'].endswith('current_behavior')]
            unaccounted = [
                item for item in click_results
                if item['status'] != 'clicked' and not item['status'].endswith('current_behavior')
            ]
            self.assertGreater(len(clicked), 30)
            self.assertGreater(len(skipped), 5)
            self.assertEqual(unaccounted, [])
            self.assertTrue(requests)
            self.assert_clean_browser(page, captured)
        finally:
            page.close()

    def test_every_sidebar_page_survives_full_browser_refresh(self):
        page = self.new_page()
        elevated = page.request.post(
            f'{self.base_url}/api/auth/elevate',
            data={'username': 'ui-admin', 'password': 'UiSmokeAdmin-2026!'},
        )
        self.assertTrue(elevated.ok, elevated.text())
        captured = self.capture_errors(page)
        self.install_api_mocks(page, [])
        try:
            self.goto_shell(page)
            self.wait_for_shell_scripts(page)
            for page_name in ALL_PAGES:
                with self.subTest(page=page_name):
                    page.evaluate("name => window.switchPage(name)", page_name)
                    expect(page.locator(f'#page-{page_name}')).to_have_class(re.compile(r'active'))
                    # switchPage 不返回初始化 Promise（交互式提权不能阻塞
                    # 调用方），完成语义统一经 data-init-state 观察。
                    page.wait_for_function(
                        "name => document.getElementById('page-' + name)?.dataset.initState === 'ready'",
                        arg=page_name,
                    )
                    page.reload(wait_until='load')
                    self.wait_for_shell_scripts(page)
                    expect(page.locator(f'#page-{page_name}')).to_have_class(re.compile(r'active'))
                    page.wait_for_function(
                        "name => document.getElementById('page-' + name)?.dataset.initState === 'ready'",
                        arg=page_name,
                    )
                    self.assertEqual(
                        page.evaluate("location.hash.slice(1).split('?')[0]"),
                        page_name,
                    )
                    self.assert_clean_browser(page, captured)
                    captured['page_errors'].clear()
                    captured['console_errors'].clear()
                    captured['http_errors'].clear()
        finally:
            page.close()

    def test_navigation_cancels_pending_suite_initialization_without_errors(self):
        page = self.new_page()
        self.install_api_mocks(page, [])
        captured = self.capture_errors(page)
        try:
            self.goto_shell(page)
            self.wait_for_shell_scripts(page)
            page.evaluate("""() => {
                _browserSuitesCache = [];
                const original = window.fetch;
                window.__suiteCancelled = false;
                window.fetch = (url, options = {}) => {
                    if (String(url) !== '/api/test/suites') return original(url, options);
                    window.__suitePending = true;
                    return new Promise((resolve, reject) => {
                        options.signal.addEventListener('abort', () => {
                            window.__suiteCancelled = true;
                            reject(new DOMException('Navigation cancelled', 'AbortError'));
                        }, {once: true});
                    });
                };
                window.__pendingNavigation = (switchPage('test-suites'), GmsPageLifecycle.ready);
            }""")
            page.wait_for_function("window.__suitePending === true")
            page.evaluate("() => switchPage('api-docs')")
            page.evaluate("() => window.__pendingNavigation")
            self.assertTrue(page.evaluate("window.__suiteCancelled"))
            self.assertEqual(page.evaluate("GmsPageLifecycle.status"), 'ready')
            self.assert_clean_browser(page, captured)
        finally:
            page.close()

    def test_suite_initialization_records_real_network_failure(self):
        page = self.new_page()
        self.install_api_mocks(page, [])
        captured = self.capture_errors(page)
        try:
            self.goto_shell(page)
            self.wait_for_shell_scripts(page)
            page.evaluate("""async () => {
                _browserSuitesCache = [];
                const original = window.fetch;
                window.__networkToasts = [];
                const toast = window.showToast;
                window.showToast = (...args) => { window.__networkToasts.push(args); toast(...args); };
                window.fetch = (url, options) => String(url) === '/api/test/suites'
                    ? Promise.reject(new TypeError('Failed to fetch (fixture)')) : original(url, options);
                switchPage('test-suites');
                await GmsPageLifecycle.ready;
            }""")
            self.assertEqual(page.evaluate("GmsPageLifecycle.status"), 'error')
            self.assertTrue(any('Failed to fetch (fixture)' in entry['text'] for entry in captured['console_errors']))
            self.assertTrue(any('Failed to fetch (fixture)' in args[0] for args in page.evaluate("window.__networkToasts")))
            self.assertEqual(page.evaluate("window.__e2eUnhandledRejections"), [])
        finally:
            page.close()

    def test_suite_directory_clicks_cancel_without_rejections_or_success_toasts(self):
        actions = {
            'root': "renderSuiteBreadcrumb(''); document.querySelector('#suite-browser-breadcrumb button').click();",
            'sibling': """renderSuiteBreadcrumb('results/fixture');
                Array.from(document.querySelectorAll('#suite-browser-breadcrumb button'))
                    .find(button => button.textContent === '跳到 logs').click();""",
            'suite': "renderTestSuiteBrowserList(); document.querySelector('.suite-suite-item').click();",
        }
        for name, action in actions.items():
            with self.subTest(action=name):
                page = self.new_page()
                self.install_api_mocks(page, [])
                captured = self.capture_errors(page)
                try:
                    self.goto_shell(page)
                    self.wait_for_shell_scripts(page)
                    page.evaluate("async () => { switchPage('test-suites'); await GmsPageLifecycle.ready; }")
                    page.evaluate("""() => {
                        const original = window.fetch;
                        window.__directoryPending = false;
                        window.__directoryCancelled = false;
                        window.__actionToasts = [];
                        const toast = window.showToast;
                        window.showToast = (...args) => { window.__actionToasts.push(args); toast(...args); };
                        window.fetch = (url, options = {}) => {
                            if (!String(url).startsWith('/api/test/suites/files?')) return original(url, options);
                            window.__directoryPending = true;
                            return new Promise((resolve, reject) => {
                                options.signal.addEventListener('abort', () => {
                                    window.__directoryCancelled = true;
                                    reject(new DOMException('Navigation cancelled', 'AbortError'));
                                }, {once: true});
                            });
                        };
                        state.suiteBrowser.selectedSuitePath = '/fixture/tools';
                        _browserSuitesCache = [{tools_path: '/fixture/tools', test_type: 'cts'}];
                    }""")
                    page.evaluate("() => { " + action + " }")
                    page.wait_for_function("window.__directoryPending === true")
                    page.evaluate("async () => { switchPage('api-docs'); await GmsPageLifecycle.ready; }")
                    # Let the browser dispatch any unhandledrejection event.
                    page.evaluate("() => new Promise(resolve => setTimeout(resolve, 0))")
                    self.assertTrue(page.evaluate("window.__directoryCancelled"))
                    self.assertEqual(page.evaluate("window.__actionToasts"), [])
                    self.assert_clean_browser(page, captured)
                finally:
                    page.close()

    def test_cached_page_restore_resumes_interrupted_initialization_and_api_calls(self):
        page = self.new_page()
        self.install_api_mocks(page, [])
        captured = self.capture_errors(page)
        try:
            self.goto_shell(page)
            self.wait_for_shell_scripts(page)
            page.evaluate("""() => {
                const original = window.fetch;
                window.fetch = (url, options = {}) => {
                    if (String(url) !== '/api/test/suites' || !window.__pauseNextSuiteFetch) {
                        return original(url, options);
                    }
                    window.__pauseNextSuiteFetch = false;
                    window.__suitePending = true;
                    return new Promise((resolve, reject) => {
                        options.signal.addEventListener('abort', () => {
                            reject(new DOMException('Page hidden', 'AbortError'));
                        }, {once: true});
                    });
                };
            }""")
            for cycle in range(2):
                with self.subTest(cycle=cycle):
                    page.evaluate("""() => {
                        _browserSuitesCache = [];
                        window.__suitePending = false;
                        window.__pauseNextSuiteFetch = true;
                        window.__hiddenDocumentSignal = GmsDocumentAbort.signal;
                        switchPage('test-suites');
                        window.__hiddenPageSignal = GmsPageLifecycle.signalFor('test-suites');
                        window.__hiddenInitialization = GmsPageLifecycle.ready;
                    }""")
                    page.wait_for_function("window.__suitePending === true")
                    # Deterministically exercise the browser's cache lifecycle
                    # even when WebSockets make this test page ineligible for it.
                    page.evaluate("""() => {
                        window.dispatchEvent(new PageTransitionEvent('pagehide', {persisted: true}));
                        window.dispatchEvent(new PageTransitionEvent('pageshow', {persisted: true}));
                    }""")
                    restored = page.evaluate("""async () => {
                        await window.__hiddenInitialization;
                        await GmsPageLifecycle.ready;
                        const health = await apiCall('/api/system/health');
                        return {
                            oldDocumentAborted: window.__hiddenDocumentSignal.aborted,
                            oldPageAborted: window.__hiddenPageSignal.aborted,
                            newDocumentAborted: GmsDocumentAbort.signal.aborted,
                            newPageAborted: GmsPageLifecycle.signalFor('test-suites').aborted,
                            status: GmsPageLifecycle.status,
                            state: document.getElementById('page-test-suites').dataset.initState,
                            healthy: health.success,
                        };
                    }""")
                    self.assertEqual(restored, {
                        'oldDocumentAborted': True, 'oldPageAborted': True,
                        'newDocumentAborted': False, 'newPageAborted': False,
                        'status': 'ready', 'state': 'ready', 'healthy': True,
                    })
                    self.assert_clean_browser(page, captured)
        finally:
            page.close()
