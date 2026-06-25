import os
import re
import socket
import subprocess
import threading
import time
import unittest
from pathlib import Path
from urllib.request import urlopen


try:
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import expect, sync_playwright
except Exception:  # pragma: no cover - exercised only when Playwright is unavailable
    PlaywrightError = Exception
    expect = None
    sync_playwright = None


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class RuntimeUiSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if sync_playwright is None:
            raise unittest.SkipTest("Playwright is not installed")
        cls.port = free_port()
        cls.base_url = f"http://127.0.0.1:{cls.port}"
        env = os.environ.copy()
        env["GMS_PORT"] = str(cls.port)
        cls.server = subprocess.Popen(
            ["python", "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", str(cls.port)],
            cwd=Path(__file__).resolve().parents[1],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        cls.server_output = []

        def drain_server_output():
            if cls.server.stdout:
                for line in cls.server.stdout:
                    cls.server_output.append(line)

        cls.server_output_thread = threading.Thread(
            target=drain_server_output,
            name="runtime-ui-server-output",
            daemon=True,
        )
        cls.server_output_thread.start()
        deadline = time.time() + 30
        last_error = None
        while time.time() < deadline:
            if cls.server.poll() is not None:
                output = "".join(cls.server_output)
                raise RuntimeError(f"test server exited early:\n{output}")
            try:
                with urlopen(f"{cls.base_url}/api/system/health", timeout=1) as response:
                    if response.status == 200:
                        break
            except Exception as exc:
                last_error = exc
                time.sleep(0.25)
        else:
            raise RuntimeError(f"test server did not start: {last_error}")

        try:
            cls.playwright = sync_playwright().start()
            cls.browser = cls.playwright.chromium.launch(headless=True)
        except PlaywrightError as exc:
            cls.tearDownClass()
            raise unittest.SkipTest(f"Playwright browser is unavailable: {exc}") from exc

    @classmethod
    def tearDownClass(cls):
        browser = getattr(cls, "browser", None)
        if browser:
            browser.close()
        playwright = getattr(cls, "playwright", None)
        if playwright:
            playwright.stop()
        server = getattr(cls, "server", None)
        if server and server.poll() is None:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=5)
        output_thread = getattr(cls, "server_output_thread", None)
        if output_thread:
            output_thread.join(timeout=2)
        if server and server.stdout:
            server.stdout.close()
        Path("data/automation_runs.sqlite3").unlink(missing_ok=True)

    def new_page(self):
        page = self.browser.new_page(viewport={"width": 1440, "height": 960})
        page.set_default_timeout(8000)
        page.set_default_navigation_timeout(15000)
        page.route(
            "**/api/websites/load",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body='{"success":true,"tools":{},"last_updated":null}',
            ),
        )
        page.route(
            "**/api/websites/save",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body='{"success":true}',
            ),
        )
        page.route(
            "**/api/websites/sync",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body='{"success":true,"tools":{},"last_updated":null}',
            ),
        )
        page.add_init_script(
            """
            localStorage.setItem('gms_sidebar_visible_pages', JSON.stringify([
                'test', 'desktop', 'terminal', 'users', 'devices', 'reports',
                'report-analysis', 'test-suites', 'apk-analysis', 'security-audit',
                'api-docs', 'architecture', 'websites', 'tools', 'gms-assistant',
                'automation', 'redmine-agent', 'gerrit-dashboard', 'agent'
            ]));
            """
        )
        return page

    def close_initial_modals(self, page):
        for _ in range(10):
            if page.locator(".modal.show").count():
                page.keyboard.press("Escape")
                expect(page.locator(".modal.show")).to_have_count(0)
                return
            page.wait_for_timeout(200)

    def show_all_sidebar_pages(self, page):
        page.evaluate(
            """
            if (typeof applySidebarVisibility === 'function') {
                applySidebarVisibility([
                    'test', 'desktop', 'terminal', 'users', 'devices', 'reports',
                    'report-analysis', 'test-suites', 'apk-analysis', 'security-audit',
                    'api-docs', 'architecture', 'websites', 'tools', 'gms-assistant',
                    'automation', 'redmine-agent', 'gerrit-dashboard', 'agent'
                ]);
            }
            """
        )

    def visible_sidebar_pages(self, page):
        return page.locator(".sidebar-item[data-page]").evaluate_all(
            "(items) => items.map(item => item.dataset.page)"
        )

    def goto_shell(self, page):
        page.goto(self.base_url, wait_until="domcontentloaded")
        page.wait_for_selector(".sidebar-item[data-page]")
        self.close_initial_modals(page)
        self.show_all_sidebar_pages(page)

    def frame_for(self, page, selector):
        handle = page.locator(selector).element_handle()
        self.assertIsNotNone(handle, f"missing iframe: {selector}")
        frame = handle.content_frame()
        self.assertIsNotNone(frame, f"iframe not loaded: {selector}")
        return frame

    def assert_no_page_errors(self, page_errors):
        self.assertEqual(page_errors, [])

    def press_escape_in_frame(self, frame):
        frame.evaluate("document.dispatchEvent(new KeyboardEvent('keydown', {key:'Escape'}))")

    def assert_frame_modal_closes_with_escape(self, frame, open_script, modal_selector):
        function_name = open_script.split("(", 1)[0]
        frame.wait_for_function(f"typeof {function_name} === 'function'")
        frame.evaluate(open_script)
        expect(frame.locator(modal_selector)).to_have_class(re.compile(r"show"))
        self.press_escape_in_frame(frame)
        expect(frame.locator(modal_selector)).not_to_have_class(re.compile(r"show"))

    def test_sidebar_pages_switch_without_runtime_errors(self):
        page = self.new_page()
        page_errors = []
        page.on("pageerror", lambda exc: page_errors.append(str(exc)))
        try:
            self.goto_shell(page)
            pages = page.locator(".sidebar-item[data-page]").evaluate_all(
                "(items) => items.map(item => item.dataset.page)"
            )
            for page_name in pages:
                page.locator(f'.sidebar-item[data-page="{page_name}"]').click()
                expect(page.locator(f"#page-{page_name}")).to_have_class(re.compile(r"active"))
            self.assertEqual(page_errors, [])
        finally:
            page.close()

    def test_first_visit_defaults_to_test_page(self):
        page = self.browser.new_page(viewport={"width": 1440, "height": 960})
        page.set_default_timeout(8000)
        page.set_default_navigation_timeout(15000)
        page_errors = []
        page.on("pageerror", lambda exc: page_errors.append(str(exc)))
        try:
            page.goto(self.base_url, wait_until="load")
            page.wait_for_selector(".sidebar-item[data-page]")
            self.close_initial_modals(page)
            expect(page.locator("#page-test")).to_have_class(re.compile(r"active"))
            expect(page.locator('.sidebar-item[data-page="test"]')).to_have_class(re.compile(r"active"))
            self.assertEqual(page.evaluate("localStorage.getItem('gms_current_page')"), "test")
            self.assert_no_page_errors(page_errors)
        finally:
            page.close()

    def test_saved_users_page_restores_auto_refresh_on_load(self):
        page = self.new_page()
        page_errors = []
        page.on("pageerror", lambda exc: page_errors.append(str(exc)))
        try:
            page.route(
                "**/api/users/list",
                lambda route: route.fulfill(
                    status=200,
                    content_type="application/json",
                    body='{"users":[]}',
                ),
            )
            page.add_init_script(
                """
                localStorage.setItem('gms_current_page', 'users');
                window.__usersAutoRefreshIntervals = [];
                const originalSetInterval = window.setInterval.bind(window);
                window.setInterval = (handler, delay, ...args) => {
                  const id = originalSetInterval(handler, delay, ...args);
                  const source = Function.prototype.toString.call(handler);
                  if (delay === 10000 && source.includes('loadUsersList')) {
                    window.__usersAutoRefreshIntervals.push(delay);
                  }
                  return id;
                };
                """
            )
            page.goto(self.base_url, wait_until="load")
            page.wait_for_selector(".sidebar-item[data-page]")
            self.close_initial_modals(page)
            page.wait_for_function("typeof window.switchPage === 'function'")
            expect(page.locator("#page-users")).to_have_class(re.compile(r"active"))
            page.wait_for_function(
                "window.__usersAutoRefreshIntervals && window.__usersAutoRefreshIntervals.length > 0"
            )
            self.assert_no_page_errors(page_errors)
        finally:
            page.close()

    def test_saved_architecture_page_sets_title_before_load_and_lazy_loads_frame(self):
        page = self.new_page()
        page_errors = []
        page.on("pageerror", lambda exc: page_errors.append(str(exc)))
        try:
            page.context.add_cookies([
                {
                    "name": "gms_current_page",
                    "value": "architecture",
                    "url": self.base_url,
                }
            ])
            page.add_init_script(
                """
                localStorage.setItem('gms_current_page', 'architecture');
                """
            )
            page.goto(self.base_url, wait_until="domcontentloaded")
            self.close_initial_modals(page)
            self.assertEqual(page.title(), "系统架构 - GMS远程测试")
            page.wait_for_function("typeof window.switchPage === 'function'")
            expect(page.locator("#page-architecture")).to_have_class(re.compile(r"active"))
            expect(page.locator("#architecture-iframe")).to_have_attribute("src", re.compile(r"/templates/architecture\.html"))
            self.assert_no_page_errors(page_errors)
        finally:
            page.close()

    def test_saved_page_cookie_sets_initial_html_title(self):
        page = self.new_page()
        try:
            page.context.add_cookies([
                {
                    "name": "gms_current_page",
                    "value": "devices",
                    "url": self.base_url,
                }
            ])
            page.goto(self.base_url, wait_until="commit")

            self.assertEqual(page.title(), "设备管理 - GMS远程测试")
        finally:
            page.close()

    def test_architecture_template_uses_local_fonts_only(self):
        html = (Path(__file__).resolve().parents[1] / "web" / "templates" / "architecture.html").read_text(encoding="utf-8")

        self.assertNotIn("fonts.font.im", html)
        self.assertNotIn("fonts.googleapis.com", html)

    def test_arrow_key_navigation_persists_page_for_reload(self):
        page = self.new_page()
        page_errors = []
        page.on("pageerror", lambda exc: page_errors.append(str(exc)))
        try:
            self.goto_shell(page)
            page.wait_for_function("typeof window.switchPage === 'function'")
            expect(page.locator("#page-test")).to_have_class(re.compile(r"active"))

            page.evaluate("document.activeElement && document.activeElement.blur()")
            page.keyboard.press("ArrowDown")
            expect(page.locator("#page-desktop")).to_have_class(re.compile(r"active"))
            self.assertEqual(page.title(), "主机桌面 - GMS远程测试")
            self.assertEqual(page.evaluate("localStorage.getItem('gms_current_page')"), "desktop")
            self.assertIn("gms_current_page=desktop", page.evaluate("document.cookie"))

            page.reload(wait_until="domcontentloaded")
            self.assertEqual(page.title(), "主机桌面 - GMS远程测试")
            page.wait_for_load_state("load")
            page.wait_for_function("typeof window.switchPage === 'function'")
            expect(page.locator("#page-desktop")).to_have_class(re.compile(r"active"))
            self.assertEqual(page.evaluate("localStorage.getItem('gms_current_page')"), "desktop")
            self.assert_no_page_errors(page_errors)
        finally:
            page.close()

    def test_agent_page_quick_action_opens_every_sidebar_page(self):
        page = self.new_page()
        page_errors = []
        page.on("pageerror", lambda exc: page_errors.append(str(exc)))
        try:
            self.goto_shell(page)
            page.wait_for_function("typeof openAgentPageAction === 'function'")

            for page_name in self.visible_sidebar_pages(page):
                with self.subTest(page=page_name):
                    page.evaluate("target => openAgentPageAction(target, '{}')", page_name)
                    expect(page.locator(f"#page-{page_name}")).to_have_class(re.compile(r"active"))
                    expect(page.locator(f'.sidebar-item[data-page="{page_name}"]')).to_have_class(re.compile(r"active"))

            page.evaluate("openAgentPageAction('redmine-agent', JSON.stringify({tab:'department', name:'黄超群'}))")
            redmine_src = page.locator("#redmine-agent-frame").get_attribute("src")
            self.assertIsNotNone(redmine_src)
            self.assertIn("tab=department", redmine_src)
            self.assertIn("name=", redmine_src)

            page.evaluate("openAgentPageAction('gerrit-dashboard', '{}')")
            gerrit_src = page.locator("#gerrit-dashboard-frame").get_attribute("src")
            self.assertEqual(gerrit_src, "/gerrit-dashboard")

            self.assert_no_page_errors(page_errors)
        finally:
            page.close()

    def test_device_actions_send_expected_requests(self):
        page = self.new_page()
        requests = []

        def handle_device_request(route):
            request = route.request
            requests.append(
                {
                    "method": request.method,
                    "path": request.url.split(self.base_url, 1)[-1],
                    "body": request.post_data_json if request.post_data else None,
                }
            )
            route.fulfill(
                status=200,
                content_type="application/json",
                body='{"success":true,"message":"ok","device_list":["D1"]}',
            )

        try:
            self.goto_shell(page)
            page.route("**/api/devices/**", handle_device_request)
            page.route("**/api/usbip/**", handle_device_request)
            page.route("**/api/adb-forward/**", handle_device_request)
            page.evaluate(
                """
                state.devices = [{device_id: 'D1'}];
                state.selectedDevices = new Set(['D1']);
                state.adbForwardRunning = false;
                state.usbipConnected = false;
                showConfirmDialog = async () => true;
                initAndStartVnc = async () => true;
                document.getElementById('wifi-ssid').value = 'LabWifi';
                document.getElementById('wifi-password').value = 'secret';
                """
            )

            page.evaluate("rebootDevices()")
            page.evaluate("remountDevices()")
            page.evaluate("submitWifiConfig()")
            page.evaluate("lockSelectedDevices('lock')")
            page.evaluate("showDeviceScreen()")
            page.evaluate("setupUsbipForward()")
            page.evaluate("setupAdbPortForward()")

            self.assertEqual(
                [(item["method"], item["path"]) for item in requests],
                [
                    ("POST", "/api/devices/reboot"),
                    ("POST", "/api/devices/remount"),
                    ("POST", "/api/devices/wifi"),
                    ("POST", "/api/devices/bootloader-lock"),
                    ("POST", "/api/devices/scrcpy"),
                    ("POST", "/api/usbip/connect"),
                    ("POST", "/api/adb-forward/start"),
                ],
            )
            self.assertEqual(requests[0]["body"], {"devices": ["D1"]})
            self.assertEqual(requests[1]["body"], {"devices": ["D1"]})
            self.assertEqual(
                requests[2]["body"],
                {"devices": ["D1"], "ssid": "LabWifi", "password": "secret"},
            )
            self.assertEqual(requests[3]["body"], {"devices": ["D1"]})
            self.assertEqual(requests[4]["body"], {"devices": ["D1"]})
            self.assertEqual(requests[5]["body"], {"manual_connect": True})
            self.assertIsNone(requests[6]["body"])
        finally:
            page.close()

    def test_firmware_and_apk_actions_send_expected_requests(self):
        page = self.new_page()
        requests = []

        def handle_request(route):
            request = route.request
            requests.append((request.method, request.url.split(self.base_url, 1)[-1]))
            route.fulfill(
                status=200,
                content_type="application/json",
                body='{"success":true,"task_id":"apk-task","status":"completed"}',
            )

        try:
            self.goto_shell(page)
            page.route("**/api/burn/**", handle_request)
            page.route("**/api/apk/**", handle_request)
            page.evaluate(
                """
                state.selectedDevices = new Set(['D1']);
                window.apkCurrentTaskId = 'apk-task';
                executeBurnOperation = async (endpoint, data) => {
                    await apiCall(endpoint, 'POST', {
                        devices: Array.from(state.selectedDevices),
                        ...data
                    });
                };
                document.getElementById('gsi-script').value = '/tmp/burn.sh';
                document.getElementById('gsi-system').value = '/tmp/system.img';
                document.getElementById('gsi-vendor').value = '';
                document.getElementById('sn-code').value = 'SN001';
                """
            )

            page.evaluate("submitGsiBurn()")
            page.evaluate("submitSnBurn()")
            page.evaluate("startApkAnalysis()")
            page.evaluate(
                """Promise.all([
                    fetch('/api/burn/firmware', {method:'POST'}),
                    fetch('/api/apk/download/apk-task'),
                    fetch('/api/apk/task/apk-task', {method:'DELETE'})
                ])"""
            )

            for expected in (
                ("POST", "/api/burn/gsi"),
                ("POST", "/api/burn/serial"),
                ("POST", "/api/apk/analyze/apk-task"),
                ("POST", "/api/burn/firmware"),
                ("GET", "/api/apk/download/apk-task"),
                ("DELETE", "/api/apk/task/apk-task"),
            ):
                self.assertIn(expected, requests)
        finally:
            page.close()

    def test_sidebar_visibility_modal_closes_with_escape(self):
        page = self.new_page()
        try:
            self.goto_shell(page)
            page.locator(".sidebar-brand").click()
            modal = page.locator("#sidebar-visibility-modal")
            expect(modal).to_have_class(re.compile(r"show"))
            page.keyboard.press("Escape")
            expect(modal).not_to_have_class(re.compile(r"show"))
        finally:
            page.close()

    def test_main_shell_static_and_dynamic_modals_close_with_escape(self):
        page = self.new_page()
        page_errors = []
        page.on("pageerror", lambda exc: page_errors.append(str(exc)))
        try:
            self.goto_shell(page)
            page.wait_for_function("typeof ModalManager === 'object'")
            modal_ids = page.locator(".modal[id]").evaluate_all(
                "(items) => items.map(item => item.id).filter(Boolean)"
            )
            self.assertGreater(len(modal_ids), 10)

            for modal_id in modal_ids:
                with self.subTest(modal=modal_id):
                    page.evaluate("id => ModalManager.open(id)", modal_id)
                    expect(page.locator(f"#{modal_id}")).to_have_class(re.compile(r"show"))
                    page.keyboard.press("Escape")
                    expect(page.locator(f"#{modal_id}")).not_to_have_class(re.compile(r"show"))

            page.evaluate("selectReportSource()")
            expect(page.locator("#report-source-modal")).to_be_visible()
            page.keyboard.press("Escape")
            expect(page.locator("#report-source-modal")).to_have_count(0)

            page.evaluate("showRedmineAuthDialog('https://redmine.local/issues/1', null, null, null, null, {})")
            expect(page.locator("#redmine-auth-modal")).to_be_visible()
            page.keyboard.press("Escape")
            expect(page.locator("#redmine-auth-modal")).to_have_count(0)

            modal_id = page.evaluate("createAnalysisModal('runtime-smoke', '运行时弹框测试', '加载中').modalId")
            expect(page.locator(f"#{modal_id}")).to_be_visible()
            page.keyboard.press("Escape")
            expect(page.locator(f"#{modal_id}")).not_to_have_class(re.compile(r"show"))

            self.assert_no_page_errors(page_errors)
        finally:
            page.close()

    def test_embedded_dashboard_notification_bridge_reaches_main_shell(self):
        page = self.new_page()
        try:
            self.goto_shell(page)
            page.evaluate(
                """
                window.postMessage({
                    type: 'gms-dashboard-notification',
                    title: '运行时通知测试',
                    message: 'iframe bridge ok',
                    level: 'success'
                }, '*');
                """
            )
            expect(page.locator(".notification-badge")).to_be_visible()
        finally:
            page.close()

    def test_redmine_and_gerrit_iframe_modals_close_with_escape(self):
        page = self.new_page()
        try:
            self.goto_shell(page)

            page.locator('.sidebar-item[data-page="redmine-agent"]').click()
            redmine = self.frame_for(page, "#redmine-agent-frame")
            redmine.wait_for_function("typeof showSettingsModal === 'function'")
            redmine.evaluate("showSettingsModal()")
            expect(redmine.locator("#settingsModal")).to_have_class(re.compile(r"show"))
            redmine.evaluate("document.dispatchEvent(new KeyboardEvent('keydown', {key:'Escape'}))")
            expect(redmine.locator("#settingsModal")).not_to_have_class(re.compile(r"show"))

            page.locator('.sidebar-item[data-page="gerrit-dashboard"]').click()
            gerrit = self.frame_for(page, "#gerrit-dashboard-frame")
            gerrit.wait_for_function("typeof showSettings === 'function'")
            gerrit.evaluate("showSettings()")
            expect(gerrit.locator("#settingsModal")).to_have_class(re.compile(r"show"))
            gerrit.evaluate("document.dispatchEvent(new KeyboardEvent('keydown', {key:'Escape'}))")
            expect(gerrit.locator("#settingsModal")).not_to_have_class(re.compile(r"show"))
        finally:
            page.close()

    def test_iframe_notify_user_reaches_main_shell_from_real_frames(self):
        page = self.new_page()
        try:
            self.goto_shell(page)
            page.locator('.sidebar-item[data-page="redmine-agent"]').click()
            redmine = self.frame_for(page, "#redmine-agent-frame")
            redmine.wait_for_function("typeof notifyUser === 'function'")
            redmine.evaluate("notifyUser('Redmine iframe 通知测试', 'ok', 'success')")
            expect(page.locator(".notification-badge")).to_be_visible()

            page.locator('.sidebar-item[data-page="gerrit-dashboard"]').click()
            gerrit = self.frame_for(page, "#gerrit-dashboard-frame")
            gerrit.wait_for_function("typeof notifyUser === 'function'")
            gerrit.evaluate("notifyUser('Gerrit iframe 通知测试', 'ok', 'success')")
            expect(page.locator(".notification-badge")).to_be_visible()
        finally:
            page.close()

    def test_gms_update_monitor_notification_bridge_reaches_main_shell(self):
        page = self.new_page()
        try:
            self.goto_shell(page)
            page.evaluate(
                """
                const frame = document.createElement('iframe');
                frame.id = 'gms-update-monitor-smoke-frame';
                frame.src = '/gms-update-monitor';
                frame.style.display = 'none';
                document.body.appendChild(frame);
                """
            )
            frame = self.frame_for(page, "#gms-update-monitor-smoke-frame")
            frame.wait_for_function("typeof notifyUser === 'function'")
            frame.evaluate("notifyUser('GMS更新监控通知测试', 'ok', 'success')")
            expect(page.locator(".notification-badge")).to_be_visible()
        finally:
            page.close()

    def test_automation_workbench_buttons_create_and_advance_stub_run(self):
        page = self.new_page()
        try:
            page.goto(f"{self.base_url}/automation", wait_until="domcontentloaded")
            page.wait_for_selector("#automation-create-run")
            page.fill("#automation-artifact", "/tmp/update.img")
            page.fill("#automation-devices", "TESTSERIAL001")
            page.evaluate("document.getElementById('automation-create-run').click()")
            expect(page.locator("#automation-toast")).to_contain_text("已创建")
            page.evaluate("Array.from(document.querySelectorAll('button')).find(btn => btn.textContent.trim() === 'Worker Tick').click()")
            expect(page.locator("#automation-toast")).to_contain_text("推进到")
            page.evaluate("document.querySelector('button[data-status=\"queued\"]').click()")
            expect(page.locator('button[data-status="queued"]')).to_have_class(re.compile(r"active"))
        finally:
            page.close()

    def test_redmine_dashboard_safe_controls_and_modals(self):
        page = self.new_page()
        page_errors = []
        page.on("pageerror", lambda exc: page_errors.append(str(exc)))
        try:
            self.goto_shell(page)
            page.locator('.sidebar-item[data-page="redmine-agent"]').click()
            redmine = self.frame_for(page, "#redmine-agent-frame")
            redmine.wait_for_function("typeof switchTab === 'function'")

            for tab_name in ["department", "stats", "project", "issues", "runs"]:
                redmine.evaluate("tab => switchTab(tab)", tab_name)
                expect(redmine.locator(f'.tab[data-tab="{tab_name}"]')).to_have_class(re.compile(r"active"))

            self.assert_frame_modal_closes_with_escape(redmine, "showSettingsModal()", "#settingsModal")
            self.assert_frame_modal_closes_with_escape(redmine, "showAddUserModal()", "#addUserModal")
            self.assert_frame_modal_closes_with_escape(redmine, "showAddDepartmentModal()", "#addDepartmentModal")
            self.assert_frame_modal_closes_with_escape(redmine, "showAddProjectModal()", "#addProjectModal")

            redmine.wait_for_function("typeof setTrendStartDate === 'function'")
            redmine.evaluate("setTrendStartDate('daily', '每日')")
            expect(redmine.locator("#trendStartModal")).to_have_class(re.compile(r"show"))
            self.press_escape_in_frame(redmine)
            expect(redmine.locator("#trendStartModal")).not_to_have_class(re.compile(r"show"))

            redmine.evaluate("refreshCurrentTab()")
            self.assert_no_page_errors(page_errors)
        finally:
            page.close()

    def test_gerrit_dashboard_safe_controls_and_modals(self):
        page = self.new_page()
        page_errors = []
        page.on("pageerror", lambda exc: page_errors.append(str(exc)))
        try:
            self.goto_shell(page)
            page.locator('.sidebar-item[data-page="gerrit-dashboard"]').click()
            gerrit = self.frame_for(page, "#gerrit-dashboard-frame")
            gerrit.wait_for_function("typeof switchTab === 'function'")

            for tab_name in ["personal", "department", "query"]:
                gerrit.evaluate("tab => switchTab(tab)", tab_name)
                expect(gerrit.locator(f'.tab[data-tab="{tab_name}"]')).to_have_class(re.compile(r"active"))

            self.assert_frame_modal_closes_with_escape(gerrit, "showSettings()", "#settingsModal")
            self.assert_frame_modal_closes_with_escape(gerrit, "showAddPersonalModal()", "#addPersonalModal")
            self.assert_frame_modal_closes_with_escape(gerrit, "showAddDepartmentModal()", "#addDepartmentModal")
            self.assert_frame_modal_closes_with_escape(gerrit, "showAddDepartmentOwnerModal()", "#addDepartmentOwnerModal")

            gerrit.wait_for_function("typeof setTrendStartDate === 'function'")
            gerrit.evaluate("setTrendStartDate('daily', '每日')")
            expect(gerrit.locator("#trendStartModal")).to_have_class(re.compile(r"show"))
            self.press_escape_in_frame(gerrit)
            expect(gerrit.locator("#trendStartModal")).not_to_have_class(re.compile(r"show"))

            gerrit.evaluate("refreshCurrentTab()")
            self.assert_no_page_errors(page_errors)
        finally:
            page.close()

    def test_auxiliary_dashboards_safe_buttons_do_not_throw(self):
        page = self.new_page()
        page_errors = []
        page.on("pageerror", lambda exc: page_errors.append(str(exc)))
        try:
            page.goto(f"{self.base_url}/gms-update-monitor", wait_until="domcontentloaded")
            page.wait_for_selector("button")
            for tab_name in ["changes", "artifacts", "packages", "requirements"]:
                page.evaluate("tab => setTab(tab)", tab_name)
                expect(page.locator(f'button[data-tab="{tab_name}"]')).to_have_class(re.compile(r"active"))
                page.evaluate("reload(true)")
                page.evaluate("page(1)")
                page.evaluate("page(-1)")

            page.goto(f"{self.base_url}/mainline-known-issues", wait_until="domcontentloaded")
            page.wait_for_selector("button")
            page.evaluate("reload(true)")
            page.evaluate("page(1)")
            page.evaluate("page(-1)")

            page.goto(f"{self.base_url}/automation", wait_until="domcontentloaded")
            page.wait_for_selector("button[data-status]")
            for status in ["", "queued", "testing", "completed"]:
                page.evaluate("status => setStatusFilter(status)", status)
                selector = 'button[data-status="' + status + '"]'
                expect(page.locator(selector)).to_have_class(re.compile(r"active"))
            page.evaluate("loadAll()")

            self.assert_no_page_errors(page_errors)
        finally:
            page.close()
