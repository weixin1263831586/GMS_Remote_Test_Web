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
                raise AssertionError(
                    f"{inventory['page']} button left {visible_modals} modal(s) open: {button}"
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
                    page.reload(wait_until='load')
                    self.wait_for_shell_scripts(page)
                    expect(page.locator(f'#page-{page_name}')).to_have_class(re.compile(r'active'))
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
