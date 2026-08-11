import json
import os
import re
import socket
import subprocess
import tempfile
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


class RuntimeUiHarness(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if sync_playwright is None:
            raise unittest.SkipTest("Playwright is not installed")
        cls.port = free_port()
        cls.base_url = f"http://127.0.0.1:{cls.port}"
        runtime_parent = "/dev/shm" if Path("/dev/shm").is_dir() else None
        cls.runtime_dir = tempfile.TemporaryDirectory(dir=runtime_parent)
        env = os.environ.copy()
        env["GMS_PORT"] = str(cls.port)
        env["GMS_DATA_ROOT"] = cls.runtime_dir.name
        env["GMS_ENV"] = "development"
        env["GMS_SKIP_RUNTIME_ENV"] = "1"
        env["ATS_WORKER_ENABLED"] = "0"
        env["GMS_AUTH_REQUIRED"] = "true"
        env["GMS_SECURE_COOKIES"] = "false"
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
        runtime_dir = getattr(cls, "runtime_dir", None)
        if runtime_dir:
            runtime_dir.cleanup()

    def new_page(self):
        # Runtime smoke tests intentionally execute helper expressions in the
        # page context.  Keep the production CSP strict while allowing that
        # Playwright-only instrumentation in this isolated browser context.
        page = self.browser.new_page(
            viewport={"width": 1440, "height": 960},
            bypass_csp=True,
        )
        page.set_default_timeout(8000)
        page.set_default_navigation_timeout(15000)
        status = page.request.get(f"{self.base_url}/api/auth/status")
        self.assertTrue(status.ok, status.text())
        endpoint = "setup" if status.json().get("setup_required") else "login"
        authenticated = page.request.post(
            f"{self.base_url}/api/auth/{endpoint}",
            data={
                "username": "ui-admin",
                "password": "UiSmokeAdmin-2026!",
                "display_name": "UI Smoke Admin",
            },
        )
        self.assertTrue(authenticated.ok, authenticated.text())
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
            try {
                localStorage.setItem('gms_sidebar_visible_pages', JSON.stringify([
                    'test', 'desktop', 'terminal', 'users', 'devices', 'reports',
                    'report-analysis', 'test-suites', 'apk-analysis', 'security-audit',
                    'api-docs', 'architecture', 'websites', 'tools', 'gms-assistant',
                    'automation', 'cluster', 'redmine-agent', 'gerrit-dashboard', 'agent', 'notes'
                ]));
            } catch (_error) {
                // Init scripts also run in transient/opaque child frames where
                // storage access is intentionally unavailable.
            }
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
                    'automation', 'cluster', 'redmine-agent', 'gerrit-dashboard', 'agent', 'notes'
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


class RuntimeUiSmokeTests(RuntimeUiHarness):
    def test_user_actions_and_report_identity_use_friendly_horizontal_display(self):
        page = self.new_page()
        try:
            self.goto_shell(page)
            result = page.evaluate(
                """
                async () => {
                    displayUsersList([{
                        client_id: 'hcq@172.16.14.66',
                        user_id: 'N387pLbIBhpMw5JsWUL9hg',
                        username: 'hcq',
                        ip: '172.16.14.66',
                        source_label: '内网',
                        source: 'internal',
                        running: true,
                        configured: true,
                        devices: ['RK3576GMS6'],
                        cluster_jobs: [{
                            id: 'job-1',
                            worker_id: 'ats-worker-controller',
                            attempt_id: 'attempt-1',
                            status: 'running'
                        }]
                    }, {
                        client_id: 'wlq@172.16.14.67',
                        username: 'wlq',
                        ip: '172.16.14.67',
                        source_label: '内网',
                        source: 'internal',
                        running: false,
                        configured: true,
                        devices: [],
                        cluster_jobs: []
                    }]);
                    const actions = Array.from(document.querySelectorAll(
                        '#users-table-body tr td:last-child > div'
                    ));
                    const buttons = Array.from(
                        document.querySelectorAll('#users-table-body button')
                    );
                    displayTestReports([{
                        timestamp: 'cluster-job-941843984fd44e1b9111532981e188c9',
                        report_name: '2026.07.30_10.39.50.173_6846',
                        source_timestamp: 'Fri Jul 30 10:39:53 CST 2026',
                        display_client_id: 'hcq@172.16.14.233',
                        test_type: 'CTS',
                        suite_version: '17_r1',
                        worker_id: 'ats-worker-controller',
                        pass: 1,
                        fail: 0,
                        total: 1
                    }]);
                    const reportCells = document.querySelectorAll(
                        '#reports-table-body tr:first-child td'
                    );
                    let deletePrompt = '';
                    const originalConfirm = showConfirmDialog;
                    showConfirmDialog = async (_title, message) => {
                        deletePrompt = message;
                        return false;
                    };
                    await deleteReport(
                        'cluster-job-941843984fd44e1b9111532981e188c9',
                        'cluster:job-1:attempt-1',
                        '2026.07.30_10.39.50.173_6846'
                    );
                    showConfirmDialog = originalConfirm;
                    return {
                        actionDisplays: actions.map(
                            action => getComputedStyle(action).display
                        ),
                        actionColumns: actions.map(
                            action => getComputedStyle(action).gridTemplateColumns
                        ),
                        buttonLabels: buttons.map(button => button.textContent.trim()),
                        removeLefts: Array.from(
                            document.querySelectorAll(
                                '#users-table-body button[data-remove-user]'
                            )
                        ).map(button => Math.round(
                            button.getBoundingClientRect().left
                        )),
                        reportClient: reportCells[0].textContent.trim(),
                        reportSuite: reportCells[2].textContent.trim(),
                        reportWorker: reportCells[3].textContent.trim(),
                        reportName: reportCells[4].textContent.trim(),
                        reportTimestampTitle: reportCells[4].title,
                        reportFolder: tradefedResultFolderName(
                            '/suite/results/2026.07.30_10.39.50.173_6846'
                        ),
                        legacyStartDisplayFolder: tradefedResultFolderName(
                            'Fri Jul 30 10:39:53 CST 2026'
                        ),
                        reportNameDataset: document.querySelector(
                            '#reports-table-body tr:first-child'
                        ).dataset.reportName,
                        deletePrompt
                    };
                }
                """
            )

            self.assertEqual(result["actionDisplays"], ["grid", "grid"])
            self.assertEqual(result["actionColumns"], ["44px 44px", "44px 44px"])
            self.assertEqual(result["buttonLabels"], ["任务", "移除", "移除"])
            self.assertEqual(len(set(result["removeLefts"])), 1)
            self.assertEqual(result["reportClient"], "hcq@172.16.14.233")
            self.assertEqual(result["reportSuite"], "android-cts-17_r1")
            self.assertEqual(result["reportWorker"], "ats-worker-controller")
            self.assertEqual(
                result["reportName"],
                "2026.07.30_10.39.50.173_6846",
            )
            self.assertEqual(
                result["reportTimestampTitle"],
                "cluster-job-941843984fd44e1b9111532981e188c9",
            )
            self.assertEqual(
                result["reportFolder"],
                "2026.07.30_10.39.50.173_6846",
            )
            self.assertEqual(result["legacyStartDisplayFolder"], "")
            self.assertEqual(
                result["reportNameDataset"],
                "2026.07.30_10.39.50.173_6846",
            )
            self.assertEqual(
                result["deletePrompt"],
                "确定要删除报告 2026.07.30_10.39.50.173_6846 吗？此操作不可恢复。",
            )
        finally:
            page.close()

    def test_auth_status_failure_does_not_stack_terminal_elevation_dialog(self):
        page = self.new_page()
        page.route(
            "**/api/auth/status",
            lambda route: route.fulfill(
                status=500,
                content_type="application/json",
                body='{"success":false,"error":"auth unavailable"}',
            ),
        )
        page.add_init_script(
            "localStorage.setItem('gms_current_page', 'terminal')"
        )
        try:
            page.goto(self.base_url, wait_until="load")
            expect(page.locator("#auth-gate")).to_be_visible()
            expect(page.locator("#elevate-modal")).not_to_have_class(
                re.compile(r"\bshow\b")
            )
            self.assertFalse(page.evaluate("state.authReady"))
        finally:
            page.close()

    def test_anonymous_mode_opens_elevation_dialog_before_desktop_request(self):
        page = self.new_page()
        protected_requests = []
        page.on(
            "request",
            lambda request: protected_requests.append(request.url)
            if "/api/desktop/" in request.url
            else None,
        )
        try:
            self.goto_shell(page)
            page.evaluate(
                """
                () => {
                  state.currentUser = null;
                  state.authRequired = false;
                  state.authSetupRequired = false;
                  state.elevated = false;
                  state.elevatedUntil = null;
                  switchPage('desktop', null);
                }
                """
            )
            expect(page.locator("#elevate-modal")).to_have_class(re.compile(r"show"))
            expect(page.locator("#elevate-username")).to_be_editable()
            expect(page.locator("#elevate-password")).to_be_editable()
            self.assertEqual(protected_requests, [])
        finally:
            page.close()

    def test_desktop_prompts_before_protected_vnc_requests(self):
        page = self.new_page()
        protected_responses = []
        page.on(
            "response",
            lambda response: protected_responses.append(response.status)
            if "/api/desktop/" in response.url
            else None,
        )
        page.route(
            "**/api/desktop/vnc/status",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body='{"success":true,"running":true}',
            ),
        )
        page.route(
            "**/api/desktop/novnc/access",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body='{"success":true,"url":"about:blank"}',
            ),
        )
        try:
            self.goto_shell(page)
            page.evaluate("state.elevated = false; state.elevatedUntil = null")
            page.evaluate("switchPage('desktop', null)")
            expect(page.locator("#elevate-modal")).to_have_class(re.compile(r"show"))
            page.locator("#elevate-password").fill("UiSmokeAdmin-2026!")
            page.locator("#elevate-modal .btn-primary").click()
            expect(page.locator("#host-workspace-grid iframe")).to_have_count(1)
            self.assertTrue(protected_responses)
            self.assertNotIn(403, protected_responses)
        finally:
            page.close()

    def test_expired_terminal_elevation_prompts_and_reconnects_after_auth(self):
        page = self.new_page()
        try:
            self.goto_shell(page)
            page.wait_for_function("typeof recoverTerminalElevation === 'function'")
            page.evaluate(
                """
                () => {
                  window.__terminalElevationReconnect = false;
                  recoverTerminalElevation(
                    {disposed: false},
                    '重新连接主机终端',
                    () => { window.__terminalElevationReconnect = true; }
                  );
                  window.__parallelElevationResult = null;
                  requestElevatedAccess('并发桌面授权').then(result => {
                    window.__parallelElevationResult = result;
                  });
                }
                """
            )
            expect(page.locator("#elevate-modal")).to_have_class(re.compile(r"show"))
            expect(page.locator("#elevate-username")).to_have_value("ui-admin")
            page.locator("#elevate-password").fill("UiSmokeAdmin-2026!")
            page.locator("#elevate-modal .btn-primary").click()
            page.wait_for_function(
                "state.elevated && window.__terminalElevationReconnect && window.__parallelElevationResult === true"
            )
            expect(page.locator("#elevate-modal")).not_to_have_class(re.compile(r"show"))
        finally:
            page.close()

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
                self.show_all_sidebar_pages(page)
                page.evaluate('(name) => window.switchPage(name, null)', page_name)
                expect(page.locator(f"#page-{page_name}")).to_have_class(re.compile(r"active"))
            self.assertEqual(page_errors, [])
        finally:
            page.close()

    def test_first_visit_defaults_to_test_page(self):
        page = self.new_page()
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

    def test_test_workspace_operation_switches_to_system_log(self):
        page = self.new_page()
        page_errors = []
        page.on("pageerror", lambda exc: page_errors.append(str(exc)))
        try:
            self.goto_shell(page)
            page.wait_for_function("typeof switchLogTab === 'function'")
            page.evaluate("switchLogTab('module')")
            expect(page.locator('.log-tab-btn[data-log-tab="module"]')).to_have_class(
                re.compile(r"\bactive\b")
            )

            page.evaluate("document.querySelector('#btn-device-info').click()")

            expect(page.locator('.log-tab-btn[data-log-tab="system"]')).to_have_class(
                re.compile(r"\bactive\b")
            )
            expect(page.locator('.log-tab-btn[data-log-tab="module"]')).not_to_have_class(
                re.compile(r"\bactive\b")
            )
            self.assert_no_page_errors(page_errors)
        finally:
            page.close()

    def test_start_test_wakes_cluster_log_polling_immediately(self):
        page = self.new_page()
        event_requests = []

        def handle_job(route):
            if "/events?" in route.request.url:
                event_requests.append(time.monotonic())
                route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps({
                        "success": True,
                        "events": [{
                            "sequence": 0,
                            "level": "info",
                            "source": "stdout",
                            "message": "wrapper output without suite keyword",
                        }, {
                            "sequence": 1,
                            "level": "info",
                            "source": "stdout",
                            "message": "VTS immediate polling log",
                        }],
                    }),
                )
                return
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({
                    "success": True,
                    "job": {
                        "id": "job-immediate",
                        "status": "running",
                        "assigned_worker_id": "ats-worker-controller",
                        "current_attempt_id": "attempt-immediate",
                    },
                }),
            )

        try:
            self.goto_shell(page)
            page.route(
                "**/api/test/start",
                lambda route: route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps({
                        "success": True,
                        "data": {
                            "cluster_job_id": "job-immediate",
                            "attempt_id": "attempt-immediate",
                        },
                    }),
                ),
            )
            page.route("**/api/cluster/jobs/job-immediate**", handle_job)
            page.evaluate(
                """async () => {
                    startStatusPolling();
                    await new Promise(resolve => setTimeout(resolve, 300));
                    state.selectedDevices = new Set(['SERIAL-1']);
                    document.querySelector('#test-module').value = 'MockModule';
                    document.querySelector('#test-case').value = 'MockClass#testCase';
                    const suite = document.querySelector('#test-suite');
                    suite.replaceChildren(new Option('/tmp/mock-suite', '/tmp/mock-suite'));
                    await startTest();
                }"""
            )
            page.wait_for_timeout(750)
            diagnostic = page.evaluate(
                """() => {
                    flushLogQueue();
                    return {
                        clusterJobId: state.clusterJobId,
                        testing: state.testing,
                        stopping: state.testStopping,
                        systemLog: document.querySelector('#system-log-output').textContent,
                        moduleLog: document.querySelector('#module-log-output').textContent
                    };
                }"""
            )
            self.assertTrue(event_requests, diagnostic)
            self.assertIn("wrapper output without suite keyword", diagnostic["moduleLog"], diagnostic)
            self.assertNotIn("wrapper output without suite keyword", diagnostic["systemLog"], diagnostic)
            self.assertIn("VTS immediate polling log", diagnostic["moduleLog"], diagnostic)

            page.evaluate("checkInitialTestStatus()")
            page.wait_for_timeout(1100)
            recovered = page.evaluate(
                """() => {
                    flushLogQueue();
                    const text = document.querySelector('#module-log-output').textContent;
                    return {
                        sequence: state.clusterEventSequence,
                        occurrences: text.split('VTS immediate polling log').length - 1
                    };
                }"""
            )
            self.assertEqual(recovered["sequence"], 1)
            self.assertEqual(recovered["occurrences"], 1)
            self.assertTrue(event_requests)
        finally:
            page.close()

    def test_start_test_capacity_conflict_is_explained_without_elevation(self):
        page = self.new_page()
        try:
            self.goto_shell(page)
            page.route(
                "**/api/test/start",
                lambda route: route.fulfill(
                    status=409,
                    content_type="application/json",
                    body=json.dumps({
                        "success": False,
                        "error": "worker capacity is exhausted",
                    }),
                ),
            )
            page.evaluate(
                """async () => {
                    state.elevated = false;
                    state.devices = [{
                        device_id: 'ats-worker-246:RK3576GMS1',
                        status: 'online',
                        locked: false
                    }];
                    state.selectedDevices = new Set([
                        'ats-worker-246:RK3576GMS1'
                    ]);
                    document.querySelector('#test-module').value = 'MockModule';
                    const suite = document.querySelector('#test-suite');
                    suite.replaceChildren(
                        new Option('/tmp/mock-suite', '/tmp/mock-suite')
                    );
                    await startTest();
                    flushLogQueue();
                }"""
            )

            expect(page.locator("#toast")).to_contain_text(
                "Worker 已达到最大并发任务数"
            )
            expect(page.locator("#system-log-output")).to_contain_text(
                "Worker 已达到最大并发任务数"
            )
            expect(page.locator("#elevate-modal")).not_to_have_class(
                re.compile(r"show")
            )
            self.assertFalse(page.evaluate("state.testing"))
        finally:
            page.close()

    def test_cluster_stop_keeps_polling_until_job_is_terminal(self):
        page = self.new_page()
        terminal = {"value": False}

        def handle_job(route):
            if "/events?" in route.request.url:
                route.fulfill(
                    status=200,
                    content_type="application/json",
                    body='{"success":true,"events":[]}',
                )
                return
            status = "cancelled" if terminal["value"] else "stopping"
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({
                    "success": True,
                    "job": {
                        "id": "job-stopping",
                        "status": status,
                        "error": "",
                        "assigned_worker_id": "ats-worker-controller",
                        "current_attempt_id": "attempt-stopping",
                    },
                }),
            )

        try:
            self.goto_shell(page)
            page.route(
                "**/api/cluster/jobs/job-stopping/cancel",
                lambda route: route.fulfill(
                    status=200,
                    content_type="application/json",
                    body='{"success":true}',
                ),
            )
            page.route("**/api/cluster/jobs/job-stopping**", handle_job)
            page.evaluate(
                """async () => {
                    startStatusPolling();
                    state.clusterJobId = 'job-stopping';
                    state.clusterEventSequence = -1;
                    state.testing = true;
                    updateTestToggleButton(true);
                    await stopTest();
                }"""
            )
            page.wait_for_timeout(400)
            stopping = page.evaluate(
                """() => ({
                    job: state.clusterJobId,
                    stopping: state.testStopping,
                    disabled: document.querySelector('#test-toggle-btn').disabled,
                    label: document.querySelector('#test-toggle-btn').textContent
                })"""
            )
            self.assertEqual(stopping["job"], "job-stopping")
            self.assertTrue(stopping["stopping"])
            self.assertTrue(stopping["disabled"])
            self.assertIn("停止中", stopping["label"])

            terminal["value"] = True
            page.evaluate("wakeTestStatusPolling()")
            page.wait_for_function("state.clusterJobId === '' && !state.testStopping")
            completed = page.evaluate(
                """() => ({
                    disabled: document.querySelector('#test-toggle-btn').disabled,
                    label: document.querySelector('#test-toggle-btn').textContent
                })"""
            )
            self.assertFalse(completed["disabled"])
            self.assertIn("开始测试", completed["label"])
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

    def test_cluster_device_pool_hides_offline_inventory(self):
        page = self.new_page()
        try:
            page.goto(f"{self.base_url}/cluster", wait_until="domcontentloaded")
            page.wait_for_function("typeof render === 'function'")
            rows = page.evaluate(
                """() => {
                    state.workers = [];
                    state.devices = [
                        {worker_id: 'worker-1', serial: 'ONLINE-1', state: 'available', properties: {}},
                        {worker_id: 'worker-1', serial: 'OFFLINE-1', state: 'offline', properties: {}},
                    ];
                    state.suites = [];
                    state.jobs = [];
                    state.tests = [];
                    state.library = [];
                    render();
                    return Array.from(document.querySelectorAll('#devices tr')).map(row => row.textContent);
                }"""
            )

            self.assertTrue(any("ONLINE-1" in row for row in rows))
            self.assertFalse(any("OFFLINE-1" in row for row in rows))
        finally:
            page.close()

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
            test_page_frame = page.locator("#page-test").evaluate(
                """container => ({
                    borderTopWidth: getComputedStyle(container).borderTopWidth,
                    marginTop: getComputedStyle(container).marginTop,
                    paddingTop: getComputedStyle(container).paddingTop,
                })"""
            )
            self.assertEqual(
                test_page_frame,
                {"borderTopWidth": "0px", "marginTop": "0px", "paddingTop": "15px"},
            )

            expected_page_frame = None
            expected_analysis_upload_frame = None
            for page_name in self.visible_sidebar_pages(page):
                with self.subTest(page=page_name):
                    page.evaluate("target => openAgentPageAction(target, '{}')", page_name)
                    expect(page.locator(f"#page-{page_name}")).to_have_class(re.compile(r"active"))
                    expect(page.locator(f'.sidebar-item[data-page="{page_name}"]')).to_have_class(re.compile(r"active"))
                    if page_name != "test":
                        page_geometry = page.locator(f"#page-{page_name}").evaluate(
                            """container => {
                                const pageRect = container.getBoundingClientRect();
                                const title = container.querySelector('.section-title');
                                const titleRect = title ? title.getBoundingClientRect() : null;
                                const firstChild = container.firstElementChild;
                                const content = firstChild && firstChild.nextElementSibling;
                                const contentRect = content ? content.getBoundingClientRect() : null;
                                const containerStyle = getComputedStyle(container);
                                const frame = getComputedStyle(container, '::after');
                                return {
                                    pageLeft: pageRect.left,
                                    pageTop: pageRect.top,
                                    pageWidth: pageRect.width,
                                    pageHeight: pageRect.height,
                                    titleLeft: titleRect && titleRect.left,
                                    titleTop: titleRect && titleRect.top,
                                    contentLeft: contentRect && contentRect.left,
                                    contentTop: contentRect && contentRect.top,
                                    titleToContent: titleRect && contentRect && contentRect.top - titleRect.bottom,
                                    frameTop: parseFloat(frame.top),
                                    frameRight: parseFloat(frame.right),
                                    frameBottom: parseFloat(frame.bottom),
                                    frameLeft: parseFloat(frame.left),
                                    frameBorder: frame.borderTopWidth,
                                    frameDisplay: frame.display,
                                    pageBorder: containerStyle.borderTopWidth,
                                    pageMargin: containerStyle.marginTop,
                                };
                            }"""
                        )
                        page_frame = {
                            key: page_geometry[key]
                            for key in ["pageLeft", "pageTop", "pageWidth", "pageHeight"]
                        }
                        if expected_page_frame is None:
                            expected_page_frame = page_frame
                        else:
                            self.assertEqual(page_frame, expected_page_frame)
                        self.assertAlmostEqual(
                            page_geometry["titleLeft"] - page_geometry["pageLeft"],
                            15,
                            delta=0.5,
                        )
                        self.assertAlmostEqual(
                            page_geometry["titleTop"] - page_geometry["pageTop"],
                            15,
                            delta=0.5,
                        )
                        self.assertAlmostEqual(
                            page_geometry["contentLeft"],
                            page_geometry["titleLeft"],
                            delta=0.5,
                        )
                        self.assertAlmostEqual(page_geometry["titleToContent"], 6, delta=0.5)
                        self.assertAlmostEqual(page_geometry["frameLeft"], 15, delta=0.5)
                        self.assertAlmostEqual(page_geometry["frameRight"], 15, delta=0.5)
                        self.assertAlmostEqual(page_geometry["frameBottom"], 15, delta=0.5)
                        self.assertAlmostEqual(
                            page_geometry["pageTop"] + page_geometry["frameTop"],
                            page_geometry["contentTop"],
                            delta=0.5,
                        )
                        self.assertEqual(page_geometry["frameBorder"], "1px")
                        self.assertEqual(page_geometry["pageBorder"], "0px")
                        self.assertEqual(page_geometry["pageMargin"], "0px")
                        overlapping_frames = page.locator(f"#page-{page_name}").evaluate(
                            """container => {
                                const pageRect = container.getBoundingClientRect();
                                const frameStyle = getComputedStyle(container, '::after');
                                if (frameStyle.display === 'none') return [];
                                const target = {
                                    left: pageRect.left + parseFloat(frameStyle.left),
                                    top: pageRect.top + parseFloat(frameStyle.top),
                                    right: pageRect.right - parseFloat(frameStyle.right),
                                    bottom: pageRect.bottom - parseFloat(frameStyle.bottom),
                                };
                                return Array.from(container.querySelectorAll('*')).filter(node => {
                                    const style = getComputedStyle(node);
                                    if (style.display === 'none' || style.visibility === 'hidden') return false;
                                    if (parseFloat(style.borderTopWidth) <= 0) return false;
                                    const rect = node.getBoundingClientRect();
                                    return Math.abs(rect.left - target.left) <= 1
                                        && Math.abs(rect.top - target.top) <= 1
                                        && Math.abs(rect.right - target.right) <= 1
                                        && Math.abs(rect.bottom - target.bottom) <= 1;
                                }).map(node => node.id || node.className || node.tagName);
                            }"""
                        )
                        self.assertEqual(overlapping_frames, [])
                        if page_name in {"report-analysis", "apk-analysis"}:
                            upload_selector = (
                                "#report-upload-zone"
                                if page_name == "report-analysis"
                                else "#apk-upload-zone"
                            )
                            expect(page.locator(upload_selector)).to_have_class(
                                re.compile(r"\bupload-empty\b")
                            )
                            upload_geometry = page.locator(upload_selector).evaluate(
                                """zone => {
                                    const rect = zone.getBoundingClientRect();
                                    const pageRect = zone.closest('.page-content').getBoundingClientRect();
                                    const pageTitleStyle = getComputedStyle(
                                        zone.closest('.page-content').querySelector(':scope > .section-title')
                                    );
                                    const title = zone.querySelector('.analysis-upload-title');
                                    const instructions = zone.querySelector('.upload-instructions');
                                    const button = zone.querySelector('button');
                                    const titleRect = title.getBoundingClientRect();
                                    const buttonRect = button.getBoundingClientRect();
                                    const titleStyle = getComputedStyle(title);
                                    const instructionsStyle = getComputedStyle(instructions);
                                    const buttonStyle = getComputedStyle(button);
                                    return {
                                        left: rect.left - pageRect.left,
                                        top: rect.top - pageRect.top,
                                        bottom: pageRect.bottom - rect.bottom,
                                        width: rect.width,
                                        height: rect.height,
                                        titleLeft: titleRect.left - pageRect.left,
                                        titleTop: titleRect.top - pageRect.top,
                                        buttonLeft: buttonRect.left - pageRect.left,
                                        buttonTop: buttonRect.top - pageRect.top,
                                        buttonWidth: buttonRect.width,
                                        borderStyle: getComputedStyle(zone).borderTopStyle,
                                        borderWidth: getComputedStyle(zone).borderTopWidth,
                                        borderRadius: getComputedStyle(zone).borderTopLeftRadius,
                                        minHeight: getComputedStyle(zone).minHeight,
                                        pageTitleFont: [
                                            pageTitleStyle.fontFamily,
                                            pageTitleStyle.fontSize,
                                            pageTitleStyle.fontWeight,
                                            pageTitleStyle.lineHeight,
                                        ],
                                        titleFont: [
                                            titleStyle.fontFamily,
                                            titleStyle.fontSize,
                                            titleStyle.fontWeight,
                                            titleStyle.lineHeight,
                                        ],
                                        instructionsFont: [
                                            instructionsStyle.fontFamily,
                                            instructionsStyle.fontSize,
                                            instructionsStyle.fontWeight,
                                            instructionsStyle.lineHeight,
                                        ],
                                        buttonFont: [
                                            buttonStyle.fontFamily,
                                            buttonStyle.fontSize,
                                            buttonStyle.fontWeight,
                                            buttonStyle.lineHeight,
                                        ],
                                    };
                                }"""
                            )
                            if expected_analysis_upload_frame is None:
                                expected_analysis_upload_frame = upload_geometry
                            else:
                                self.assertEqual(
                                    upload_geometry,
                                    expected_analysis_upload_frame,
                                )
                            self.assertAlmostEqual(upload_geometry["left"], 15, delta=0.5)
                            self.assertAlmostEqual(upload_geometry["top"], 40, delta=0.5)
                            self.assertAlmostEqual(
                                upload_geometry["height"],
                                page.evaluate("window.innerHeight * 0.5"),
                                delta=0.5,
                            )
                            self.assertGreater(upload_geometry["bottom"], 15)
                            self.assertEqual(upload_geometry["borderStyle"], "solid")
                            self.assertEqual(upload_geometry["borderWidth"], "1px")
                            self.assertEqual(upload_geometry["borderRadius"], "8px")
                            self.assertEqual(
                                upload_geometry["minHeight"],
                                f"{page.evaluate('window.innerHeight * 0.5'):g}px",
                            )
                            self.assertAlmostEqual(upload_geometry["buttonWidth"], 160, delta=0.5)
                            self.assertEqual(page_geometry["frameDisplay"], "none")
                            if page_name == "report-analysis":
                                self.assertEqual(
                                    page.locator("#report-upload-progress").evaluate(
                                        "progress => getComputedStyle(progress).opacity"
                                    ),
                                    "0",
                                )
                                page.locator(upload_selector).evaluate(
                                    "zone => zone.classList.remove('upload-empty')"
                                )
                                result_state_frame = page.locator(f"#page-{page_name}").evaluate(
                                    """(container, selector) => {
                                        const zone = container.querySelector(selector);
                                        const frame = getComputedStyle(container, '::after');
                                        return {
                                            frameDisplay: frame.display,
                                            uploadBorder: getComputedStyle(zone).borderTopWidth,
                                            uploadHeight: zone.getBoundingClientRect().height,
                                        };
                                    }""",
                                    upload_selector,
                                )
                                self.assertEqual(result_state_frame["frameDisplay"], "block")
                                self.assertEqual(result_state_frame["uploadBorder"], "0px")
                                self.assertAlmostEqual(
                                    result_state_frame["uploadHeight"],
                                    80,
                                    delta=0.5,
                                )
                                page.locator(upload_selector).evaluate(
                                    "zone => zone.classList.add('upload-empty')"
                                )
                        if page_name == "architecture":
                            architecture_frame = page.locator(
                                "#page-architecture .architecture-container"
                            ).evaluate(
                                """container => {
                                    const style = getComputedStyle(container);
                                    const iframeStyle = getComputedStyle(container.querySelector('iframe'));
                                    return {
                                        background: style.backgroundColor,
                                        border: style.borderTopWidth,
                                        padding: style.paddingTop,
                                        radius: style.borderTopLeftRadius,
                                        iframeRadius: iframeStyle.borderTopLeftRadius,
                                    };
                                }"""
                            )
                            self.assertEqual(
                                architecture_frame,
                                {
                                    "background": "rgba(0, 0, 0, 0)",
                                    "border": "0px",
                                    "padding": "0px",
                                    "radius": "8px",
                                    "iframeRadius": "7px",
                                },
                            )

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
            path = request.url.split(self.base_url, 1)[-1]
            if "/api/usbip/source-devices" in request.url:
                route.fulfill(
                    status=200,
                    content_type="application/json",
                    body='{"success":true,"devices":[{"busid":"1-2","label":"Android 1-2"}]}',
                )
                return
            if path.startswith("/api/usbip/status"):
                requests.append({
                    "method": request.method,
                    "path": path,
                    "body": None,
                })
                connected = any(
                    item["method"] == "POST"
                    and item["path"] == "/api/usbip/connect"
                    for item in requests
                ) and not any(
                    item["method"] == "POST"
                    and item["path"] == "/api/usbip/disconnect"
                    for item in requests
                )
                selection = (
                    ',"cluster_selections":[{"device_host":"tester@192.0.2.10",'
                    '"source_host":"","worker_id":"ats-worker-controller","busids":["1-2"],'
                    '"device_serials":["D1"],'
                    '"device_serials_by_busid":{"1-2":["D1"]}}]'
                    if connected else ',"cluster_selections":[]'
                )
                route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=(
                        f'{{"success":true,"connected":{str(connected).lower()},'
                        f'"device_host":"tester@192.0.2.10"{selection}}}'
                    ),
                )
                return
            if path == "/api/adb-forward/status":
                requests.append({
                    "method": request.method,
                    "path": path,
                    "body": None,
                })
                route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=(
                        '{"success":true,"connected":false,"cluster_enabled":true,'
                        '"local_worker_id":"ats-worker-controller","assignments":[],"hosts":['
                        '{"worker_id":"worker-source","name":"Device Host",'
                        '"address":"10.10.10.206","status":"online","adb_proxy":true,'
                        '"devices":[{"serial":"D1","state":"available",'
                        '"transport":"local_usb","model":"RK3572"}]},'
                        '{"worker_id":"ats-worker-controller","name":"Controller",'
                        '"address":"10.10.10.10","status":"online","adb_proxy":true,'
                        '"devices":[]}]}'
                    ),
                )
                return
            if path.startswith("/api/devices/list"):
                requests.append({
                    "method": request.method,
                    "path": path,
                    "body": None,
                })
                route.fulfill(
                    status=200,
                    content_type="application/json",
                    body='[{"device_id":"D1","status":"online"}]',
                )
                return
            requests.append(
                {
                    "method": request.method,
                    "path": path,
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
                state.elevated = true;
                requestElevatedAccess = async () => true;
                state.config = {...(state.config || {}), device_host: 'tester@192.0.2.10'};
                showConfirmDialog = async () => true;
                initAndStartVnc = async () => true;
                document.getElementById('wifi-ssid').value = 'LabWifi';
                document.getElementById('wifi-password').value = 'secret';
                """
            )

            page.evaluate("rebootDevices()")
            page.evaluate("remountDevices()")
            page.evaluate("submitWifiConfig()")
            page.evaluate("showDeviceScreen()")
            page.evaluate("lockSelectedDevices('lock')")
            page.evaluate("setupUsbipForward()")
            self.assertTrue(page.locator("#usbip-attach-modal").is_visible())
            attach_message = page.locator("#usbip-attach-message").inner_text()
            self.assertIn("Windows/Linux 按住 Ctrl", attach_message)
            self.assertIn("macOS 按住 Command", attach_message)
            self.assertNotIn("⌘", attach_message)
            attach_style = page.locator(
                "#usbip-attach-modal .modal-content"
            ).evaluate(
                """element => {
                    const style = getComputedStyle(element);
                    return {
                        resize: style.resize,
                        width: Math.round(element.getBoundingClientRect().width),
                        height: Math.round(element.getBoundingClientRect().height)
                    };
                }"""
            )
            self.assertEqual(attach_style["resize"], "none")
            self.assertEqual(attach_style["width"], 620)
            expect(
                page.locator("#usbip-attach-modal .modal-content")
            ).to_have_class(re.compile(r"\bdevice-routing-modal-content\b"))
            page.evaluate("submitUsbipAttach()")
            self.assertTrue(page.locator("#usbip-attach-modal").is_visible())
            expect(page.locator("#usbip-source-device")).to_be_disabled()
            expect(page.locator("#usbip-source-device")).to_contain_text(
                "该来源设备均已接入"
            )
            page.evaluate("setupAdbPortForward()")
            self.assertTrue(page.locator("#adb-proxy-modal").is_visible())
            self.assertEqual(
                page.locator("#adb-proxy-source-host").inner_text().strip(),
                "worker-source",
            )
            page.evaluate("submitAdbProxyConnect()")

            device_refreshes = [
                item for item in requests
                if item["path"].startswith("/api/devices/list?")
            ]
            self.assertGreaterEqual(len(device_refreshes), 1)
            self.assertTrue(all(
                item["method"] == "GET"
                and "force_refresh=1" in item["path"]
                for item in device_refreshes
            ))
            self.assertTrue(any(
                "source=auto" in item["path"]
                for item in device_refreshes
            ))
            self.assertEqual(
                [
                    (item["method"], item["path"])
                    for item in requests
                    if not item["path"].startswith("/api/devices/list?")
                ],
                [
                    ("POST", "/api/devices/reboot"),
                    ("POST", "/api/devices/remount"),
                    ("POST", "/api/devices/wifi"),
                    ("POST", "/api/devices/scrcpy"),
                    ("POST", "/api/devices/bootloader-lock"),
                    ("GET", "/api/usbip/status"),
                    ("GET", "/api/usbip/status"),
                    ("POST", "/api/usbip/connect"),
                    ("GET", "/api/usbip/status?device_host=tester%40192.0.2.10"),
                    ("GET", "/api/usbip/status?device_host=tester%40192.0.2.10"),
                    ("GET", "/api/adb-forward/status"),
                    ("POST", "/api/adb-forward/start"),
                    ("GET", "/api/adb-forward/status"),
                ],
            )
            def request_body(path):
                return next(
                    item["body"] for item in requests if item["path"] == path
                )
            self.assertEqual(request_body("/api/devices/reboot"), {"devices": ["D1"]})
            self.assertEqual(request_body("/api/devices/remount"), {"devices": ["D1"]})
            self.assertEqual(
                request_body("/api/devices/wifi"),
                {"devices": ["D1"], "ssid": "LabWifi", "password": "secret"},
            )
            self.assertEqual(
                request_body("/api/devices/bootloader-lock"),
                {"devices": ["D1"]},
            )
            self.assertEqual(request_body("/api/devices/scrcpy"), {"devices": ["D1"]})
            self.assertEqual(
                next(
                    item["body"] for item in requests
                    if item["path"] == "/api/usbip/connect"
                ),
                {
                    "device_host": "tester@192.0.2.10",
                    "worker_id": "ats-worker-controller",
                    "busids": ["1-2"],
                    "manual_connect": True,
                },
            )
            self.assertEqual(
                next(
                    item["body"] for item in requests
                    if item["path"] == "/api/adb-forward/start"
                ),
                {
                    "source_worker_id": "worker-source",
                    "target_worker_id": "ats-worker-controller",
                    "devices": ["D1"],
                },
            )

            requests.clear()
            page.evaluate("closeAdbProxyModal()")
            page.evaluate("setupUsbipForward()")
            self.assertTrue(page.locator("#usbip-attach-modal").is_visible())
            self.assertIn(
                "D1",
                page.locator("#usbip-assignments").inner_text(),
            )
            manage_style = page.locator(
                "#usbip-attach-modal .modal-content"
            ).evaluate(
                """element => {
                    const style = getComputedStyle(element);
                    return {
                        resize: style.resize,
                        width: Math.round(element.getBoundingClientRect().width),
                        height: Math.round(element.getBoundingClientRect().height)
                    };
                }"""
            )
            self.assertEqual(manage_style["resize"], "none")
            self.assertEqual(manage_style["width"], 620)
            expect(
                page.locator("#usbip-attach-modal .modal-content")
            ).to_have_class(re.compile(r"\bdevice-routing-modal-content\b"))
            disconnect_button = page.locator("#usbip-assignments button").first
            expect(disconnect_button).to_be_enabled()
            disconnect_button.click()
            for _ in range(20):
                if any(
                    item["path"] == "/api/usbip/disconnect"
                    for item in requests
                ):
                    break
                page.wait_for_timeout(50)
            relevant = [
                (index, item)
                for index, item in enumerate(requests)
                if item["path"] == "/api/usbip/disconnect"
                or item["path"].startswith("/api/usbip/status?device_host=")
            ]
            disconnect_index = next(
                index for index, item in enumerate(requests)
                if item["path"] == "/api/usbip/disconnect"
            )
            for _ in range(20):
                if any(
                    item["path"].startswith("/api/devices/list?force_refresh=1")
                    for item in requests[disconnect_index + 1:]
                ):
                    break
                page.wait_for_timeout(50)
            self.assertEqual(
                [
                    (item["method"], item["path"])
                    for index, item in relevant
                    if index <= disconnect_index
                ],
                [
                    ("GET", "/api/usbip/status?device_host=tester%40192.0.2.10"),
                    ("GET", "/api/usbip/status?device_host=tester%40192.0.2.10"),
                    ("POST", "/api/usbip/disconnect"),
                ],
            )
            self.assertTrue(page.locator("#usbip-attach-modal").is_visible())
            self.assertTrue(any(
                item["path"].startswith("/api/devices/list?force_refresh=1")
                and "source=auto" in item["path"]
                for item in requests[disconnect_index + 1:]
            ))
            self.assertEqual(
                requests[disconnect_index]["body"],
                {
                    "device_host": "tester@192.0.2.10",
                    "worker_id": "ats-worker-controller",
                    "busids": ["1-2"],
                    "source_host": "",
                },
            )
            stale_refresh_logs = page.evaluate(
                """async () => {
                    const originalSetTimeout = window.setTimeout;
                    const originalAddLogEntry = window.addLogEntry;
                    const messages = [];
                    window.setTimeout = callback => {
                        callback();
                        return 0;
                    };
                    window.addLogEntry = message => messages.push(message);
                    try {
                        usbipOperationGeneration = 10;
                        await refreshUsbipDetachedWorkers(new Map(), 9);
                    } finally {
                        window.setTimeout = originalSetTimeout;
                        window.addLogEntry = originalAddLogEntry;
                    }
                    return messages;
                }"""
            )
            self.assertEqual(stale_refresh_logs, [])
        finally:
            page.close()

    def test_fastboot_device_is_visible_but_not_adb_selectable(self):
        page = self.new_page()
        try:
            self.goto_shell(page)
            page.evaluate(
                """
                state.devices = [
                    {device_id: 'ADB-1', status: 'online', protocol: 'adb', locked: false},
                    {device_id: 'FB-1', status: 'fastboot', protocol: 'fastboot', locked: false}
                ];
                state.selectedDevices = new Set();
                renderDevices();
                """
            )

            fastboot = page.locator('.device-item[data-device-id="FB-1"]')
            expect(fastboot).to_contain_text("Fastboot")
            expect(fastboot.locator('input[type="checkbox"]')).to_be_enabled()

            page.evaluate("toggleDevice('FB-1')")
            self.assertEqual(
                page.evaluate("Array.from(state.selectedDevices)"),
                ["FB-1"],
            )
            self.assertFalse(page.evaluate("validateDeviceSelection()"))
            self.assertTrue(page.evaluate("validateBootloaderDeviceSelection()"))

            bootloader_requests = []

            def handle_bootloader_request(route, request):
                bootloader_requests.append(request.post_data_json)
                route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=(
                        '{"success":false,"error":"mock unlock failed",'
                        '"data":{"results":[{"device":"FB-1","success":false,'
                        '"error":"still locked"}],"summary":{"failed":1}}}'
                    ),
                )

            page.route(
                "**/api/devices/bootloader-unlock",
                handle_bootloader_request,
            )
            page.evaluate("requestElevatedAccess = async () => true")
            operation_logs = page.evaluate(
                """async () => {
                    const originalAddLogEntry = window.addLogEntry;
                    const messages = [];
                    window.addLogEntry = message => messages.push(message);
                    try {
                        await lockSelectedDevices('unlock');
                    } finally {
                        window.addLogEntry = originalAddLogEntry;
                    }
                    return messages;
                }"""
            )
            self.assertEqual(
                bootloader_requests,
                [{"devices": ["FB-1"]}],
            )
            self.assertIn("设备解锁失败: mock unlock failed", operation_logs)
            self.assertNotIn("设备解锁完成", operation_logs)

            page.evaluate("state.selectedDevices.clear()")
            page.evaluate("selectAllDevices()")
            self.assertEqual(
                page.evaluate("Array.from(state.selectedDevices)"),
                ["ADB-1"],
            )
        finally:
            page.close()

    def test_adb_proxy_device_is_visible_for_tests_but_blocks_usb_actions(self):
        page = self.new_page()
        try:
            page.set_viewport_size({"width": 1920, "height": 1080})
            self.goto_shell(page)
            page.evaluate(
                """
                state.devices = [
                    {
                        device_id: 'ats-worker-246:RK3576GMS1',
                        serial: 'RK3576GMS1',
                        worker_id: 'ats-worker-246',
                        status: 'online',
                        protocol: 'adb',
                        transport: 'adb_proxy',
                        adb_proxy_source_worker_id: 'ats-worker-controller',
                        locked: false
                    },
                    {
                        device_id: 'LOCAL-2',
                        serial: 'LOCAL-2',
                        status: 'online',
                        protocol: 'adb',
                        transport: 'local_usb',
                        locked: false
                    },
                    {
                        device_id: 'LOCAL-3',
                        serial: 'LOCAL-3',
                        status: 'online',
                        protocol: 'adb',
                        transport: 'local_usb',
                        locked: false
                    },
                    {
                        device_id: 'ats-worker-246:USBIP001',
                        serial: 'USBIP001',
                        status: 'online',
                        protocol: 'adb',
                        transport: 'usbip',
                        is_usbip: true,
                        usbip_source_host: 'tester@192.0.2.10',
                        locked: false
                    }
                ];
                state.selectedDevices = new Set(['ats-worker-246:RK3576GMS1']);
                renderDevices();
                """
            )

            device = page.locator(
                '.device-item[data-device-id="ats-worker-246:RK3576GMS1"]'
            )
            expect(device.locator(".device-id")).to_have_text("RK3576GMS1")
            expect(device.locator(".device-source")).to_have_text(
                "ADB · ats-worker-controller"
            )
            expect(
                page.locator(
                    '.device-item[data-device-id="ats-worker-246:USBIP001"] '
                    + '.device-source'
                )
            ).to_have_text("USB/IP · 192.0.2.10")
            locked_usbip = page.evaluate(
                """() => {
                    const card = buildDeviceItemEl({
                        deviceId: 'USBIP-LOCKED',
                        displaySerial: 'RK3576GMS1',
                        isLocked: true,
                        lockedBy: 'hcq@172.16.14.66',
                        selectable: false,
                        transport: 'usbip',
                        isUsbip: true,
                        usbipSourceHost: 'hcq@172.16.14.66'
                    });
                    return {
                        infoLines: Array.from(
                            card.querySelector('.device-info').children
                        ).map(element => element.textContent),
                        status: card.querySelector('.device-status').textContent,
                        lockRows: card.querySelectorAll('.lock-status').length,
                        title: card.title
                    };
                }"""
            )
            self.assertEqual(
                locked_usbip["infoLines"],
                ["RK3576GMS1", "USB/IP · 172.16.14.66"],
            )
            self.assertEqual(locked_usbip["status"], "已分配")
            self.assertEqual(locked_usbip["lockRows"], 0)
            self.assertIn("占用：hcq@172.16.14.66", locked_usbip["title"])
            self.assertEqual(
                device.locator(".device-info").evaluate(
                    "element => getComputedStyle(element).columnGap"
                ),
                "12px",
            )
            self.assertEqual(
                page.locator("#device-list-left .device-item").count(),
                4,
            )
            self.assertEqual(
                page.locator("#device-list-left").evaluate(
                    """element => getComputedStyle(element)
                        .gridTemplateColumns.split(' ').length"""
                ),
                3,
            )
            self.assertEqual(
                page.locator("#device-list-right .device-item").count(),
                0,
            )
            expect(device.locator('input[type="checkbox"]')).to_be_enabled()
            self.assertTrue(page.evaluate("validateDeviceSelection()"))
            self.assertFalse(page.evaluate("validateBootloaderDeviceSelection()"))
            for button_id in (
                "btn-lock-device",
                "btn-unlock-device",
                "btn-burn-firmware",
                "btn-burn-gsi",
            ):
                expect(page.locator(f"#{button_id}")).to_be_disabled()
        finally:
            page.close()

    def test_cluster_device_management_groups_hosts_and_labels_adb_proxy(self):
        page = self.new_page()
        try:
            self.goto_shell(page)
            page.evaluate(
                """
                devicesManagementClusterMode = true;
                state.deviceGroups = [];
                state.groupFilter = '';
                allDevices = [{
                    device_id: 'ats-worker-246:RK3576GMS1',
                    serial_no: 'RK3576GMS1',
                    worker_id: 'ats-worker-246',
                    host_display_name: 'ats-worker-246',
                    source_host: 'ats-worker-controller → ats-worker-246',
                    source_type: 'adb_proxy',
                    transport: 'adb_proxy',
                    status: 'online',
                    cluster_state: 'allocated',
                    cluster_readonly: true,
                    cluster_shell_available: true,
                    cluster_device_inspection: true,
                    locked_by: '测试任务'
                }];
                displayDevicesManagement(allDevices);
                """
            )

            table = page.locator("#devices-table-body")
            expect(table).to_contain_text("ats-worker-246")
            expect(table).to_contain_text("ADB Proxy")
            expect(table).to_contain_text("ats-worker-controller → ats-worker-246")
            expect(table).to_contain_text("已分配")
            expect(table.locator("tr.device-group-row")).to_have_count(1)
        finally:
            page.close()

    def test_adb_proxy_modal_offers_only_remaining_source_devices(self):
        page = self.new_page()
        try:
            self.goto_shell(page)
            result = page.evaluate(
                """
                () => {
                    adbProxyStatus = {
                        cluster_enabled: true,
                        hosts: [
                            {
                                worker_id: 'worker-source',
                                name: 'Device Host',
                                address: '172.16.14.233',
                                status: 'busy',
                                adb_proxy: true,
                                devices: [
                                    {serial: 'RK-A', state: 'available', transport: 'local_usb'},
                                    {serial: 'RK-B', state: 'available', transport: 'local_usb'}
                                ]
                            },
                            {
                                worker_id: 'worker-target',
                                name: 'Test Host',
                                address: '172.16.14.246',
                                status: 'online',
                                adb_proxy: true,
                                devices: [
                                    {serial: 'RK-A', state: 'available', transport: 'adb_proxy'}
                                ]
                            }
                        ],
                        assignments: [{
                            source_worker_id: 'worker-source',
                            source_name: 'Device Host',
                            target_worker_id: 'worker-target',
                            target_name: 'Test Host',
                            devices: ['RK-A'],
                            status: 'connected'
                        }]
                    };
                    adbProxyOperationRunning = false;
                    renderAdbProxyHosts();
                    renderAdbProxyAssignments();
                    return {
                        source: document.getElementById('adb-proxy-source-host').value,
                        sourceLabel: document.getElementById(
                            'adb-proxy-source-host'
                        ).selectedOptions[0]?.textContent,
                        target: document.getElementById('adb-proxy-target-host').value,
                        targetLabel: document.getElementById(
                            'adb-proxy-target-host'
                        ).selectedOptions[0]?.textContent,
                        devices: Array.from(
                            document.getElementById('adb-proxy-source-devices').options
                        ).map(option => option.value).filter(Boolean),
                        assignment: document.getElementById(
                            'adb-proxy-assignments'
                        ).textContent,
                        message: document.getElementById('adb-proxy-message').textContent,
                        submitDisabled: document.getElementById(
                            'adb-proxy-connect-submit'
                        ).disabled
                    };
                }
                """
            )

            self.assertEqual(result["source"], "worker-source")
            self.assertEqual(result["sourceLabel"], "worker-source")
            self.assertEqual(result["target"], "worker-target")
            self.assertEqual(result["targetLabel"], "worker-target")
            self.assertEqual(result["devices"], ["RK-B"])
            self.assertIn(
                "worker-source → worker-target｜设备：RK-A",
                result["assignment"],
            )
            self.assertNotIn("172.16.14.", result["assignment"])
            self.assertNotIn("connected", result["assignment"])
            self.assertIn("还有 1 台", result["message"])
            self.assertFalse(result["submitDisabled"])
        finally:
            page.close()

    def test_adb_proxy_modal_supports_compact_source_only_ubuntu_host(self):
        page = self.new_page()
        try:
            self.goto_shell(page)
            result = page.evaluate(
                """
                () => {
                    adbProxyStatus = {
                        cluster_enabled: false,
                        hosts: [
                            {
                                worker_id: 'ats-worker-controller',
                                name: 'Controller Local Worker',
                                address: '172.16.14.233',
                                status: 'online',
                                adb_proxy: true,
                                adb_proxy_source_only: false,
                                devices: []
                            },
                            {
                                worker_id: 'adb-source-246',
                                name: 'Ubuntu ADB来源',
                                address: '172.16.14.246',
                                status: 'online',
                                adb_proxy: true,
                                adb_proxy_source_only: true,
                                devices: [{
                                    serial: 'RK3576GMS1',
                                    state: 'available',
                                    transport: 'local_usb'
                                }]
                            }
                        ],
                        assignments: []
                    };
                    adbProxyOperationRunning = false;
                    renderAdbProxyHosts();
                    ModalManager.open('adb-proxy-modal');
                    return {
                        sources: Array.from(
                            document.getElementById('adb-proxy-source-host').options
                        ).map(option => option.value).filter(Boolean),
                        targets: Array.from(
                            document.getElementById('adb-proxy-target-host').options
                        ).map(option => option.value).filter(Boolean),
                        hasUbuntuForm: Boolean(
                            document.getElementById('adb-proxy-ubuntu-host')
                            && document.getElementById('adb-proxy-ubuntu-password')
                        )
                    };
                }
                """
            )

            self.assertEqual(result["sources"], ["adb-source-246"])
            self.assertEqual(result["targets"], ["ats-worker-controller"])
            self.assertTrue(result["hasUbuntuForm"])
            source_box = page.locator("#adb-proxy-source-host").bounding_box()
            target_box = page.locator("#adb-proxy-target-host").bounding_box()
            self.assertGreater(target_box["y"], source_box["y"] + source_box["height"])
            modal_height = page.locator(
                "#adb-proxy-modal .adb-proxy-modal-content"
            ).bounding_box()["height"]
            self.assertLess(modal_height, 550)
            expect(
                page.locator("#adb-proxy-modal .modal-content")
            ).to_have_class(re.compile(r"\bdevice-routing-modal-content\b"))
        finally:
            page.close()

    def test_adb_proxy_modal_keeps_busy_targets_visible_but_disabled(self):
        page = self.new_page()
        try:
            self.goto_shell(page)
            result = page.evaluate(
                """
                () => {
                    adbProxyStatus = {
                        cluster_enabled: true,
                        local_worker_id: 'ats-worker-controller',
                        hosts: [
                            {
                                worker_id: 'ats-worker-244',
                                status: 'online',
                                adb_proxy: true,
                                devices: [{
                                    serial: 'ATS244001',
                                    state: 'available',
                                    transport: 'local_usb'
                                }]
                            },
                            {
                                worker_id: 'ats-worker-246',
                                status: 'busy',
                                adb_proxy: true,
                                devices: []
                            },
                            {
                                worker_id: 'ats-worker-controller',
                                status: 'busy',
                                adb_proxy: true,
                                devices: []
                            }
                        ],
                        assignments: []
                    };
                    adbProxyOperationRunning = false;
                    renderAdbProxyHosts();
                    return {
                        target: document.getElementById('adb-proxy-target-host').value,
                        options: Array.from(
                            document.getElementById('adb-proxy-target-host').options
                        ).map(option => ({
                            value: option.value,
                            label: option.textContent,
                            disabled: option.disabled
                        })),
                        message: document.getElementById('adb-proxy-message').textContent,
                        submitDisabled: document.getElementById(
                            'adb-proxy-connect-submit'
                        ).disabled
                    };
                }
                """
            )

            self.assertEqual(result["target"], "")
            self.assertEqual(
                result["options"],
                [
                    {
                        "value": "",
                        "label": "没有可用的ADB接入主机",
                        "disabled": True,
                    },
                    {
                        "value": "ats-worker-246",
                        "label": "ats-worker-246（测试中，不可用）",
                        "disabled": True,
                    },
                    {
                        "value": "ats-worker-controller",
                        "label": "ats-worker-controller（测试中，不可用）",
                        "disabled": True,
                    },
                ],
            )
            self.assertIn("ats-worker-246", result["message"])
            self.assertIn("正在执行测试", result["message"])
            self.assertTrue(result["submitDisabled"])
        finally:
            page.close()

    def test_adb_proxy_operation_refreshes_current_target_devices(self):
        page = self.new_page()
        try:
            self.goto_shell(page)
            calls = page.evaluate(
                """
                async () => {
                    const originalWorkspaceWorkerId = window.workspaceWorkerId;
                    const originalLoadDevices = window.loadDevices;
                    const captured = [];
                    window.workspaceWorkerId = () => 'worker-target';
                    window.loadDevices = async (...args) => {
                        captured.push(args);
                        return [];
                    };
                    try {
                        await refreshAdbProxyTargetDevices({
                            assignment: {target_worker_id: 'worker-target'}
                        });
                    } finally {
                        window.workspaceWorkerId = originalWorkspaceWorkerId;
                        window.loadDevices = originalLoadDevices;
                    }
                    return captured;
                }
                """
            )

            self.assertEqual(calls, [[True, {"silent": True}]])
        finally:
            page.close()

    def test_gsi_burn_refresh_waits_for_delayed_fastboot_inventory(self):
        page = self.new_page()
        try:
            self.goto_shell(page)
            page.evaluate(
                """
                state.clusterStatus = {
                    ...(state.clusterStatus || {}),
                    enabled: false,
                    local_worker_id: 'ats-worker-controller'
                };
                window.GmsWorkspace?.update({
                    scope_mode: 'single',
                    worker_id: 'ats-worker-controller',
                    device_ids: []
                }, {source: 'test'});
                state.devices = [
                    {device_id: 'LATE-FB', status: 'online', protocol: 'adb', locked: true}
                ];
                renderDevices();
                window.__burnRefreshCalls = 0;
                window.__stopBurnRefresh = startBurnDeviceProtocolRefresh(
                    ['LATE-FB'],
                    {
                        intervalMs: 100,
                        timeoutMs: 10000,
                        refreshDevices: async () => {
                            window.__burnRefreshCalls += 1;
                            const fastbootVisible =
                                window.__burnRefreshCalls >= 2;
                            state.devices = [{
                                device_id: 'LATE-FB',
                                status: fastbootVisible
                                    ? 'fastboot' : 'online',
                                protocol: fastbootVisible
                                    ? 'fastboot' : 'adb',
                                locked: true
                            }];
                            renderDevices();
                            return state.devices;
                        }
                    }
                );
                void 0;
                """
            )

            page.wait_for_timeout(500)
            self.assertGreaterEqual(
                page.evaluate("window.__burnRefreshCalls"),
                2,
                page.evaluate(
                    """
                    ({
                        workerId: workspaceWorkerId(),
                        localWorkerId: workspaceLocalWorkerId(),
                        isLocal: isLocalWorkspaceWorker(workspaceWorkerId())
                    })
                    """
                ),
            )
            expect(
                page.locator('.device-item[data-device-id="LATE-FB"]')
            ).to_contain_text("Fastboot")
        finally:
            page.evaluate("window.__stopBurnRefresh?.()")
            page.close()

    def test_remote_device_controls_follow_worker_capabilities(self):
        page = self.new_page()
        worker_ready = {"value": False}

        def json_response(route, payload):
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(payload),
            )

        page.route(
            "**/api/devices/management",
            lambda route: json_response(route, {"success": True, "devices": []}),
        )
        page.route(
            "**/api/cluster/status",
            lambda route: json_response(
                route,
                {
                    "success": True,
                    "enabled": True,
                    "local_worker_id": "ats-worker-controller",
                },
            ),
        )
        page.route(
            "**/api/cluster/devices",
            lambda route: json_response(
                route,
                {
                    "success": True,
                    "devices": [
                        {
                            "id": "worker-1:REMOTE-1",
                            "serial": "REMOTE-1",
                            "worker_id": "worker-1",
                            "state": "available",
                            "properties": {},
                        }
                    ],
                },
            ),
        )

        def cluster_hosts(route):
            ready = worker_ready["value"]
            json_response(
                route,
                {
                    "success": True,
                    "hosts": [
                        {
                            "worker_id": "worker-1",
                            "name": "Worker 1",
                            "status": "online",
                            "address": "192.0.2.10" if ready else "",
                            "ssh_user": "tester" if ready else "",
                            "capabilities": {"device_inspection": ready},
                        }
                    ],
                },
            )

        page.route("**/api/cluster/hosts", cluster_hosts)
        page.route(
            "**/api/cluster/workers",
            lambda route: json_response(
                route,
                {
                    "success": True,
                    "workers": [
                        {
                            "id": "worker-1",
                            "status": "online",
                            "capabilities": {
                                "device_inspection": worker_ready["value"]
                            },
                        }
                    ],
                },
            ),
        )

        def cluster_device_action(route):
            body = route.request.post_data_json
            if body.get("action") == "screenshot":
                payload = {"success": True, "image": "data:image/png;base64,iVBORw0KGgo="}
            else:
                payload = {"success": True, "elements": [], "source": "android_cli"}
            json_response(route, payload)

        page.route("**/api/cluster/devices/actions", cluster_device_action)
        page.route(
            "**/api/device-groups",
            lambda route: json_response(
                route, {"success": True, "data": {"groups": []}}
            ),
        )

        try:
            self.goto_shell(page)
            page.wait_for_function("state.clusterStatus?.enabled === true")
            page.evaluate(
                "GmsWorkspace.update({scope_mode: 'cluster', worker_id: 'worker-1'})"
            )
            page.evaluate("switchPage('devices')")
            page.evaluate("loadDevicesManagement()")
            row = page.locator('#devices-table-body tr').filter(has_text="REMOTE-1")
            expect(row).to_have_count(1)
            expect(row.locator("button", has_text="adb shell")).to_be_disabled()
            expect(row.locator("button", has_text="device info")).to_be_disabled()
            expect(row.locator("button", has_text="UI 操控")).to_be_disabled()

            worker_ready["value"] = True
            page.evaluate("loadDevicesManagement()")
            row = page.locator('#devices-table-body tr').filter(has_text="REMOTE-1")
            expect(row.locator("button", has_text="adb shell")).to_be_enabled()
            expect(row.locator("button", has_text="device info")).to_be_enabled()
            expect(row.locator("button", has_text="UI 操控")).to_be_enabled()
            row.locator("button", has_text="UI 操控").click()
            expect(page.locator("#page-devices")).to_be_visible()
            expect(page.locator("#ui-control-modal")).to_have_class(re.compile(r"\bshow\b"))
        finally:
            page.evaluate(
                "GmsWorkspace.update({scope_mode: 'single', worker_id: 'ats-worker-controller'})"
            )
            page.wait_for_timeout(200)
            page.close()

    def test_device_info_discards_stale_device_responses_and_uses_fast_default(self):
        page = self.new_page()
        page_errors = []
        page.on("pageerror", lambda error: page_errors.append(str(error)))
        try:
            self.goto_shell(page)
            page.evaluate(
                """
                () => {
                    window.__dcfgOriginalFetch = window.fetch.bind(window);
                    window.__dcfgPendingProps = {};
                    window.__dcfgConfigUrls = [];
                    window.fetch = (input, init) => {
                        const url = String(input);
                        const parsed = new URL(url, location.origin);
                        const jsonResponse = payload => Promise.resolve(new Response(
                            JSON.stringify(payload),
                            {status: 200, headers: {'Content-Type': 'application/json'}}
                        ));
                        if (parsed.pathname === '/api/config-explorer/props') {
                            const serial = parsed.searchParams.get('device_id');
                            return new Promise(resolve => {
                                window.__dcfgPendingProps[serial] = rows => resolve(new Response(
                                    JSON.stringify({success: true, data: {rows}}),
                                    {status: 200, headers: {'Content-Type': 'application/json'}}
                                ));
                            });
                        }
                        if (parsed.pathname === '/api/config-explorer/packages/all') {
                            return jsonResponse({success: true, data: {packages: ['android']}});
                        }
                        if (parsed.pathname === '/api/config-explorer') {
                            window.__dcfgConfigUrls.push(parsed.href);
                            return jsonResponse({
                                success: true,
                                data: {
                                    package: 'android', total: 1, overlayed_count: 0,
                                    resources: [{
                                        name: 'bool/config_test', type: 'bool',
                                        default_value: 'false', effective_value: null,
                                        overlay_changed: null, overlay_source: null,
                                        lookup_error: null
                                    }]
                                }
                            });
                        }
                        return window.__dcfgOriginalFetch(input, init);
                    };
                    allDevices = [
                        {device_id: 'DEVICE-A', serial_no: 'DEVICE-A', worker_id: 'ats-worker-controller'},
                        {device_id: 'DEVICE-B', serial_no: 'DEVICE-B', worker_id: 'ats-worker-controller'}
                    ];
                    openDeviceConfigExplorer('DEVICE-A', 'ats-worker-controller');
                    dcfgSwitchTab('props');
                }
                """
            )
            page.wait_for_function("Boolean(window.__dcfgPendingProps['DEVICE-A'])")
            page.evaluate(
                """
                () => {
                    openDeviceConfigExplorer('DEVICE-B', 'ats-worker-controller');
                    dcfgSwitchTab('props');
                }
                """
            )
            page.wait_for_function("Boolean(window.__dcfgPendingProps['DEVICE-B'])")
            page.evaluate(
                "window.__dcfgPendingProps['DEVICE-B']([{name:'ro.product.model',value:'MODEL-B'}])"
            )
            expect(page.locator("#dcfg-prop-results")).to_contain_text("MODEL-B")
            page.evaluate(
                "window.__dcfgPendingProps['DEVICE-A']([{name:'ro.product.model',value:'MODEL-A'}])"
            )
            page.wait_for_timeout(150)

            expect(page.locator("#device-config-serial")).to_have_text("DEVICE-B")
            expect(page.locator("#dcfg-prop-results")).to_contain_text("MODEL-B")
            expect(page.locator("#dcfg-prop-results")).not_to_contain_text("MODEL-A")
            expect(page.locator("#dcfg-effective")).not_to_be_checked()
            self.assertTrue(
                page.evaluate(
                    """
                    window.__dcfgConfigUrls.length >= 2
                    && window.__dcfgConfigUrls.every(url =>
                        new URL(url).searchParams.get('with_effective') === 'false')
                    """
                )
            )
            self.assert_no_page_errors(page_errors)
        finally:
            page.evaluate(
                """
                () => {
                    if (typeof closeDeviceConfigExplorer === 'function') closeDeviceConfigExplorer();
                    if (window.__dcfgOriginalFetch) window.fetch = window.__dcfgOriginalFetch;
                }
                """
            )
            page.close()

    def test_device_actions_use_adb_transport_id_not_display_serial(self):
        page = self.new_page()
        try:
            self.goto_shell(page)
            result = page.evaluate(
                """
                () => {
                    allDevices = [{
                        device_id: '192.0.2.50:5555',
                        serial_no: 'HARDWARE-SERIAL',
                        worker_id: 'ats-worker-controller',
                        source_type: 'local',
                        source_host: 'controller',
                        status: 'online',
                        protocol: 'adb',
                    }];
                    devicesManagementClusterMode = false;
                    state.deviceGroups = [];
                    displayDevicesManagement(allDevices);
                    const row = document.querySelector('#devices-table-body tr');
                    const buttons = [...row.querySelectorAll('button[data-serial]')];
                    return {
                        display: row.cells[0].textContent.trim(),
                        actionSerials: buttons.map(button => button.dataset.serial),
                    };
                }
                """
            )

            self.assertEqual(result["display"], "HARDWARE-SERIAL")
            self.assertEqual(
                result["actionSerials"],
                ["192.0.2.50:5555", "192.0.2.50:5555", "192.0.2.50:5555"],
            )
        finally:
            page.close()

    def test_device_action_buttons_reflect_local_claim_ownership(self):
        page = self.new_page()
        try:
            self.goto_shell(page)
            result = page.evaluate(
                """
                () => {
                    const base = {
                        worker_id: 'ats-worker-controller', source_type: 'local',
                        source_host: 'controller', status: 'online', protocol: 'adb'
                    };
                    allDevices = [
                        {...base, device_id: 'OWNED', serial_no: 'OWNED',
                            locked_by: 'ui-admin', locked_by_self: true},
                        {...base, device_id: 'OTHER', serial_no: 'OTHER',
                            locked_by: 'occupied', locked_by_self: false},
                    ];
                    devicesManagementClusterMode = false;
                    state.deviceGroups = [];
                    displayDevicesManagement(allDevices);
                    return Object.fromEntries(
                        [...document.querySelectorAll('#devices-table-body tr')].map(row => {
                            const serial = row.cells[0]?.textContent.trim();
                            const controls = [...row.querySelectorAll('button[data-serial]')]
                                .filter(button => !button.textContent.includes('释放'));
                            return [serial, controls.map(button => button.disabled)];
                        }).filter(([serial]) => serial)
                    );
                }
                """
            )

            # ADB Shell always needs an exclusive claim. Read-only Device Info
            # and UI inspection remain available to the current claim owner.
            self.assertEqual(result["OWNED"], [True, False, False])
            self.assertEqual(result["OTHER"], [True, True, True])
        finally:
            page.close()

    def test_manual_device_refresh_reports_busy_then_restores_control(self):
        page = self.new_page()
        try:
            self.goto_shell(page)
            page.evaluate(
                """
                () => {
                    window.__originalLoadDevicesForRefresh = loadDevices;
                    loadDevices = () => new Promise(resolve => {
                        window.__resolveManualDeviceRefresh = resolve;
                    });
                    document.getElementById('refresh-devices-btn').click();
                }
                """
            )
            button = page.locator("#refresh-devices-btn")
            expect(button).to_be_disabled()
            expect(button).to_have_text("刷新中…")
            self.assertEqual(button.get_attribute("aria-busy"), "true")

            page.evaluate("window.__resolveManualDeviceRefresh([])")
            expect(button).to_be_enabled()
            expect(button).to_have_text("↻ 刷新设备")
            self.assertIsNone(button.get_attribute("aria-busy"))
        finally:
            page.evaluate(
                """
                () => {
                    if (window.__originalLoadDevicesForRefresh) {
                        loadDevices = window.__originalLoadDevicesForRefresh;
                    }
                }
                """
            )
            page.close()

    def test_manual_suite_refresh_forces_reload_and_restores_control(self):
        page = self.new_page()
        try:
            self.goto_shell(page)
            page.evaluate(
                """
                () => {
                    window.__originalLoadTestSuitesForRefresh = loadTestSuites;
                    window.__suiteRefreshForce = null;
                    loadTestSuites = force => new Promise(resolve => {
                        window.__suiteRefreshForce = force;
                        window.__resolveManualSuiteRefresh = resolve;
                    });
                    document.getElementById('refresh-suites-btn').click();
                }
                """
            )
            button = page.locator("#refresh-suites-btn")
            expect(button).to_be_disabled()
            expect(button).to_have_text("刷新中…")
            self.assertEqual(button.get_attribute("aria-busy"), "true")
            self.assertTrue(page.evaluate("window.__suiteRefreshForce"))

            page.evaluate("window.__resolveManualSuiteRefresh([])")
            expect(button).to_be_enabled()
            expect(button).to_have_text("↻ 刷新套件")
            self.assertIsNone(button.get_attribute("aria-busy"))
        finally:
            page.evaluate(
                """
                () => {
                    if (window.__originalLoadTestSuitesForRefresh) {
                        loadTestSuites = window.__originalLoadTestSuitesForRefresh;
                    }
                }
                """
            )
            page.close()

    def test_device_management_scope_switch_does_not_publish_stale_inventory(self):
        page = self.new_page()
        try:
            self.goto_shell(page)
            result = page.evaluate(
                """
                async () => {
                    const originalFetch = window.fetch.bind(window);
                    let resolveManagement;
                    const jsonResponse = payload => new Response(JSON.stringify(payload), {
                        status: 200,
                        headers: {'Content-Type': 'application/json'}
                    });
                    window.fetch = (input, options) => {
                        const path = new URL(String(input), location.origin).pathname;
                        if (path === '/api/devices/management') {
                            return new Promise(resolve => { resolveManagement = resolve; });
                        }
                        if (path === '/api/cluster/devices') {
                            return Promise.resolve(jsonResponse({
                                success: true,
                                devices: [{
                                    id: 'worker-a:STALE-REMOTE', serial: 'STALE-REMOTE',
                                    worker_id: 'worker-a', state: 'available', properties: {}
                                }]
                            }));
                        }
                        if (path === '/api/cluster/hosts') {
                            return Promise.resolve(jsonResponse({success: true, hosts: [{
                                worker_id: 'worker-a', status: 'online',
                                address: '192.0.2.10', ssh_user: 'tester',
                                capabilities: {device_inspection: true}
                            }]}));
                        }
                        if (path === '/api/cluster/status') {
                            return Promise.resolve(jsonResponse({
                                success: true, enabled: true,
                                local_worker_id: 'ats-worker-controller'
                            }));
                        }
                        return originalFetch(input, options);
                    };
                    state.clusterStatus = {
                        enabled: true, local_worker_id: 'ats-worker-controller'
                    };
                    GmsWorkspace.update({scope_mode: 'cluster', worker_id: 'worker-a'});
                    allDevices = [{
                        device_id: 'CURRENT-SINGLE', serial_no: 'CURRENT-SINGLE',
                        worker_id: 'ats-worker-controller', status: 'online'
                    }];
                    devicesManagementClusterMode = false;
                    const staleLoad = loadDevicesManagementOnce('cluster');
                    while (!resolveManagement) {
                        await new Promise(resolve => setTimeout(resolve, 0));
                    }
                    GmsWorkspace.update({
                        scope_mode: 'single', worker_id: 'ats-worker-controller'
                    });
                    resolveManagement(jsonResponse({
                        success: true,
                        devices: [{
                            device_id: 'STALE-LOCAL', serial_no: 'STALE-LOCAL',
                            status: 'online', source_type: 'local'
                        }]
                    }));
                    await staleLoad;
                    window.fetch = originalFetch;
                    return {
                        ids: allDevices.map(device => device.device_id),
                        clusterMode: devicesManagementClusterMode,
                        scope: GmsWorkspace.get().scope_mode,
                    };
                }
                """
            )

            self.assertEqual(result["scope"], "single")
            self.assertEqual(result["ids"], ["CURRENT-SINGLE"])
            self.assertFalse(result["clusterMode"])
        finally:
            page.close()

    def test_device_management_inventory_follows_single_and_cluster_mode(self):
        page = self.new_page()
        cluster_device_requests = []

        def json_response(route, payload):
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(payload),
            )

        page.route(
            "**/api/devices/management",
            lambda route: json_response(route, {
                "success": True,
                "devices": [{
                    "device_id": "LOCAL-1",
                    "serial_no": "LOCAL-1",
                    "source_type": "local",
                    "source_host": "ui-admin@127.0.0.1",
                    "status": "online",
                    "protocol": "adb",
                }, {
                    "device_id": "LOCAL-FB",
                    "serial_no": "LOCAL-FB",
                    "source_type": "local",
                    "source_host": "ui-admin@127.0.0.1",
                    "status": "fastboot",
                    "protocol": "fastboot",
                }, {
                    "device_id": "REMOTE-PROXY",
                    "serial_no": "REMOTE-PROXY",
                    "source_type": "adb_proxy",
                    "source_host": "worker-1 → ats-worker-controller",
                    "transport": "adb_proxy",
                    "adb_proxy_source_worker_id": "worker-1",
                    "adb_proxy_source_serial": "REMOTE-PROXY",
                    "status": "online",
                    "protocol": "adb",
                }],
            }),
        )
        page.route(
            "**/api/cluster/status",
            lambda route: json_response(route, {
                "success": True,
                "enabled": True,
                "local_worker_id": "ats-worker-controller",
            }),
        )

        def cluster_devices(route):
            cluster_device_requests.append(route.request.url)
            json_response(route, {
                "success": True,
                "devices": [{
                    "id": "worker-1:REMOTE-1",
                    "serial": "REMOTE-1",
                    "worker_id": "worker-1",
                    "state": "available",
                    "properties": {"model": "Remote Model"},
                }, {
                    "id": "worker-1:REMOTE-PROXY",
                    "serial": "REMOTE-PROXY",
                    "worker_id": "worker-1",
                    "state": "available",
                    "properties": {"model": "Proxy Source"},
                }, {
                    "id": "worker-1:REMOTE-OFFLINE",
                    "serial": "REMOTE-OFFLINE",
                    "worker_id": "worker-1",
                    "state": "offline",
                    "properties": {"model": "Offline Model"},
                }, {
                    "id": "worker-1:REMOTE-UNKNOWN",
                    "serial": "REMOTE-UNKNOWN",
                    "worker_id": "worker-1",
                    "state": "unknown",
                    "properties": {"model": "Unknown Model"},
                }],
            })

        page.route("**/api/cluster/devices", cluster_devices)
        page.route(
            "**/api/cluster/hosts",
            lambda route: json_response(route, {
                "success": True,
                "hosts": [{
                    "worker_id": "worker-1",
                    "name": "Worker 1",
                    "status": "online",
                    "address": "192.0.2.10",
                    "ssh_user": "tester",
                    "capabilities": {"device_inspection": True},
                }],
            }),
        )
        page.route(
            "**/api/device-groups",
            lambda route: json_response(route, {
                "success": True,
                "data": {"groups": [
                    {
                        "id": "local-group",
                        "name": "Local Group",
                        "color": "#00aa00",
                        "device_ids": ["LOCAL-1"],
                    },
                    {
                        "id": "remote-group",
                        "name": "Remote Group",
                        "color": "#0000aa",
                        "device_ids": ["worker-1:REMOTE-1"],
                    },
                ]},
            }),
        )

        try:
            self.goto_shell(page)
            page.wait_for_function("state.clusterStatus?.enabled === true")
            page.evaluate(
                "GmsWorkspace.update({scope_mode: 'single', worker_id: 'ats-worker-controller'})"
            )
            page.evaluate("switchPage('devices')")
            expect(page.locator("#devices-table-body")).to_contain_text("LOCAL-1")
            expect(page.locator("#devices-table-body")).to_contain_text("LOCAL-FB")
            expect(page.locator("#devices-table-body")).to_contain_text("REMOTE-PROXY")
            expect(page.locator("#devices-table-body")).to_contain_text("ADB Proxy")
            expect(page.locator("#devices-table-body")).to_contain_text("Fastboot")
            expect(page.locator("#devices-table-body")).not_to_contain_text("REMOTE-1")
            expect(page.locator("#devices-table-body")).to_contain_text("Local Group")
            expect(page.locator("#devices-table-body")).not_to_contain_text("Remote Group")
            expect(page.locator("#cluster-devices-count").locator("..")).to_be_hidden()
            expect(page.locator("#fastboot-devices-count")).to_have_text("1")
            expect(page.locator("#local-devices-count")).to_have_text("3")
            fastboot_row = page.locator("#devices-table-body tr", has_text="LOCAL-FB")
            expect(fastboot_row.get_by_role("button", name="🐧 adb shell")).to_be_disabled()
            self.assertEqual(cluster_device_requests, [])

            page.evaluate(
                "GmsWorkspace.update({scope_mode: 'cluster', worker_id: 'worker-1'})"
            )
            expect(page.locator("#devices-table-body")).to_contain_text("REMOTE-1")
            expect(
                page.locator("#devices-table-body tr", has_text="REMOTE-PROXY")
            ).to_have_count(2)
            expect(page.locator("#devices-table-body")).not_to_contain_text(
                "REMOTE-OFFLINE"
            )
            expect(page.locator("#devices-table-body")).not_to_contain_text(
                "REMOTE-UNKNOWN"
            )
            expect(page.locator("#devices-table-body")).not_to_contain_text("Remote Group")
            expect(
                page.locator(
                    "#devices-table-body tr.device-group-row",
                    has_text="worker-1",
                )
            ).to_contain_text("worker-1")
            expect(page.locator("#cluster-devices-count").locator("..")).to_be_visible()
            expect(page.locator("#cluster-devices-count")).to_have_text("2")
            expect(page.locator("#local-devices-count")).to_have_text("3")
            self.assertGreaterEqual(len(cluster_device_requests), 1)
        finally:
            page.evaluate(
                "GmsWorkspace.update({scope_mode: 'single', worker_id: 'ats-worker-controller'})"
            )
            page.wait_for_timeout(200)
            page.close()

    def test_suite_share_link_keeps_path_slashes_readable(self):
        page = self.new_page()
        try:
            self.goto_shell(page)
            result = page.evaluate(
                """() => {
                    state.suiteBrowser.selectedSuitePath =
                        '/home/hcq/GMS Suite/android-cts-17_r1/android-cts/tools';
                    const link = buildSuiteBrowserLink(
                        'testcases/CtsKeystore&Tests/arm64/Cts#Keystore.apk',
                        'file'
                    );
                    const previousUrl = window.location.href;
                    window.history.replaceState(null, '', link);
                    const parsed = getSuiteBrowserRouteParams();
                    window.history.replaceState(null, '', previousUrl);
                    return {link, parsed};
                }"""
            )
            link = result["link"]

            self.assertNotRegex(link, re.compile(r"%2f", re.IGNORECASE))
            self.assertIn(
                "#test-suites?suite_path=/home/hcq/GMS+Suite/"
                "android-cts-17_r1/android-cts/tools",
                link,
            )
            self.assertIn(
                "&file=testcases/CtsKeystore%26Tests/arm64/Cts%23Keystore.apk",
                link,
            )
            self.assertIn("&worker_id=ats-worker-controller", link)
            self.assertEqual(
                result["parsed"]["suitePath"],
                "/home/hcq/GMS Suite/android-cts-17_r1/android-cts/tools",
            )
            self.assertEqual(
                result["parsed"]["filePath"],
                "testcases/CtsKeystore&Tests/arm64/Cts#Keystore.apk",
            )
            self.assertEqual(result["parsed"]["workerId"], "ats-worker-controller")
        finally:
            page.close()

    def test_local_suite_share_link_switches_from_saved_remote_worker_before_load(self):
        page = self.new_page()
        try:
            self.goto_shell(page)
            result = page.evaluate(
                """async () => {
                    const localWorker = 'ats-worker-controller';
                    const remoteWorker = 'ats-worker-246';
                    const suitePath =
                        '/home/hcq/GMS-Suite/android-cts-17_r1/android-cts/tools';
                    const select = document.getElementById('suite-worker-select');
                    const loadedWorkers = [];
                    let selected = null;

                    window.loadSuiteWorkerSelector = async () => {
                        select.innerHTML = `
                            <option value="${localWorker}">ATS Controller Local Worker</option>
                            <option value="${remoteWorker}">ats-worker-246</option>`;
                        select.value = remoteWorker;
                        select.dataset.loaded = '1';
                        select.disabled = false;
                    };
                    window.loadSuitesForBrowserWorker = async () => {
                        loadedWorkers.push(select.value);
                        testSuitesWorkerId = select.value;
                        testSuitesCache = select.value === localWorker
                            ? [{
                                tools_path: suitePath,
                                test_type: 'cts',
                                version: '17_r1',
                                suite_key: suitePath,
                                worker_id: localWorker,
                            }]
                            : [{
                                tools_path: '/remote/other-suite/tools',
                                test_type: 'cts',
                                version: 'remote',
                                suite_key: 'remote',
                                worker_id: remoteWorker,
                            }];
                        return testSuitesCache;
                    };
                    window.renderTestSuiteBrowserList = () => {};
                    window.selectTestSuiteForBrowser = async (path, directory) => {
                        selected = {
                            path,
                            directory,
                            exists: testSuitesCache.some(suite => suite.tools_path === path),
                        };
                    };

                    window.history.replaceState(
                        null,
                        '',
                        `#test-suites?suite_path=${suitePath}` +
                            `&path=results/2026.07.02_21.27.07.425_5532`
                    );
                    await initTestSuiteBrowserPage();
                    return {
                        selectedWorker: select.value,
                        loadedWorkers,
                        selected,
                    };
                }"""
            )

            self.assertEqual(result["selectedWorker"], "ats-worker-controller")
            self.assertEqual(result["loadedWorkers"], ["ats-worker-controller"])
            self.assertTrue(result["selected"]["exists"])
            self.assertEqual(
                result["selected"]["directory"],
                "results/2026.07.02_21.27.07.425_5532",
            )
        finally:
            page.close()

    def test_skill_install_action_uses_current_controller_and_keeps_zip_offline(self):
        page = self.new_page()
        try:
            self.goto_shell(page)
            result = page.evaluate(
                """() => {
                    const install = buildSkillInstallCommand();
                    const installApi = generateCurlCommand({
                        method: 'GET',
                        path: '/api/system/skills/install.sh',
                    }, {});
                    const archiveApi = generateCurlCommand({
                        method: 'GET',
                        path: '/api/system/skills',
                    }, {params: []});
                    return {
                        install,
                        installApi,
                        archiveApi,
                        primaryLabel: Array.from(document.querySelectorAll('button'))
                            .find(button => button.textContent.includes('安装/更新命令'))
                            ?.textContent.trim(),
                        offlineLabel: Array.from(document.querySelectorAll('button'))
                            .find(button => button.textContent.includes('离线包'))
                            ?.textContent.trim(),
                    };
                }"""
            )

            expected = (
                f'curl -fsSL "{self.base_url}/api/system/skills/install.sh" | bash'
            )
            self.assertEqual(result["install"], expected)
            self.assertEqual(result["installApi"]["full"], expected)
            self.assertIn("-OJ", result["archiveApi"]["full"])
            self.assertNotIn("| bash", result["archiveApi"]["full"])
            self.assertEqual(result["primaryLabel"], "📋 安装/更新命令")
            self.assertEqual(result["offlineLabel"], "📦 离线包")
        finally:
            page.close()

    def test_test_host_stays_disabled_until_initial_worker_list_is_ready(self):
        page = self.new_page()

        def json_response(route, payload):
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(payload),
            )

        page.route(
            "**/api/cluster/status",
            lambda route: json_response(route, {
                "success": True,
                "enabled": True,
                "local_worker_id": "ats-worker-controller",
            }),
        )
        page.route(
            "**/api/cluster/workers",
            lambda route: json_response(route, {
                "success": True,
                "workers": [{
                    "id": "ats-worker-controller",
                    "name": "ATS Controller Local Worker",
                    "hostname": "ats-041055-64g",
                    "status": "online",
                }],
            }),
        )
        page.add_init_script(
            """
            const nativeFetch = window.fetch.bind(window);
            window.fetch = (input, options = {}) => {
                if (String(input).includes('/api/cluster/workers')) {
                    return new Promise((resolve, reject) => setTimeout(
                        () => nativeFetch(input, options).then(resolve, reject),
                        1500
                    ));
                }
                return nativeFetch(input, options);
            };
            """
        )

        try:
            page.goto(self.base_url, wait_until="domcontentloaded")
            page.wait_for_selector("#cluster-worker")
            page.wait_for_function(
                "window.GmsWorkspace && window.state?.clusterStatus?.enabled === true"
            )
            loading = page.evaluate(
                """() => {
                    GmsWorkspace.update(
                        {scope_mode: 'cluster', worker_id: 'ats-worker-controller'},
                        {source: 'timing-test', persist: false}
                    );
                    const select = document.getElementById('cluster-worker');
                    return {
                        disabled: select.disabled,
                        busy: select.getAttribute('aria-busy'),
                        label: select.selectedOptions[0]?.textContent,
                        title: select.title,
                    };
                }"""
            )
            self.assertTrue(loading["disabled"])
            self.assertEqual(loading["busy"], "true")
            self.assertEqual(loading["label"], "加载中...")
            self.assertEqual(loading["title"], "正在加载测试主机列表")

            page.wait_for_function(
                """() => {
                    const select = document.getElementById('cluster-worker');
                    return select.dataset.workersLoaded === 'true' && !select.disabled;
                }"""
            )
            ready = page.locator("#cluster-worker").evaluate(
                """select => ({
                    disabled: select.disabled,
                    busy: select.getAttribute('aria-busy'),
                    label: select.selectedOptions[0]?.textContent,
                })"""
            )
            self.assertFalse(ready["disabled"])
            self.assertEqual(ready["busy"], "false")
            self.assertEqual(ready["label"], "ats-worker-controller")
        finally:
            page.close()

    def test_test_host_refresh_stays_visible_and_preserves_unchanged_selection(self):
        page = self.new_page()

        def json_response(route, payload):
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(payload),
            )

        workers = {
            "success": True,
            "workers": [{
                "id": "ats-worker-controller",
                "name": "ATS Controller Local Worker",
                "hostname": "ats-041055-64g",
                "status": "online",
            }],
        }
        page.route(
            "**/api/cluster/status",
            lambda route: json_response(route, {
                "success": True,
                "enabled": True,
                "local_worker_id": "ats-worker-controller",
            }),
        )
        page.route(
            "**/api/cluster/workers",
            lambda route: json_response(route, workers),
        )

        try:
            self.goto_shell(page)
            page.wait_for_function(
                """() => document.querySelector(
                    '#cluster-worker option[value="ats-worker-controller"]'
                )?.textContent === 'ats-worker-controller'"""
            )
            result = page.evaluate(
                """async workers => {
                    const select = document.getElementById('cluster-worker');
                    const selectedOption = select.selectedOptions[0];
                    const originalFetch = window.fetch.bind(window);
                    window.fetch = (input, options = {}) => {
                        if (String(input).includes('/api/cluster/workers')) {
                            return new Promise(resolve => setTimeout(() => resolve(
                                new Response(JSON.stringify(workers), {
                                    status: 200,
                                    headers: {'Content-Type': 'application/json'}
                                })
                            ), 120));
                        }
                        return originalFetch(input, options);
                    };

                    const refresh = loadClusterWorkers();
                    await new Promise(resolve => setTimeout(resolve, 30));
                    const during = {
                        value: select.value,
                        label: select.selectedOptions[0]?.textContent,
                        visibility: getComputedStyle(select).visibility,
                    };
                    await refresh;
                    return {
                        during,
                        afterValue: select.value,
                        afterLabel: select.selectedOptions[0]?.textContent,
                        sameOptionNode: select.selectedOptions[0] === selectedOption,
                    };
                }""",
                workers,
            )

            expected_label = "ats-worker-controller"
            self.assertEqual(result["during"]["visibility"], "visible")
            self.assertEqual(result["during"]["value"], "ats-worker-controller")
            self.assertEqual(result["during"]["label"], expected_label)
            self.assertEqual(result["afterValue"], "ats-worker-controller")
            self.assertEqual(result["afterLabel"], expected_label)
            self.assertTrue(result["sameOptionNode"])
        finally:
            page.close()

    def test_rapid_test_host_switch_keeps_latest_context_and_devices(self):
        page = self.new_page()

        def json_response(route, payload):
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(payload),
            )

        page.route(
            "**/api/cluster/status",
            lambda route: json_response(route, {
                "success": True,
                "enabled": True,
                "local_worker_id": "ats-worker-controller",
            }),
        )
        page.route(
            "**/api/cluster/workers",
            lambda route: json_response(route, {
                "success": True,
                "workers": [
                    {"id": "ats-worker-controller", "name": "Local", "status": "online"},
                    {"id": "worker-a", "name": "Worker A", "status": "online"},
                    {"id": "worker-b", "name": "Worker B", "status": "online"},
                ],
            }),
        )
        page.route(
            "**/api/devices/list*",
            lambda route: json_response(route, []),
        )
        page.route(
            "**/api/test/suites*",
            lambda route: json_response(route, {"success": True, "suites": []}),
        )

        try:
            self.goto_shell(page)
            page.wait_for_function("window.GmsWorkspace && window.state")
            result = page.evaluate(
                """async () => {
                    const originalFetch = window.fetch.bind(window);
                    const jsonResponse = value => new Response(JSON.stringify(value), {
                        status: 200,
                        headers: {'Content-Type': 'application/json'}
                    });
                    window.fetch = (input, options = {}) => {
                        const url = String(input);
                        if (url.includes('/api/cluster/devices?worker_id=')) {
                            const worker = new URL(url, location.origin).searchParams.get('worker_id');
                            const delay = worker === 'worker-a' ? 250 : 20;
                            const payload = {success: true, devices: [{
                                id: `${worker}:DEVICE-${worker.slice(-1).toUpperCase()}`,
                                serial: `DEVICE-${worker.slice(-1).toUpperCase()}`,
                                worker_id: worker,
                                state: 'available',
                                properties: {model: worker}
                            }]};
                            return new Promise(resolve => setTimeout(
                                () => resolve(jsonResponse(payload)), delay
                            ));
                        }
                        if (url.includes('/api/cluster/suites?worker_id=')) {
                            return Promise.resolve(jsonResponse({success: true, suites: []}));
                        }
                        if (url.endsWith('/api/users/workspace-context')
                                && String(options.method || '').toUpperCase() === 'PATCH') {
                            const body = JSON.parse(options.body || '{}');
                            const delay = body.worker_id === 'worker-a' ? 250 : 20;
                            return new Promise(resolve => setTimeout(
                                () => resolve(jsonResponse({
                                    success: true, data: {context: body}
                                })), delay
                            ));
                        }
                        return originalFetch(input, options);
                    };

                    state.clusterStatus = {enabled: true, local_worker_id: 'ats-worker-controller'};
                    const select = document.getElementById('cluster-worker');
                    select.innerHTML = '<option value="worker-a">A</option><option value="worker-b">B</option>';

                    // Start persisting A, then select B while A is in flight.
                    GmsWorkspace.update({scope_mode: 'cluster', worker_id: 'worker-a'});
                    await new Promise(resolve => setTimeout(resolve, 150));
                    GmsWorkspace.update({scope_mode: 'cluster', worker_id: 'worker-b'});
                    await new Promise(resolve => setTimeout(resolve, 550));

                    select.value = 'worker-a';
                    const first = switchTestWorker();
                    await new Promise(resolve => setTimeout(resolve, 25));
                    select.value = 'worker-b';
                    await switchTestWorker();
                    await first;
                    return {
                        contextWorker: GmsWorkspace.get().worker_id,
                        selectedWorker: select.value,
                        deviceIds: state.devices.map(device => device.device_id),
                        deviceWorkers: state.devices.map(device => device.cluster_worker_id),
                    };
                }"""
            )

            self.assertEqual(result["contextWorker"], "worker-b")
            self.assertEqual(result["selectedWorker"], "worker-b")
            self.assertEqual(result["deviceIds"], ["worker-b:DEVICE-B"])
            self.assertEqual(result["deviceWorkers"], ["worker-b"])
        finally:
            page.close()

    def test_report_diagnosis_labels_rule_fallback_with_ai_error(self):
        page = self.new_page()
        try:
            self.goto_shell(page)
            page.evaluate(
                """() => {
                    window.currentReportAnalysisData = {
                        report_name: 'mock-report',
                        failures: [{name: 'Example#testFailure', module: 'MockModule'}],
                        details: {test_type: 'CTS'}
                    };
                    renderReportDiagnosis({
                        test_name: 'Example#testFailure',
                        module: 'MockModule',
                        failure_index: 0,
                        ai_result: {
                            ai_enabled: false,
                            ai_attempted: true,
                            ai_error: 'glm_local quota exceeded',
                            root_cause: '待验证：规则判断',
                            root_cause_status: 'hypothesis',
                            root_cause_confidence: 'low',
                            observed_failure: 'AssertionError: expected true',
                            root_cause_note: '当前直接证据只证明断言失败。',
                            analysis: '规则分析内容',
                            suggestions: []
                        },
                        suite_target: {},
                        source_search_results: [],
                        knowledge_base_results: []
                    });
                }"""
            )
            expect(page.locator("#report-diagnostic-summary")).to_contain_text(
                "规则分析（AI 不可用）"
            )
            expect(page.locator("#report-diagnostic-result")).to_contain_text(
                "本地 AI 未完成分析"
            )
            expect(page.locator("#report-diagnostic-result")).to_contain_text(
                "glm_local quota exceeded"
            )
            expect(page.locator("#report-diagnostic-result")).to_contain_text(
                "已观察到的失败"
            )
            expect(page.locator("#report-diagnostic-result")).to_contain_text(
                "初步判断"
            )
            expect(page.locator("#report-diagnostic-result")).to_contain_text(
                "Hypothesis · 低置信度"
            )
            expect(page.locator("#report-diagnostic-result")).not_to_contain_text(
                "Root cause"
            )
            page.evaluate(
                """() => renderReportDiagnosis({
                    test_name: 'Example#testFailure',
                    module: 'MockModule',
                    failure_index: 0,
                    ai_result: {
                        ai_enabled: true,
                        ai_model: 'Backup AI',
                        ai_provider: 'zhipu',
                        ai_fallback_used: true,
                        ai_provider_errors: ['glm_local：本地模型额度已用尽。'],
                        root_cause: '待验证：备用模型结论',
                        root_cause_status: 'hypothesis',
                        root_cause_confidence: 'low',
                        observed_failure: 'AssertionError: expected true',
                        analysis: '备用模型分析',
                        suggestions: []
                    },
                    suite_target: {},
                    source_search_results: [],
                    knowledge_base_results: []
                })"""
            )
            expect(page.locator("#report-diagnostic-summary")).to_contain_text(
                "Backup AI（备用）"
            )
            expect(page.locator("#report-diagnostic-result")).to_contain_text(
                "本次已由 Backup AI 完成"
            )
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

    def test_sidebar_visibility_options_each_have_an_explanation(self):
        page = self.new_page()
        try:
            self.goto_shell(page)
            page.locator(".sidebar-brand").click()
            options = page.locator("#sidebar-visibility-list .sidebar-visibility-option")
            descriptions = page.locator("#sidebar-visibility-list .sidebar-description")
            self.assertGreater(options.count(), 0)
            self.assertEqual(descriptions.count(), options.count())
            for index in range(descriptions.count()):
                self.assertTrue(descriptions.nth(index).inner_text().strip())
                self.assertEqual(
                    descriptions.nth(index).evaluate(
                        "element => getComputedStyle(element).whiteSpace"
                    ),
                    "nowrap",
                )
            first_icon = options.first.locator(".sidebar-icon").bounding_box()
            first_title = options.first.locator(".sidebar-text").bounding_box()
            first_description = descriptions.first.bounding_box()
            self.assertAlmostEqual(first_icon["y"], first_title["y"], delta=4)
            self.assertAlmostEqual(first_title["y"], first_description["y"], delta=5)
        finally:
            page.close()

    def test_single_mode_simplifies_host_workspaces_and_report_header_is_stable(self):
        page = self.new_page()
        novnc_access_requests = []

        def grant_novnc_access(route):
            novnc_access_requests.append(route.request.url)
            route.fulfill(
                status=200,
                content_type="application/json",
                body='{"success":true,"url":"about:blank"}',
            )

        page.route(
            "**/api/desktop/vnc/status",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body='{"success":true,"running":true}',
            ),
        )
        page.route(
            "**/api/desktop/novnc/access",
            grant_novnc_access,
        )
        try:
            elevated = page.request.post(
                f"{self.base_url}/api/auth/elevate",
                data={
                    "username": "ui-admin",
                    "password": "UiSmokeAdmin-2026!",
                },
            )
            self.assertTrue(elevated.ok, elevated.text())
            self.goto_shell(page)
            page.evaluate("state.elevated = true; applyClusterMode(false)")
            page.evaluate("() => { switchPage('desktop', null); return true; }")
            expect(page.locator("#host-workspace-grid .host-workspace-pane")).to_have_count(1)
            expect(page.locator("#host-workspace-grid iframe")).to_have_count(1)
            expect(page.locator("[data-workspace-layout]").first).to_be_hidden()
            expect(page.locator("#host-workspace-grid [data-multi-host-control]").first).to_be_hidden()

            page.evaluate("() => { switchPage('reports', null); return true; }")
            page.evaluate("() => { switchPage('desktop', null); return true; }")
            expect(page.locator("#host-workspace-grid iframe")).to_have_count(1)
            page.wait_for_timeout(500)
            self.assertEqual(len(novnc_access_requests), 1)

            page.evaluate("() => { switchPage('reports', null); return true; }")
            checkbox = page.locator("#filter-user-checkbox")
            before = checkbox.bounding_box()
            page.evaluate("applyClusterMode(true)")
            after_cluster = checkbox.bounding_box()
            page.evaluate("applyClusterMode(false)")
            after_single = checkbox.bounding_box()
            self.assertIsNotNone(before)
            self.assertAlmostEqual(before["x"], after_cluster["x"], delta=1)
            self.assertAlmostEqual(before["x"], after_single["x"], delta=1)
        finally:
            page.close()

    def test_local_desktop_mount_does_not_wait_for_cluster_host_directory(self):
        page = self.new_page()
        page.route(
            "**/api/desktop/vnc/status",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body='{"success":true,"running":true}',
            ),
        )
        page.route(
            "**/api/desktop/novnc/access",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body='{"success":true,"url":"about:blank"}',
            ),
        )
        page.add_init_script(
            """
            window.__desktopHostDirectoryRequested = false;
            window.__desktopHostDirectoryResolved = false;
            const nativeFetch = window.fetch.bind(window);
            window.fetch = (input, options = {}) => {
              const request = nativeFetch(input, options);
              if (!String(input).includes('/api/cluster/hosts')) return request;
              window.__desktopHostDirectoryRequested = true;
              return new Promise((resolve, reject) => setTimeout(() => {
                request.then(response => {
                  window.__desktopHostDirectoryResolved = true;
                  resolve(response);
                }, reject);
              }, 2000));
            };
            """
        )
        try:
            elevated = page.request.post(
                f"{self.base_url}/api/auth/elevate",
                data={"username": "ui-admin", "password": "UiSmokeAdmin-2026!"},
            )
            self.assertTrue(elevated.ok, elevated.text())
            self.goto_shell(page)
            page.evaluate(
                """() => {
                  state.elevated = true;
                  state.elevatedUntil = Date.now() + 60000;
                  switchPage('desktop', null);
                }"""
            )
            page.wait_for_function("window.__desktopHostDirectoryRequested === true")
            expect(page.locator("#host-workspace-grid iframe")).to_have_count(1)
            self.assertFalse(page.evaluate("window.__desktopHostDirectoryResolved"))
            page.wait_for_function("window.__desktopHostDirectoryResolved === true")

            parallel_requests = page.evaluate(
                """async () => {
                  const originalApiCall = apiCall;
                  const originalAccess = requestNovncAccess;
                  const events = [];
                  try {
                    apiCall = async path => {
                      events.push('status-start');
                      await new Promise(resolve => setTimeout(resolve, 40));
                      events.push('status-end');
                      return {success: true, running: true};
                    };
                    requestNovncAccess = async () => {
                      events.push('access-start');
                      await new Promise(resolve => setTimeout(resolve, 40));
                      events.push('access-end');
                      return 'about:blank';
                    };
                    await resolveWorkspaceVncUrl({
                      id: 'default', worker_id: workspaceLocalWorkerId()
                    });
                    return events;
                  } finally {
                    apiCall = originalApiCall;
                    requestNovncAccess = originalAccess;
                  }
                }"""
            )
            self.assertEqual(parallel_requests[:2], ["status-start", "access-start"])

            page.evaluate(
                "window.__retainedDesktopFrame = document.querySelector('#host-workspace-grid iframe')"
            )
            page.evaluate("switchPage('reports', null)")
            page.evaluate("switchPage('desktop', null)")
            expect(page.locator("#host-workspace-grid iframe")).to_have_count(1)
            self.assertTrue(
                page.evaluate(
                    "document.querySelector('#host-workspace-grid iframe') === window.__retainedDesktopFrame"
                )
            )
        finally:
            page.close()

    def test_host_workspace_mode_switch_restores_desktop_and_terminal_layouts(self):
        page = self.new_page()
        try:
            self.goto_shell(page)
            page.wait_for_function("typeof applyClusterMode === 'function'")
            result = page.evaluate(
                """() => {
                  desktopHosts = [
                    {id:'default',worker_id:'ats-worker-controller',name:'Local',connection:'local@127.0.0.1'},
                    {id:'cluster:a',worker_id:'a',name:'A',connection:'a@192.0.2.10'},
                    {id:'cluster:b',worker_id:'b',name:'B',connection:'b@192.0.2.11'},
                    {id:'cluster:c',worker_id:'c',name:'C',connection:'c@192.0.2.12'},
                  ];
                  currentHost=desktopHosts[0];
                  currentPage='reports';
                  window.hostWorkspaceInitialized=true;
                  window.terminalWorkspaceInitialized=true;
                  hostWorkspaceClusterEnabled=true;
                  hostWorkspace.layout='quad';
                  hostWorkspace.panes=desktopHosts.map(host=>({type:'desktop',hostId:host.id}));
                  hostWorkspace.maximized=null;
                  hostWorkspace.instances.clear();
                  hostWorkspace.renderedSignature=null;
                  terminalWorkspace.layout='horizontal';
                  terminalWorkspace.panes=[{hostId:'default'},{hostId:'cluster:a'}];
                  terminalWorkspace.maximized=null;
                  terminalWorkspace.instances.clear();
                  terminalWorkspace.renderedSignature=null;
                  document.getElementById('host-workspace-grid').replaceChildren();
                  document.getElementById('terminal-workspace-grid').replaceChildren();

                  applyClusterMode(false);
                  const single = {
                    bodyClass: document.body.className,
                    desktopLayout: hostWorkspace.layout,
                    terminalLayout: terminalWorkspace.layout,
                    desktopPanes: hostWorkspace.panes.length,
                    terminalPanes: terminalWorkspace.panes.length,
                    desktopBar: getComputedStyle(document.querySelector('#page-desktop .host-workspace-single-bar')).display,
                    terminalBar: getComputedStyle(document.querySelector('#page-terminal .host-workspace-single-bar')).display,
                    desktopHeader: getComputedStyle(document.querySelector('#page-desktop .host-workspace-pane-header')).display,
                    terminalHeader: getComputedStyle(document.querySelector('#page-terminal .host-workspace-pane-header')).display,
                    desktopReady: document.getElementById('page-desktop').classList.contains('host-workspace-ready'),
                    terminalReady: document.getElementById('page-terminal').classList.contains('host-workspace-ready'),
                    savedDesktopLayout: JSON.parse(localStorage.getItem('gms_host_workspace')).layout,
                    savedTerminalLayout: JSON.parse(localStorage.getItem('gms_terminal_workspace')).layout,
                  };

                  applyClusterMode(true);
                  const cluster = {
                    bodyClass: document.body.className,
                    desktopLayout: hostWorkspace.layout,
                    terminalLayout: terminalWorkspace.layout,
                    desktopPanes: hostWorkspace.panes.map(pane=>pane.hostId),
                    terminalPanes: terminalWorkspace.panes.map(pane=>pane.hostId),
                    desktopBar: getComputedStyle(document.querySelector('#page-desktop .host-workspace-single-bar')).display,
                    terminalBar: getComputedStyle(document.querySelector('#page-terminal .host-workspace-single-bar')).display,
                    desktopHeader: getComputedStyle(document.querySelector('#page-desktop .host-workspace-pane-header')).display,
                    terminalHeader: getComputedStyle(document.querySelector('#page-terminal .host-workspace-pane-header')).display,
                    desktopReady: document.getElementById('page-desktop').classList.contains('host-workspace-ready'),
                    terminalReady: document.getElementById('page-terminal').classList.contains('host-workspace-ready'),
                  };
                  return {single,cluster};
                }"""
            )
            self.assertIn("workspace-scope-single", result["single"]["bodyClass"])
            self.assertEqual(result["single"]["desktopLayout"], "single")
            self.assertEqual(result["single"]["terminalLayout"], "single")
            self.assertEqual(result["single"]["desktopPanes"], 1)
            self.assertEqual(result["single"]["terminalPanes"], 1)
            self.assertEqual(result["single"]["desktopBar"], "flex")
            self.assertEqual(result["single"]["terminalBar"], "flex")
            self.assertEqual(result["single"]["desktopHeader"], "none")
            self.assertEqual(result["single"]["terminalHeader"], "none")
            self.assertTrue(result["single"]["desktopReady"])
            self.assertTrue(result["single"]["terminalReady"])
            self.assertEqual(result["single"]["savedDesktopLayout"], "quad")
            self.assertEqual(result["single"]["savedTerminalLayout"], "horizontal")

            self.assertIn("workspace-scope-cluster", result["cluster"]["bodyClass"])
            self.assertEqual(result["cluster"]["desktopLayout"], "quad")
            self.assertEqual(result["cluster"]["terminalLayout"], "horizontal")
            self.assertEqual(
                result["cluster"]["desktopPanes"],
                ["default", "cluster:a", "cluster:b", "cluster:c"],
            )
            self.assertEqual(result["cluster"]["terminalPanes"], ["default", "cluster:a"])
            self.assertEqual(result["cluster"]["desktopBar"], "none")
            self.assertEqual(result["cluster"]["terminalBar"], "none")
            self.assertEqual(result["cluster"]["desktopHeader"], "flex")
            self.assertEqual(result["cluster"]["terminalHeader"], "flex")
            self.assertTrue(result["cluster"]["desktopReady"])
            self.assertTrue(result["cluster"]["terminalReady"])
        finally:
            page.close()

    def test_host_workspace_stale_connection_cannot_overwrite_another_host_status(self):
        page = self.new_page()
        try:
            self.goto_shell(page)
            result = page.evaluate(
                """
                async () => {
                    const originalElevation = ensureTerminalElevation;
                    const originalResolver = resolveWorkspaceVncUrl;
                    const grid = document.getElementById('host-workspace-grid');
                    grid.innerHTML = `
                        <section class="host-workspace-pane" data-workspace-pane="0">
                            <div class="host-workspace-pane-header">
                                <span id="host-workspace-status-0">准备中</span>
                            </div>
                            <div class="host-workspace-pane-body" id="host-workspace-body-0"></div>
                        </section>`;
                    const body = document.getElementById('host-workspace-body-0');
                    desktopHosts = [
                        {id: 'cluster:a', worker_id: 'a', connection: 'a@192.0.2.10'},
                        {id: 'cluster:b', worker_id: 'b', connection: 'b@192.0.2.11'}
                    ];
                    hostWorkspace.renderGeneration = 41;
                    hostWorkspace.panes = [{type: 'desktop', hostId: 'cluster:a'}];
                    hostWorkspace.paneGenerations.set(0, 1);
                    let rejectOldAccess;
                    ensureTerminalElevation = async () => true;
                    resolveWorkspaceVncUrl = () => new Promise((resolve, reject) => {
                        rejectOldAccess = reject;
                    });
                    const oldMount = mountHostWorkspaceDesktop(
                        0, desktopHosts[0], body, 41, 'cluster:a', 1
                    );
                    while (!rejectOldAccess) {
                        await new Promise(resolve => setTimeout(resolve, 0));
                    }
                    hostWorkspace.panes[0] = {type: 'desktop', hostId: 'cluster:b'};
                    hostWorkspace.paneGenerations.set(0, 2);
                    body.textContent = 'Worker B desktop';
                    setHostWorkspaceStatus(0, 'Worker B 已连接', true);
                    rejectOldAccess(new Error('Worker A connection failed'));
                    await oldMount;
                    ensureTerminalElevation = originalElevation;
                    resolveWorkspaceVncUrl = originalResolver;
                    return {
                        status: document.getElementById('host-workspace-status-0').textContent,
                        body: body.textContent,
                    };
                }
                """
            )

            self.assertEqual(result["status"], "Worker B 已连接")
            self.assertEqual(result["body"], "Worker B desktop")
        finally:
            page.close()

    def test_host_workspace_initial_scope_never_paints_the_wrong_mode(self):
        for scope_mode, page_name in (
            ("single", "desktop"),
            ("cluster", "desktop"),
            ("single", "terminal"),
            ("cluster", "terminal"),
        ):
            with self.subTest(scope_mode=scope_mode, page_name=page_name):
                page = self.new_page()

                def json_response(route, payload):
                    route.fulfill(
                        status=200,
                        content_type="application/json",
                        body=json.dumps(payload),
                    )

                def response_handler(payload):
                    def handle(route):
                        json_response(route, payload)

                    return handle

                page.route(
                    "**/api/cluster/status",
                    lambda route: json_response(route, {
                        "success": True,
                        "enabled": True,
                        "local_worker_id": "ats-worker-controller",
                    }),
                )
                page.route(
                    "**/api/users/workspace-context",
                    response_handler({
                        "success": True,
                        "data": {"context": {
                            "scope_mode": scope_mode,
                            "worker_id": "worker-a" if scope_mode == "cluster" else "ats-worker-controller",
                            "device_ids": [],
                        }},
                    }),
                )
                page.route(
                    "**/api/cluster/hosts",
                    lambda route: json_response(route, {
                        "success": True,
                        "hosts": [{
                            "worker_id": "worker-a",
                            "name": "Worker A",
                            "status": "online",
                            "address": "192.0.2.10",
                            "ssh_user": "tester",
                        }],
                    }),
                )
                page.route(
                    "**/api/cluster/workers",
                    lambda route: json_response(route, {
                        "success": True,
                        "workers": [{"id": "worker-a", "name": "Worker A", "status": "online"}],
                    }),
                )
                page.route(
                    "**/api/desktop/novnc/access",
                    lambda route: json_response(route, {"success": True, "url": "about:blank"}),
                )
                page.route(
                    "**/api/desktop/vnc/status",
                    lambda route: json_response(route, {"success": True, "running": True}),
                )
                page.add_init_script(
                    """
                    localStorage.setItem('gms_current_page','__PAGE_NAME__');
                    window.__scopeClassHistory=[];
                    window.addEventListener('gms:auth-ready',()=>{
                      state.elevated=true;
                      state.elevatedUntil=Date.now()+60000;
                    });
                    document.addEventListener('DOMContentLoaded',()=>{
                      const record=()=>window.__scopeClassHistory.push(document.body.className);
                      record();
                      new MutationObserver(record).observe(document.body,{attributes:true,attributeFilter:['class']});
                    });
                    const nativeFetch=window.fetch.bind(window);
                    window.fetch=(input,options={})=>{
                      const request=nativeFetch(input,options);
                      const url=String(input);
                      if(url.includes('/api/cluster/status')||url.includes('/api/users/workspace-context')){
                        return new Promise((resolve,reject)=>setTimeout(()=>request.then(resolve,reject),1000));
                      }
                      return request;
                    };
                    """
                    .replace("__PAGE_NAME__", page_name)
                )
                try:
                    elevated = page.request.post(
                        f"{self.base_url}/api/auth/elevate",
                        data={"username": "ui-admin", "password": "UiSmokeAdmin-2026!"},
                    )
                    self.assertTrue(elevated.ok, elevated.text())
                    page.goto(self.base_url, wait_until="domcontentloaded")
                    page_selector = f"#page-{page_name}"
                    grid_selector = (
                        "#host-workspace-grid"
                        if page_name == "desktop"
                        else "#terminal-workspace-grid"
                    )
                    expect(page.locator("body")).to_have_class(re.compile(r"workspace-scope-pending"))
                    expect(page.locator(f"{page_selector} .host-workspace-mode-pending")).to_be_visible()
                    expect(page.locator(f"{page_selector} .host-workspace-layouts")).to_be_hidden()
                    expect(page.locator(f"{page_selector} .host-workspace-single-bar")).to_be_hidden()
                    pending_status_box = page.locator(
                        f"{page_selector} .host-workspace-mode-pending .host-workspace-pane-status"
                    ).bounding_box()
                    self.assertIsNotNone(pending_status_box)
                    self.assertEqual(
                        page.locator(grid_selector).evaluate(
                            "grid=>getComputedStyle(grid).visibility"
                        ),
                        "hidden",
                    )

                    expected_class = f"workspace-scope-{scope_mode}"
                    expect(page.locator("body")).to_have_class(re.compile(rf"\b{expected_class}\b"))
                    expect(page.locator(page_selector)).to_have_class(
                        re.compile(r"\bhost-workspace-ready\b")
                    )
                    expect(page.locator(f"{page_selector} .host-workspace-mode-pending")).to_be_hidden()
                    self.assertEqual(
                        page.locator(grid_selector).evaluate(
                            "grid=>getComputedStyle(grid).visibility"
                        ),
                        "visible",
                    )
                    page_box = page.locator(page_selector).bounding_box()
                    grid_box = page.locator(grid_selector).bounding_box()
                    frame_geometry = page.locator(page_selector).evaluate(
                        "page=>{const style=getComputedStyle(page);return {"
                        "inset:parseFloat(style.getPropertyValue('--page-body-frame-inset'))||0,"
                        "top:parseFloat(style.getPropertyValue('--page-body-frame-top'))||0}}"
                    )
                    self.assertIsNotNone(page_box)
                    self.assertIsNotNone(grid_box)
                    self.assertAlmostEqual(
                        grid_box["x"], page_box["x"] + frame_geometry["inset"], delta=0.5
                    )
                    self.assertAlmostEqual(
                        grid_box["y"], page_box["y"] + frame_geometry["top"], delta=0.5
                    )
                    self.assertAlmostEqual(
                        grid_box["width"],
                        page_box["width"] - (2 * frame_geometry["inset"]),
                        delta=0.5,
                    )
                    self.assertAlmostEqual(
                        grid_box["y"] + grid_box["height"],
                        page_box["y"] + page_box["height"] - frame_geometry["inset"],
                        delta=0.5,
                    )
                    if scope_mode == "single":
                        status_selector = (
                            "#host-workspace-status-single"
                            if page_name == "desktop"
                            else "#terminal-workspace-status-single"
                        )
                        final_status_box = page.locator(status_selector).bounding_box()
                        title_box = page.locator(
                            f"{page_selector} .host-workspace-title-row > .section-title"
                        ).bounding_box()
                        refresh_box = page.locator(
                            f"{page_selector} .host-workspace-single-bar button"
                        ).bounding_box()
                        self.assertIsNotNone(final_status_box)
                        self.assertIsNotNone(title_box)
                        self.assertIsNotNone(refresh_box)
                        for key in ("x", "y", "width", "height"):
                            self.assertAlmostEqual(
                                final_status_box[key], pending_status_box[key], delta=0.5
                            )
                        self.assertAlmostEqual(title_box["y"], pending_status_box["y"], delta=0.5)
                        self.assertAlmostEqual(refresh_box["y"], pending_status_box["y"], delta=0.5)
                        self.assertGreater(refresh_box["x"], final_status_box["x"])
                        refresh_button = page.locator(
                            f"{page_selector} .host-workspace-single-bar .host-workspace-refresh-btn"
                        )
                    else:
                        refresh_button = page.locator(
                            f"{page_selector} .host-workspace-pane-header .host-workspace-refresh-btn"
                        ).first
                    refresh_style = refresh_button.evaluate(
                        "button=>({border:button.style.border||getComputedStyle(button).border,"
                        "background:getComputedStyle(button).backgroundColor,"
                        "boxShadow:getComputedStyle(button).boxShadow})"
                    )
                    self.assertIn(refresh_style["border"], ("0px", "0px none rgb(255, 255, 255)"))
                    self.assertEqual(refresh_style["background"], "rgba(0, 0, 0, 0)")
                    self.assertEqual(refresh_style["boxShadow"], "none")
                    pane = page.locator(f"{page_selector} .host-workspace-pane").first
                    pane_box = pane.bounding_box()
                    pane_style = pane.evaluate(
                        "pane=>{const style=getComputedStyle(pane);return {"
                        "borderTop:style.borderTopWidth,borderRight:style.borderRightWidth,"
                        "borderBottom:style.borderBottomWidth,borderLeft:style.borderLeftWidth}}"
                    )
                    self.assertIsNotNone(pane_box)
                    if scope_mode == "single":
                        self.assertEqual(set(pane_style.values()), {"0px"})
                    else:
                        self.assertGreaterEqual(pane_box["x"], grid_box["x"] + 8)
                        self.assertGreaterEqual(pane_box["y"], grid_box["y"] + 8)
                    history = page.evaluate("window.__scopeClassHistory")
                    wrong_class = (
                        "workspace-scope-cluster"
                        if scope_mode == "single"
                        else "workspace-scope-single"
                    )
                    self.assertFalse(any(wrong_class in value for value in history), history)
                    self.assertTrue(any("workspace-scope-pending" in value for value in history), history)
                    self.assertTrue(any(expected_class in value for value in history), history)
                finally:
                    page.close()

    def test_terminal_page_switch_reuses_websocket_and_buffer(self):
        page = self.new_page()
        terminal_websockets = []
        page.on(
            "websocket",
            lambda websocket: terminal_websockets.append(websocket.url)
            if "/api/system/websocket/terminal_workspace_" in websocket.url
            else None,
        )
        try:
            self.goto_shell(page)
            elevated = page.request.post(
                f"{self.base_url}/api/auth/elevate",
                data={
                    "username": "ui-admin",
                    "password": "UiSmokeAdmin-2026!",
                },
            )
            self.assertTrue(elevated.ok, elevated.text())
            page.evaluate("state.elevated = true; state.elevatedUntil = Date.now() + 60000")
            page.evaluate("switchPage('terminal', null)")
            page.wait_for_function(
                """() => {
                  const instance = terminalWorkspace.instances.get(0);
                  return instance?.socket?.readyState === WebSocket.OPEN && instance.shellReady;
                }"""
            )
            before = page.evaluate(
                """() => {
                  const instance = terminalWorkspace.instances.get(0);
                  window.__terminalWorkspaceInstance = instance;
                  return instance.terminal.buffer.active.getLine(
                    instance.terminal.buffer.active.cursorY
                  )?.translateToString(true) || '';
                }"""
            )
            self.assertEqual(len(terminal_websockets), 1)

            page.evaluate("switchPage('reports', null)")
            page.evaluate("switchPage('terminal', null)")
            page.wait_for_timeout(500)
            after = page.evaluate(
                """() => {
                  const instance = terminalWorkspace.instances.get(0);
                  return {
                    same: instance === window.__terminalWorkspaceInstance,
                    line: instance.terminal.buffer.active.getLine(
                      instance.terminal.buffer.active.cursorY
                    )?.translateToString(true) || ''
                  };
                }"""
            )
            self.assertTrue(after["same"])
            self.assertEqual(after["line"], before)
            self.assertEqual(len(terminal_websockets), 1)
        finally:
            page.close()

    def test_device_shell_button_opens_visible_adb_workspace_session(self):
        page = self.new_page()
        try:
            self.goto_shell(page)
            elevated = page.request.post(
                f"{self.base_url}/api/auth/elevate",
                data={
                    "username": "ui-admin",
                    "password": "UiSmokeAdmin-2026!",
                },
            )
            self.assertTrue(elevated.ok, elevated.text())
            page.evaluate(
                """async () => {
                  state.elevated = true;
                  state.elevatedUntil = Date.now() + 60000;
                  window.__terminalFrames = [];
                  const nativeSend = WebSocket.prototype.send;
                  WebSocket.prototype.send = function(payload) {
                    if (this.url.includes('/api/system/websocket/terminal_workspace_')) {
                      window.__terminalFrames.push(payload);
                    }
                    return nativeSend.call(this, payload);
                  };
                  const localWorkerId = workspaceLocalWorkerId();
                  allDevices = [{
                    device_id: 'LOCAL-ADB-1',
                    serial_no: 'LOCAL-ADB-1',
                    worker_id: localWorkerId,
                    status: 'online',
                  }];
                  await openDeviceShell('LOCAL-ADB-1');
                }"""
            )
            page.wait_for_function(
                """() => {
                  const instance = terminalWorkspace.instances.get(0);
                  return currentPage === 'terminal'
                    && instance?.mode === 'adb'
                    && instance?.serialNo === 'LOCAL-ADB-1';
                }"""
            )
            expect(page.locator("#page-terminal")).to_be_visible()
            expect(page.locator("#terminal-workspace-grid .host-workspace-terminal")).to_have_count(1)
            expect(
                page.locator("[data-terminal-pane='0'] [data-terminal-pane-mode-label]")
            ).to_have_text("🐧 终端")

            deadline = time.time() + 5
            connect_messages = []
            terminal_frames = []
            while time.time() < deadline:
                terminal_frames = page.evaluate("window.__terminalFrames || []")
                connect_messages = [
                    json.loads(frame)
                    for frame in terminal_frames
                    if isinstance(frame, str) and '"type":"terminal_connect"' in frame
                ]
                if connect_messages:
                    break
                page.wait_for_timeout(100)

            socket_state = page.evaluate(
                """() => {
                  const instance = terminalWorkspace.instances.get(0);
                  return {
                    frames: window.__terminalFrames || [],
                    readyState: instance?.socket?.readyState,
                    url: instance?.socket?.url,
                    status: document.getElementById('terminal-workspace-status-0')?.textContent,
                  };
                }"""
            )
            self.assertEqual(len(connect_messages), 1, socket_state)
            self.assertEqual(connect_messages[0]["mode"], "adb")
            self.assertEqual(connect_messages[0]["serial_no"], "LOCAL-ADB-1")
            self.assertEqual(
                connect_messages[0]["worker_id"],
                page.evaluate("workspaceLocalWorkerId()"),
            )
            self.assertFalse(
                any(
                    isinstance(frame, str) and "adb -s LOCAL-ADB-1 shell" in frame
                    for frame in terminal_frames
                )
            )
            persisted = page.evaluate(
                "JSON.parse(localStorage.getItem('gms_terminal_workspace'))"
            )
            self.assertEqual(persisted["panes"], [{"hostId": "default"}])

            return_button = page.locator(
                "#terminal-workspace-host-mode-btn:visible, "
                "[data-terminal-pane-host-mode]:visible"
            )
            expect(return_button).to_be_visible()
            return_button.click()
            page.wait_for_function(
                "() => terminalWorkspace.instances.get(0)?.mode === 'ssh'"
            )
            expect(return_button).to_be_hidden()
            expect(
                page.locator("[data-terminal-pane='0'] [data-terminal-pane-mode-label]")
            ).to_have_text("🐧 终端")
            self.assertEqual(
                page.evaluate("terminalWorkspace.panes[0]"),
                {"hostId": "default"},
            )
        finally:
            page.close()

    def test_hidden_host_workspaces_resume_automatically_on_page_entry(self):
        page = self.new_page()
        page.route(
            "**/api/desktop/vnc/status",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body='{"success":true,"running":true}',
            ),
        )
        page.route(
            "**/api/desktop/novnc/access",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body='{"success":true,"url":"about:blank"}',
            ),
        )
        try:
            elevated = page.request.post(
                f"{self.base_url}/api/auth/elevate",
                data={
                    "username": "ui-admin",
                    "password": "UiSmokeAdmin-2026!",
                },
            )
            self.assertTrue(elevated.ok, elevated.text())
            self.goto_shell(page)
            page.wait_for_function("state.authReady")
            page.evaluate(
                """async () => {
                  state.elevated = true;
                  state.elevatedUntil = Date.now() + 60000;
                  switchPage('reports', null);
                  await ensureDesktopInitialized();
                }"""
            )
            page.wait_for_timeout(450)
            expect(page.locator("#host-workspace-status-0")).to_have_text("准备中")
            expect(page.locator("#host-workspace-grid iframe")).to_have_count(0)

            page.evaluate("switchPage('desktop', null)")
            expect(page.locator("#host-workspace-grid iframe")).to_have_count(1)
            expect(page.locator("#host-workspace-status-0")).not_to_have_text("准备中")

            page.evaluate(
                """async () => {
                  switchPage('reports', null);
                  await ensureTerminalWorkspaceInitialized();
                }"""
            )
            page.wait_for_function("window.xtermLoaded === true")
            expect(page.locator("#terminal-workspace-grid .host-workspace-terminal")).to_have_count(0)

            page.evaluate("switchPage('terminal', null)")
            expect(page.locator("#terminal-workspace-grid .host-workspace-terminal")).to_have_count(1)
            expect(page.locator("#terminal-workspace-status-0")).not_to_have_text("准备中")
        finally:
            page.close()

    def test_multi_host_switch_refreshes_only_selected_pane(self):
        page = self.new_page()
        try:
            self.goto_shell(page)
            page.wait_for_function("window.GmsWorkspace && typeof renderHostWorkspace === 'function'")
            result = page.evaluate(
                """async () => {
                  const originalDesktopMount = mountHostWorkspacePane;
                  const originalTerminalMount = mountTerminalWorkspacePane;
                  const desktopMounts = [];
                  const terminalMounts = [];
                  try {
                    desktopHosts = [
                      {id: 'default', worker_id: 'ats-worker-controller', name: 'Local', connection: 'local@127.0.0.1'},
                      {id: 'cluster:worker-a', worker_id: 'worker-a', name: 'Worker A', connection: 'a@192.0.2.10'},
                      {id: 'cluster:worker-b', worker_id: 'worker-b', name: 'Worker B', connection: 'b@192.0.2.11'},
                    ];
                    currentHost = desktopHosts[0];
                    currentPage = 'desktop';
                    hostWorkspace.layout = 'horizontal';
                    hostWorkspace.panes = [
                      {type: 'desktop', hostId: 'default'},
                      {type: 'desktop', hostId: 'cluster:worker-a'},
                    ];
                    hostWorkspace.instances.clear();
                    hostWorkspace.paneGenerations.clear();
                    hostWorkspace.renderedSignature = null;
                    document.getElementById('host-workspace-grid').replaceChildren();
                    mountHostWorkspacePane = async (index, pane) => {
                      const body = document.getElementById(`host-workspace-body-${index}`);
                      const frame = document.createElement('iframe');
                      frame.dataset.hostId = pane.hostId;
                      body.replaceChildren(frame);
                      hostWorkspace.instances.set(index, {
                        type: 'desktop', hostId: pane.hostId, frame, disposed: false,
                      });
                      desktopMounts.push({index, hostId: pane.hostId});
                    };
                    renderHostWorkspace();
                    await new Promise(resolve => setTimeout(resolve, 20));
                    const desktopFirst = hostWorkspace.instances.get(0);
                    const workerBefore = GmsWorkspace.get().worker_id;
                    changeHostWorkspacePaneHost(1, 'cluster:worker-b');
                    const desktopAfter = hostWorkspace.instances.get(0);
                    const desktopDuplicateDisabled = document.querySelector(
                      '[data-workspace-pane="0"] option[value="cluster:worker-b"]'
                    )?.disabled;
                    const desktopMountCountBeforeDuplicate = desktopMounts.length;
                    changeHostWorkspacePaneHost(1, 'default');

                    currentPage = 'terminal';
                    terminalWorkspace.layout = 'horizontal';
                    terminalWorkspace.panes = [
                      {hostId: 'default'},
                      {hostId: 'cluster:worker-a'},
                    ];
                    terminalWorkspace.instances.clear();
                    terminalWorkspace.paneGenerations.clear();
                    terminalWorkspace.renderedSignature = null;
                    document.getElementById('terminal-workspace-grid').replaceChildren();
                    mountTerminalWorkspacePane = async (index, pane) => {
                      const body = document.getElementById(`terminal-workspace-body-${index}`);
                      const surface = document.createElement('div');
                      surface.className = 'host-workspace-terminal';
                      surface.dataset.hostId = pane.hostId;
                      body.replaceChildren(surface);
                      terminalWorkspace.instances.set(index, {
                        type: 'terminal', hostId: pane.hostId, disposed: false,
                      });
                      terminalMounts.push({index, hostId: pane.hostId});
                    };
                    renderTerminalWorkspace();
                    const terminalFirst = terminalWorkspace.instances.get(0);
                    changeTerminalWorkspaceHost(1, 'cluster:worker-b');
                    const terminalAfter = terminalWorkspace.instances.get(0);
                    const terminalDuplicateDisabled = document.querySelector(
                      '[data-terminal-pane="0"] option[value="cluster:worker-b"]'
                    )?.disabled;
                    const terminalMountCountBeforeDuplicate = terminalMounts.length;
                    changeTerminalWorkspaceHost(1, 'default');

                    return {
                      desktopMounts,
                      terminalMounts,
                      desktopFirstPreserved: desktopFirst === desktopAfter,
                      terminalFirstPreserved: terminalFirst === terminalAfter,
                      desktopPaneHosts: hostWorkspace.panes.map(pane => pane.hostId),
                      terminalPaneHosts: terminalWorkspace.panes.map(pane => pane.hostId),
                      desktopDuplicateDisabled,
                      terminalDuplicateDisabled,
                      desktopDuplicateIgnored: desktopMounts.length === desktopMountCountBeforeDuplicate,
                      terminalDuplicateIgnored: terminalMounts.length === terminalMountCountBeforeDuplicate,
                      workerUnchanged: GmsWorkspace.get().worker_id === workerBefore,
                    };
                  } finally {
                    mountHostWorkspacePane = originalDesktopMount;
                    mountTerminalWorkspacePane = originalTerminalMount;
                  }
                }"""
            )
            self.assertEqual(
                result["desktopMounts"],
                [
                    {"index": 0, "hostId": "default"},
                    {"index": 1, "hostId": "cluster:worker-b"},
                ],
            )
            self.assertEqual(
                result["terminalMounts"],
                [
                    {"index": 0, "hostId": "default"},
                    {"index": 1, "hostId": "cluster:worker-b"},
                ],
            )
            self.assertTrue(result["desktopFirstPreserved"])
            self.assertTrue(result["terminalFirstPreserved"])
            self.assertEqual(result["desktopPaneHosts"], ["default", "cluster:worker-b"])
            self.assertEqual(result["terminalPaneHosts"], ["default", "cluster:worker-b"])
            self.assertTrue(result["desktopDuplicateDisabled"])
            self.assertTrue(result["terminalDuplicateDisabled"])
            self.assertTrue(result["desktopDuplicateIgnored"])
            self.assertTrue(result["terminalDuplicateIgnored"])
            self.assertTrue(result["workerUnchanged"])
        finally:
            page.close()

    def test_single_host_switch_applies_on_first_change(self):
        page = self.new_page()
        try:
            self.goto_shell(page)
            page.wait_for_function("window.GmsWorkspace && typeof renderHostWorkspace === 'function'")
            result = page.evaluate(
                """async () => {
                  const originalDesktopMount = mountHostWorkspacePane;
                  const originalTerminalMount = mountTerminalWorkspacePane;
                  try {
                    desktopHosts = [
                      {id: 'cluster:worker-a', worker_id: 'worker-a', name: 'Worker A', connection: 'a@192.0.2.10'},
                      {id: 'cluster:worker-b', worker_id: 'worker-b', name: 'Worker B', connection: 'b@192.0.2.11'},
                    ];
                    state.clusterStatus = {enabled: true};
                    GmsWorkspace.update(
                      {scope_mode: 'cluster', worker_id: 'worker-a'},
                      {source: 'test-setup', persist: false}
                    );

                    hostWorkspaceClusterEnabled = true;
                    hostWorkspaceScopeModeInitialized = true;
                    hostWorkspace.layout = 'single';
                    hostWorkspace.panes = [{type: 'desktop', hostId: 'cluster:worker-a'}];
                    hostWorkspace.clusterState = snapshotHostClusterState();
                    hostWorkspace.instances.clear();
                    hostWorkspace.paneGenerations.clear();
                    hostWorkspace.renderedSignature = null;
                    document.getElementById('host-workspace-grid').replaceChildren();
                    mountHostWorkspacePane = async (index, pane) => {
                      const body = document.getElementById(`host-workspace-body-${index}`);
                      const surface = document.createElement('div');
                      surface.dataset.hostId = pane.hostId;
                      body.replaceChildren(surface);
                      hostWorkspace.instances.set(index, {
                        type: 'desktop', hostId: pane.hostId, disposed: false,
                      });
                    };
                    currentPage = 'desktop';
                    renderHostWorkspace();
                    await new Promise(resolve => setTimeout(resolve, 20));
                    changeHostWorkspacePaneHost(0, 'cluster:worker-b');
                    await new Promise(resolve => setTimeout(resolve, 20));
                    const desktopPaneHost = hostWorkspace.panes[0].hostId;
                    const desktopSurfaceHost = document.querySelector(
                      '#host-workspace-body-0 [data-host-id]'
                    )?.dataset.hostId;

                    GmsWorkspace.update(
                      {worker_id: 'worker-a', origin_page: 'test'},
                      {source: 'test-reset', persist: false}
                    );
                    terminalWorkspace.layout = 'single';
                    terminalWorkspace.panes = [{hostId: 'cluster:worker-a'}];
                    terminalWorkspace.clusterState = snapshotTerminalClusterState();
                    terminalWorkspace.instances.clear();
                    terminalWorkspace.paneGenerations.clear();
                    terminalWorkspace.renderedSignature = null;
                    document.getElementById('terminal-workspace-grid').replaceChildren();
                    mountTerminalWorkspacePane = async (index, pane) => {
                      const body = document.getElementById(`terminal-workspace-body-${index}`);
                      const surface = document.createElement('div');
                      surface.dataset.hostId = pane.hostId;
                      body.replaceChildren(surface);
                      terminalWorkspace.instances.set(index, {
                        type: 'terminal', hostId: pane.hostId, disposed: false,
                      });
                    };
                    currentPage = 'terminal';
                    renderTerminalWorkspace();
                    changeTerminalWorkspaceHost(0, 'cluster:worker-b');
                    await new Promise(resolve => setTimeout(resolve, 20));

                    return {
                      desktopPaneHost,
                      desktopSurfaceHost,
                      terminalPaneHost: terminalWorkspace.panes[0].hostId,
                      terminalSurfaceHost: document.querySelector(
                        '#terminal-workspace-body-0 [data-host-id]'
                      )?.dataset.hostId,
                      contextWorker: GmsWorkspace.get().worker_id,
                    };
                  } finally {
                    mountHostWorkspacePane = originalDesktopMount;
                    mountTerminalWorkspacePane = originalTerminalMount;
                  }
                }"""
            )
            self.assertEqual(result["desktopPaneHost"], "cluster:worker-b")
            self.assertEqual(result["desktopSurfaceHost"], "cluster:worker-b")
            self.assertEqual(result["terminalPaneHost"], "cluster:worker-b")
            self.assertEqual(result["terminalSurfaceHost"], "cluster:worker-b")
            self.assertEqual(result["contextWorker"], "worker-b")
        finally:
            page.close()

    def test_cluster_worker_card_distinguishes_hidden_external_test(self):
        page = self.new_page()

        def json_route(payload):
            return lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(payload),
            )

        worker = {
            "id": "ats-worker-246",
            "name": "ats-worker-246",
            "hostname": "ats-043056-64g",
            "address": "172.16.14.246",
            "status": "busy",
            "agent_version": "0.5.1",
            "last_heartbeat_at": "2026-08-06T00:00:00+00:00",
            "cpu_percent": 1.2,
            "memory_percent": 9.8,
            "memory_available_gb": 56.4,
            "disk_free_gb": 267.5,
            "load_1m": 0.1,
            "running_jobs": 1,
            "external_jobs": 1,
            "max_jobs": 6,
            "capabilities": {"ssh_user": "hcq", "novnc_port": 6080, "tradefed": True},
            "warnings": [
                "Tradefed output has been inactive for 17016 seconds; "
                "the current module may be long-running or stalled"
            ],
        }
        local_worker = {
            **worker,
            "id": "ats-worker-controller",
            "name": "ats-worker-controller",
            "hostname": "controller",
            "address": "127.0.0.1",
            "status": "online",
            "running_jobs": 0,
            "external_jobs": 0,
            "warnings": [],
        }
        page.route(
            "**/api/cluster/workers",
            json_route({"success": True, "workers": [worker, local_worker]}),
        )
        page.route("**/api/cluster/devices", json_route({"success": True, "devices": []}))
        page.route("**/api/cluster/suites", json_route({"success": True, "suites": []}))
        page.route("**/api/cluster/jobs", json_route({"success": True, "jobs": []}))
        page.route("**/api/cluster/worker-tests", json_route({"success": True, "tests": []}))
        page.route(
            "**/api/cluster/suite-library",
            json_route({
                "success": True,
                "archives": [{"name": "android-cts.zip", "size": 1024, "modified": 1}],
            }),
        )
        page.route(
            "**/api/cluster/status",
            json_route({
                "success": True,
                "enabled": True,
                "remote_dispatch_enabled": True,
                "local_worker_id": "ats-worker-controller",
            }),
        )
        try:
            self.goto_shell(page)
            page.evaluate("switchPage('cluster', null)")
            frame = self.frame_for(page, "#cluster-frame")
            card = frame.locator("#workers .card").filter(has_text="ats-worker-246")
            expect(card.locator(".status.external_busy")).to_have_text("外部占用")
            expect(card.locator(".host-assessment")).to_contain_text("外部测试中")
            expect(card.locator(".host-warning")).to_contain_text(
                "Tradefed 已 4小时43分钟 未产生输出"
            )
            expect(card.locator(".host-tests")).to_contain_text(
                "检测到 1 个外部测试，详情暂不可用"
            )
            expect(card.locator(".host-tests")).not_to_contain_text("当前无测试")
            expect(frame.locator("#job-worker option").first).to_have_attribute(
                "value", "ats-worker-controller"
            )
            expect(frame.locator("#library-worker-0 option").first).to_have_attribute(
                "value", "ats-worker-controller"
            )
            self.assertTrue(
                card.evaluate(
                    """card => {
                      const tests = card.querySelector('.host-tests');
                      const warning = card.querySelector('.host-warning');
                      return Boolean(
                        tests && warning
                        && (tests.compareDocumentPosition(warning) & Node.DOCUMENT_POSITION_FOLLOWING)
                        && getComputedStyle(tests).borderTopWidth === '1px'
                      );
                    }"""
                )
            )
        finally:
            page.close()

    def test_terminal_credential_dialog_saves_then_retries(self):
        page = self.new_page()
        saved = []

        def save_credential(route):
            saved.append(route.request.post_data_json)
            route.fulfill(
                status=200,
                content_type="application/json",
                body='{"success":true}',
            )

        page.route("**/api/config/client-ssh-credentials", save_credential)
        try:
            self.goto_shell(page)
            page.evaluate(
                """() => {
                  window.__terminalCredentialRetried = false;
                  showDevicePasswordModal(
                    'hcq@172.16.14.118',
                    'terminal',
                    () => { window.__terminalCredentialRetried = true; }
                  );
                }"""
            )
            expect(page.locator("#device-password-modal")).to_have_class(re.compile(r"show"))
            expect(page.locator("#device-password-modal-title")).to_have_text("主机终端 SSH 密码")
            expect(page.locator("#device-host-display")).to_have_value("hcq@172.16.14.118")
            expect(page.locator("#device-host-display")).not_to_be_editable()
            page.locator("#device-pswd").fill("temporary-password")
            page.locator("#device-password-modal .btn-primary").click()
            page.wait_for_function("window.__terminalCredentialRetried === true")
            self.assertEqual(saved[0]["device_host"], "hcq@172.16.14.118")
            self.assertEqual(saved[0]["password"], "temporary-password")
        finally:
            page.close()

    def test_sidebar_settings_project_guide_is_accessible(self):
        page = self.new_page()
        page_errors = []
        page.on("pageerror", lambda exc: page_errors.append(str(exc)))
        try:
            self.goto_shell(page)
            page.locator(".sidebar-brand").click()
            page.locator('[data-sidebar-settings-tab="guide"]').click()

            guide = page.locator("#sidebar-settings-panel-guide")
            expect(guide).to_be_visible()
            expect(guide).to_contain_text("5 步快速开始测试")
            expect(page.locator("#project-guide-url")).to_have_attribute(
                "href", f"{self.base_url}/"
            )
            expect(page.locator("#project-guide-url")).to_contain_text(
                f"{self.base_url}/"
            )
            guide_images = page.locator("#sidebar-settings-panel-guide .project-guide-image-button img")
            expect(guide_images).to_have_count(9)
            for image_index in range(guide_images.count()):
                guide_images.nth(image_index).scroll_into_view_if_needed()
                expect(guide_images.nth(image_index)).to_have_js_property("naturalWidth", 1600)

            page.locator(".project-guide-image-button").first.click()
            image_modal = page.locator("#guide-image-modal")
            expect(image_modal).to_have_class(re.compile(r"\bshow\b"))
            expect(page.locator("#guide-image-title")).to_have_text("测试实例：类型、套件、模块与用例")
            page.locator("#guide-image-zoom-btn").click()
            expect(page.locator("#guide-image-preview")).to_have_class(re.compile(r"\bactual-size\b"))
            page.keyboard.press("Escape")
            expect(image_modal).not_to_have_class(re.compile(r"\bshow\b"))
            expect(guide).to_be_visible()

            page.locator('[data-sidebar-settings-tab="visibility"]').click()
            expect(page.locator("#sidebar-settings-panel-visibility")).to_be_visible()
            expect(guide).to_be_hidden()
            self.assert_no_page_errors(page_errors)
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

    def test_main_shell_modals_fit_supported_viewports_and_stack_in_order(self):
        page = self.new_page()
        page_errors = []
        page.on("pageerror", lambda exc: page_errors.append(str(exc)))
        try:
            self.goto_shell(page)
            page.wait_for_function("typeof ModalManager === 'object'")
            modal_ids = page.locator(".modal[id]").evaluate_all(
                "(items) => items.map(item => item.id).filter(Boolean)"
            )
            viewports = [
                {"width": 1440, "height": 960},
                {"width": 768, "height": 720},
                {"width": 390, "height": 844},
                {"width": 844, "height": 390},
            ]

            for viewport in viewports:
                page.set_viewport_size(viewport)
                for modal_id in modal_ids:
                    with self.subTest(viewport=viewport, modal=modal_id):
                        page.evaluate("id => ModalManager.open(id)", modal_id)
                        report = page.locator(f"#{modal_id}").evaluate(
                            """modal => {
                              const content = modal.querySelector('.modal-content');
                              if (!content) return {missingContent: true};
                              const rect = content.getBoundingClientRect();
                              const controls = Array.from(content.querySelectorAll(
                                'button, [role="button"]'
                              )).filter(node => {
                                const style = getComputedStyle(node);
                                return style.display !== 'none' && style.visibility !== 'hidden';
                              });
                              return {
                                missingContent: false,
                                rect: {
                                  left: rect.left,
                                  top: rect.top,
                                  right: rect.right,
                                  bottom: rect.bottom,
                                  width: rect.width,
                                  height: rect.height
                                },
                                viewport: {width: innerWidth, height: innerHeight},
                                overflowControls: controls.filter(node =>
                                  node.clientWidth > 0 && node.scrollWidth > node.clientWidth + 2
                                ).map(node => node.id || node.className || node.tagName).slice(0, 8)
                              };
                            }"""
                        )
                        self.assertFalse(report["missingContent"], report)
                        rect = report["rect"]
                        self.assertGreater(rect["width"], 0, report)
                        self.assertGreater(rect["height"], 0, report)
                        self.assertGreaterEqual(rect["left"], -1, report)
                        self.assertGreaterEqual(rect["top"], -1, report)
                        self.assertLessEqual(rect["right"], report["viewport"]["width"] + 1, report)
                        self.assertLessEqual(rect["bottom"], report["viewport"]["height"] + 1, report)
                        self.assertEqual(report["overflowControls"], [], report)
                        page.evaluate("id => ModalManager.close(id)", modal_id)

            first_id, second_id = modal_ids[:2]
            page.evaluate(
                "ids => { ModalManager.open(ids[0]); ModalManager.open(ids[1]); }",
                [first_id, second_id],
            )
            stack = page.evaluate(
                """ids => ({
                  active: ModalManager._activeModals.slice(),
                  firstZ: Number(getComputedStyle(document.getElementById(ids[0])).zIndex),
                  secondZ: Number(getComputedStyle(document.getElementById(ids[1])).zIndex),
                  firstInert: document.getElementById(ids[0]).inert,
                  secondInert: document.getElementById(ids[1]).inert
                })""",
                [first_id, second_id],
            )
            self.assertEqual(stack["active"][-2:], [first_id, second_id])
            self.assertGreater(stack["secondZ"], stack["firstZ"])
            self.assertTrue(stack["firstInert"])
            self.assertFalse(stack["secondInert"])
            page.keyboard.press("Escape")
            expect(page.locator(f"#{second_id}")).not_to_have_class(re.compile(r"show"))
            expect(page.locator(f"#{first_id}")).to_have_class(re.compile(r"show"))
            self.assertFalse(page.locator(f"#{first_id}").evaluate("modal => modal.inert"))
            page.keyboard.press("Escape")
            expect(page.locator(".modal.show")).to_have_count(0)
            self.assert_no_page_errors(page_errors)
        finally:
            page.close()

    def test_standalone_pages_keep_overlays_inside_mobile_viewport(self):
        page = self.new_page()
        page_errors = []
        page.on("pageerror", lambda exc: page_errors.append(str(exc)))

        def assert_overlay_fits(selector):
            report = page.locator(selector).evaluate(
                """overlay => {
                  const rect = overlay.getBoundingClientRect();
                  return {
                    left: rect.left, top: rect.top, right: rect.right, bottom: rect.bottom,
                    width: rect.width, height: rect.height,
                    viewportWidth: innerWidth, viewportHeight: innerHeight,
                    className: overlay.className,
                    transform: getComputedStyle(overlay).transform
                  };
                }"""
            )
            self.assertGreater(report["width"], 0, report)
            self.assertGreater(report["height"], 0, report)
            self.assertGreaterEqual(report["left"], -1, report)
            self.assertGreaterEqual(report["top"], -1, report)
            self.assertLessEqual(report["right"], report["viewportWidth"] + 1, report)
            self.assertLessEqual(report["bottom"], report["viewportHeight"] + 1, report)

        try:
            page.set_viewport_size({"width": 390, "height": 720})

            page.goto(f"{self.base_url}/redmine-agent", wait_until="domcontentloaded")
            page.wait_for_function("typeof showModal === 'function'")
            redmine_ids = page.locator(".modal[id]").evaluate_all(
                "(items) => items.map(item => item.id)"
            )
            for modal_id in redmine_ids:
                page.evaluate("id => showModal(id)", modal_id)
                assert_overlay_fits(f"#{modal_id} .modal-content")
                page.evaluate("id => hideModal(id)", modal_id)

            page.goto(f"{self.base_url}/gerrit-dashboard", wait_until="domcontentloaded")
            page.wait_for_function("typeof showModal === 'function'")
            gerrit_ids = page.locator(".modal[id]").evaluate_all(
                "(items) => items.map(item => item.id)"
            )
            for modal_id in gerrit_ids:
                page.evaluate("id => showModal(id)", modal_id)
                assert_overlay_fits(f"#{modal_id} .modal-content")
                page.evaluate("id => hideModal(id)", modal_id)

            page.goto(f"{self.base_url}/cluster", wait_until="domcontentloaded")
            page.wait_for_function("typeof syncClusterModalState === 'function'")
            page.evaluate(
                """() => {
                  document.getElementById('onboarding').hidden = false;
                  document.getElementById('worker-config-modal').hidden = false;
                  syncClusterModalState();
                }"""
            )
            assert_overlay_fits("#onboarding .onboarding-modal")
            assert_overlay_fits("#worker-config-modal .onboarding-modal")
            self.assertTrue(page.locator("#onboarding").evaluate("modal => modal.inert"))
            page.keyboard.press("Escape")
            expect(page.locator("#worker-config-modal")).to_be_hidden()
            expect(page.locator("#onboarding")).to_be_visible()
            page.keyboard.press("Escape")
            expect(page.locator("#onboarding")).to_be_hidden()

            page.goto(f"{self.base_url}/automation", wait_until="domcontentloaded")
            page.wait_for_function("typeof openTrace === 'function'")
            page.evaluate("openTrace()")
            page.wait_for_function(
                "document.getElementById('ats-trace-drawer').getBoundingClientRect().right <= innerWidth + 1"
            )
            assert_overlay_fits("#ats-trace-drawer")
            page.keyboard.press("Escape")
            expect(page.locator("#ats-trace-drawer")).not_to_have_class(re.compile(r"open"))
            page.evaluate("void promptBuildPassword()")
            expect(page.locator(".password-backdrop")).to_be_visible()
            assert_overlay_fits(".password-dialog")
            page.keyboard.press("Escape")
            expect(page.locator(".password-backdrop")).to_have_count(0)

            self.assert_no_page_errors(page_errors)
        finally:
            page.close()

    def test_worker_config_retries_after_admin_elevation(self):
        page = self.new_page()
        requests = []

        def worker_config(route):
            requests.append(route.request.url)
            if len(requests) == 1:
                route.fulfill(
                    status=403,
                    content_type="application/json",
                    body=json.dumps({
                        "detail": {
                            "message": "Elevation required",
                            "elevation_required": True,
                        },
                    }),
                )
                return
            route.fulfill(
                status=200,
                content_type="application/json",
                body='{"success":true,"config":{"max_jobs":4}}',
            )

        try:
            page.route(
                "**/api/cluster/workers/ats-worker-246/config",
                worker_config,
            )
            page.goto(f"{self.base_url}/cluster", wait_until="domcontentloaded")
            page.wait_for_function("typeof openWorkerConfig === 'function'")
            page.evaluate(
                """async () => {
                    window.__workerConfigElevationLabels = [];
                    window.requestElevatedAccess = async label => {
                        window.__workerConfigElevationLabels.push(label);
                        return true;
                    };
                    await openWorkerConfig('ats-worker-246');
                }"""
            )

            self.assertEqual(len(requests), 2)
            self.assertEqual(
                page.evaluate("window.__workerConfigElevationLabels"),
                ["执行集群敏感操作"],
            )
            expect(page.locator("#config-max-jobs")).to_have_value("4")
            expect(page.locator("#config-error")).to_be_hidden()
        finally:
            page.close()

    def test_standalone_pages_have_no_uncontained_mobile_overflow(self):
        page = self.new_page()
        page_errors = []
        page.on("pageerror", lambda exc: page_errors.append(str(exc)))
        paths = [
            "/automation",
            "/cluster",
            "/redmine-agent",
            "/gerrit-dashboard",
            "/mainline-known-issues",
            "/gms-update-monitor",
            "/templates/architecture.html",
        ]
        viewports = [
            {"width": 390, "height": 720},
            {"width": 844, "height": 390},
        ]
        try:
            for viewport in viewports:
                page.set_viewport_size(viewport)
                for path in paths:
                    with self.subTest(viewport=viewport, path=path):
                        page.goto(f"{self.base_url}{path}", wait_until="domcontentloaded")
                        page.wait_for_timeout(100)
                        report = page.evaluate(
                            """() => {
                              const viewportWidth = innerWidth;
                              const isVisible = node => {
                                const style = getComputedStyle(node);
                                return style.display !== 'none'
                                  && style.visibility !== 'hidden'
                                  && !node.closest('[aria-hidden="true"]')
                                  && node.getClientRects().length > 0;
                              };
                              const hasHorizontalContainer = node => {
                                for (let parent = node.parentElement;
                                     parent && parent !== document.body;
                                     parent = parent.parentElement) {
                                  const overflow = getComputedStyle(parent).overflowX;
                                  if (overflow === 'auto' || overflow === 'scroll') return true;
                                }
                                return false;
                              };
                              const leaks = Array.from(document.querySelectorAll(
                                'button, input, select, textarea, a[href]'
                              )).filter(isVisible).filter(node => {
                                const rect = node.getBoundingClientRect();
                                return (rect.left < -1 || rect.right > viewportWidth + 1)
                                  && !hasHorizontalContainer(node);
                              }).map(node => ({
                                tag: node.tagName,
                                id: node.id,
                                className: String(node.className || ''),
                                text: String(node.textContent || node.value || '').trim().slice(0, 80),
                                rect: {
                                  left: node.getBoundingClientRect().left,
                                  right: node.getBoundingClientRect().right
                                }
                              })).slice(0, 12);
                              const clippedButtons = Array.from(document.querySelectorAll(
                                'button, [role="button"]'
                              )).filter(isVisible).filter(node =>
                                node.clientWidth > 0 && node.scrollWidth > node.clientWidth + 2
                              ).map(node => node.id || String(node.className || '') || node.textContent)
                                .slice(0, 12);
                              return {
                                viewportWidth,
                                documentWidth: document.documentElement.scrollWidth,
                                leaks,
                                clippedButtons
                              };
                            }"""
                        )
                        self.assertLessEqual(
                            report["documentWidth"], report["viewportWidth"] + 1, report
                        )
                        self.assertEqual(report["leaks"], [], report)
                        self.assertEqual(report["clippedButtons"], [], report)
            self.assert_no_page_errors(page_errors)
        finally:
            page.close()

    def test_cluster_dashboard_half_screen_panels_do_not_overlap(self):
        page = self.new_page()
        try:
            page.goto(f"{self.base_url}/cluster", wait_until="domcontentloaded")
            page.wait_for_function(
                "document.querySelectorAll('#dashboard-stats .dash-stat-card').length === 5"
            )

            for viewport in [
                {"width": 960, "height": 720},
                {"width": 760, "height": 720},
            ]:
                with self.subTest(viewport=viewport):
                    page.set_viewport_size(viewport)
                    page.wait_for_timeout(150)
                    report = page.evaluate(
                        """() => {
                          const visible = element => {
                            const style = getComputedStyle(element);
                            return style.display !== 'none'
                              && style.visibility !== 'hidden'
                              && element.getClientRects().length > 0;
                          };
                          const rect = element => {
                            const value = element.getBoundingClientRect();
                            return {
                              id: element.id || String(element.className || ''),
                              left: value.left, right: value.right,
                              top: value.top, bottom: value.bottom,
                              width: value.width, height: value.height,
                            };
                          };
                          const intersections = selector => {
                            const items = Array.from(document.querySelectorAll(selector))
                              .filter(visible).map(rect);
                            const overlaps = [];
                            for (let left = 0; left < items.length; left += 1) {
                              for (let right = left + 1; right < items.length; right += 1) {
                                const a = items[left], b = items[right];
                                const width = Math.min(a.right, b.right) - Math.max(a.left, b.left);
                                const height = Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top);
                                if (width > 1 && height > 1) overlaps.push([a, b]);
                              }
                            }
                            return overlaps;
                          };
                          const panels = Array.from(document.querySelectorAll('.dash-panel'))
                            .filter(visible).map(rect);
                          const pane = document.querySelector('#tab-dashboard');
                          return {
                            cardOverlaps: intersections('.dash-stat-card'),
                            panelOverlaps: intersections('.dash-panel'),
                            panels,
                            paneOverflowY: getComputedStyle(pane).overflowY,
                            documentWidth: document.documentElement.scrollWidth,
                            viewportWidth: innerWidth,
                          };
                        }"""
                    )
                    self.assertEqual(report["cardOverlaps"], [], report)
                    self.assertEqual(report["panelOverlaps"], [], report)
                    self.assertEqual(len(report["panels"]), 4, report)
                    self.assertTrue(
                        all(panel["height"] >= 239 for panel in report["panels"]),
                        report,
                    )
                    self.assertEqual(report["paneOverflowY"], "auto", report)
                    self.assertLessEqual(
                        report["documentWidth"], report["viewportWidth"] + 1, report
                    )

            resize_count = page.evaluate(
                """async () => {
                  window.__dashResizeCount = 0;
                  dashCharts.__test = {
                    isDisposed: () => false,
                    resize: () => { window.__dashResizeCount += 1; },
                  };
                  scheduleDashboardChartsResize();
                  await new Promise(resolve => requestAnimationFrame(() => resolve()));
                  return window.__dashResizeCount;
                }"""
            )
            self.assertGreaterEqual(resize_count, 1)
            page.set_viewport_size({"width": 820, "height": 720})
            page.wait_for_function("window.__dashResizeCount >= 2")
            page.evaluate("delete dashCharts.__test")
        finally:
            page.close()

    def test_cluster_manual_refresh_controls_share_busy_and_idle_state(self):
        page = self.new_page()
        try:
            page.goto(f"{self.base_url}/cluster", wait_until="domcontentloaded")
            page.wait_for_function("refreshPromise === null")
            page.evaluate(
                """() => {
                  window.__clusterOriginalApi = api;
                  window.__clusterRefreshResolvers = [];
                  api = path => new Promise(resolve => {
                    window.__clusterRefreshResolvers.push(() => resolve(
                      path === '/api/cluster/status'
                        ? {enabled: true, local_worker_id: 'ats-worker-controller'}
                        : {}
                    ));
                  });
                  document.querySelector('#dash-refresh-charts').click();
                }"""
            )
            for selector in ["#dash-refresh-charts", "#refresh"]:
                button = page.locator(selector)
                expect(button).to_be_disabled()
                expect(button).to_have_text("刷新中…")
                expect(button).to_have_attribute("aria-busy", "true")

            page.evaluate(
                "window.__clusterRefreshResolvers.splice(0).forEach(resolve => resolve())"
            )
            page.wait_for_function("refreshPromise === null")
            page.evaluate(
                """() => {
                  window.__clusterRefreshResolvers.splice(0).forEach(resolve => resolve());
                  api = window.__clusterOriginalApi;
                }"""
            )
            for selector in ["#dash-refresh-charts", "#refresh"]:
                button = page.locator(selector)
                expect(button).to_be_enabled()
                expect(button).to_have_text("↻ 刷新")
                expect(button).not_to_have_attribute("aria-busy", "true")
        finally:
            page.close()

    def test_shell_pages_keep_visible_controls_inside_narrow_viewport(self):
        page = self.new_page()
        page_errors = []
        page.on("pageerror", lambda exc: page_errors.append(str(exc)))
        try:
            self.goto_shell(page)
            page.set_viewport_size({"width": 390, "height": 720})
            for page_name in self.visible_sidebar_pages(page):
                with self.subTest(page=page_name):
                    self.show_all_sidebar_pages(page)
                    page.evaluate("name => switchPage(name, null)", page_name)
                    report = page.locator(f"#page-{page_name}").evaluate(
                        """container => {
                          const bounds = container.getBoundingClientRect();
                          const isVisible = node => {
                            const style = getComputedStyle(node);
                            return style.display !== 'none'
                              && style.visibility !== 'hidden'
                              && !node.closest('[aria-hidden="true"]')
                              && node.getClientRects().length > 0;
                          };
                          const hasHorizontalContainer = node => {
                            for (let parent = node.parentElement;
                                 parent && parent !== container;
                                 parent = parent.parentElement) {
                              const overflow = getComputedStyle(parent).overflowX;
                              if (overflow === 'auto' || overflow === 'scroll') return true;
                            }
                            return false;
                          };
                          const controls = Array.from(container.querySelectorAll(
                            'button, input, select, textarea, a[href]'
                          )).filter(isVisible);
                          return {
                            container: {
                              left: bounds.left, right: bounds.right,
                              width: bounds.width, viewportWidth: innerWidth
                            },
                            leaks: controls.filter(node => {
                              const rect = node.getBoundingClientRect();
                              return (rect.left < bounds.left - 1
                                || rect.right > Math.min(bounds.right, innerWidth) + 1)
                                && !hasHorizontalContainer(node);
                            }).map(node => ({
                              tag: node.tagName,
                              id: node.id,
                              text: String(node.textContent || node.value || '').trim().slice(0, 80),
                              rect: {
                                left: node.getBoundingClientRect().left,
                                right: node.getBoundingClientRect().right
                              }
                            })).slice(0, 12),
                            clippedButtons: controls.filter(node =>
                              (node.tagName === 'BUTTON' || node.getAttribute('role') === 'button')
                              && node.clientWidth > 0
                              && node.scrollWidth > node.clientWidth + 2
                            ).map(node => node.id || node.textContent).slice(0, 12)
                          };
                        }"""
                    )
                    self.assertGreater(report["container"]["width"], 0, report)
                    self.assertGreaterEqual(report["container"]["left"], -1, report)
                    self.assertLessEqual(
                        report["container"]["right"],
                        report["container"]["viewportWidth"] + 1,
                        report,
                    )
                    self.assertEqual(report["leaks"], [], report)
                    self.assertEqual(report["clippedButtons"], [], report)
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
        runs = []

        def fulfill_automation(route):
            path = route.request.url.split("?", 1)[0]
            method = route.request.method
            data = {}
            if path.endswith("/api/automation/dashboard"):
                data = {"run_total": len(runs), "run_by_status": {}, "run_by_profile": {}}
            elif path.endswith("/api/automation/profiles"):
                data = {"items": []}
            elif path.endswith("/api/automation/runs/preflight"):
                data = {
                    "ready": True,
                    "worker_id": "ats-worker-controller",
                    "test_type": "CTS",
                    "test_suite": "",
                    "devices": ["TESTSERIAL001"],
                }
            elif path.endswith("/api/automation/runs") and method == "POST":
                run = {
                    "id": "ats_ui_smoke",
                    "profile_id": "manual",
                    "status": "queued",
                    "current_stage": "queued",
                    "artifact_path": "/tmp/update.img",
                    "devices_json": '["TESTSERIAL001"]',
                    "report_timestamp": "2026-07-16T10:00:00Z",
                    "report_id": "report-ui-smoke",
                }
                runs[:] = [run]
                data = run
            elif path.endswith("/api/automation/runs"):
                data = {"items": runs}
            elif path.endswith("/api/automation/worker/tick"):
                data = runs[0] if runs else None
            elif path.endswith("/timeline"):
                data = {"items": [{
                    "created_at": "2026-07-16T09:59:00Z",
                    "stage": "testing",
                    "domain": "cluster",
                    "event_type": "command.acknowledged",
                    "level": "info",
                    "message": "Tradefed started",
                }]}
            elif path.endswith("/api/automation/worker/status"):
                data = {"running": False, "interval_seconds": 5, "last_tick_seconds_ago": None}
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"success": True, "data": data}),
            )

        page.route("**/api/automation/**", fulfill_automation)
        page.route(
            "**/api/cluster/status",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body='{"success":true,"enabled":false,"local_worker_id":"ats-worker-controller"}',
            ),
        )
        page.route(
            "**/api/devices/list*",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body='[{"id":"TESTSERIAL001","status":"device","locked":false}]',
            ),
        )
        page.route(
            "**/api/test/suites",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body='{"suites":[]}',
            ),
        )
        try:
            page.goto(f"{self.base_url}/automation", wait_until="domcontentloaded")
            page.wait_for_function("document.body.dataset.automationReady === 'true'")
            page.evaluate("document.querySelector('button[data-workflow=\"create\"]').click()")
            page.wait_for_selector("#automation-create-run")
            page.fill("#automation-artifact", "/tmp/update.img")
            expect(page.locator("#artifact-mode-hint")).to_contain_text("直接使用已有固件")
            expect(page.locator("#build-server")).to_be_disabled()
            page.check('#automation-device-list input[value="TESTSERIAL001"]', force=True)
            page.once("dialog", lambda dialog: dialog.accept())
            page.evaluate("document.getElementById('automation-create-run').click()")
            expect(page.locator("#automation-toast")).to_contain_text("流水线已启动")
            expect(page.locator("#automation-events .event-line")).to_have_count(1)
            expect(page.locator("#automation-events .event-line-message")).to_contain_text("Tradefed started")
            page.evaluate("switchWorkflowPane('reports')")
            expect(page.locator("#automation-runs-report .report-card")).to_have_count(1)
            expect(page.locator("#automation-runs-report")).to_contain_text("分析报告")
            page.evaluate("switchWorkflowPane('runs')")
            page.evaluate(
                """async () => {
                    const response = await fetch('/api/automation/worker/tick?executor=stub', {method: 'POST'});
                    if (!response.ok) throw new Error(`stub tick failed: ${response.status}`);
                    await loadRuns();
                }"""
            )
            page.evaluate("document.querySelector('button[data-status=\"queued\"]').click()")
            expect(page.locator('button[data-status="queued"]')).to_have_class(re.compile(r"active"))
        finally:
            page.close()

    def test_automation_admin_action_prompts_and_retries_after_elevation(self):
        page = self.new_page()
        tick_attempts = []

        def fulfill_automation(route):
            path = route.request.url.split("?", 1)[0]
            if path.endswith("/api/automation/worker/tick"):
                tick_attempts.append(route.request.url)
                if len(tick_attempts) == 1:
                    route.fulfill(
                        status=403,
                        content_type="application/json",
                        body=json.dumps({
                            "detail": {
                                "message": "Elevation required",
                                "elevation_required": True,
                            }
                        }),
                    )
                    return
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"success": True, "data": None}),
            )

        page.route("**/api/automation/**", fulfill_automation)
        page.route(
            "**/api/cluster/status",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({
                    "success": True,
                    "enabled": False,
                    "local_worker_id": "ats-worker-controller",
                }),
            ),
        )
        page.route(
            "**/api/devices/list*",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body="[]",
            ),
        )
        page.route(
            "**/api/test/suites",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body='{"suites":[]}',
            ),
        )
        try:
            page.goto(f"{self.base_url}/automation", wait_until="domcontentloaded")
            page.wait_for_function(
                "document.body.dataset.automationReady === 'true'"
            )
            result = page.evaluate(
                """async () => {
                    window.__elevationLabels = [];
                    window.requestElevatedAccess = async label => {
                        window.__elevationLabels.push(label);
                        return true;
                    };
                    const response = await api(
                        '/api/automation/worker/tick?executor=stub',
                        {method: 'POST'}
                    );
                    return {
                        response,
                        labels: window.__elevationLabels,
                        allocatedLabel: statusLabel('allocated'),
                        failedLabel: statusLabel('test_failed')
                    };
                }"""
            )
            self.assertEqual(len(tick_attempts), 2)
            self.assertEqual(
                result["labels"],
                ["执行 GMS ATS 管理操作"],
            )
            self.assertEqual(result["allocatedLabel"], "已分配")
            self.assertEqual(result["failedLabel"], "测试失败")
        finally:
            page.close()

    def test_automation_lunch_targets_follow_selected_workspace(self):
        page = self.new_page()

        def fulfill_automation(route):
            path = route.request.url.split("?", 1)[0]
            if path.endswith("/api/automation/dashboard"):
                data = {"run_total": 0, "run_by_status": {}, "run_by_profile": {}}
            elif path.endswith("/api/automation/profiles"):
                data = {"items": [{
                    "id": "manual",
                    "name": "Manual",
                    "build": {},
                    "test_plan": {},
                }]}
            elif path.endswith("/api/automation/runs"):
                data = {"items": []}
            elif path.endswith("/api/automation/worker/status"):
                data = {"running": False, "interval_seconds": 5, "last_tick_seconds_ago": None}
            else:
                data = {"items": []}
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"success": True, "data": data}),
            )

        def fulfill_build(route):
            path = route.request.url.split("?", 1)[0]
            if path.endswith("/api/build/servers"):
                data = {"items": [{
                    "id": "mock-build",
                    "name": "Mock Build",
                    "workspace_root": "/src",
                    "auth": {"type": "env_password"},
                }]}
            elif path.endswith("/api/build/templates"):
                data = {"items": [
                    {
                        "id": "mock-template",
                        "name": "Mock Template",
                        "server_id": "mock-build",
                        "init_commands": ["source build/envsetup.sh", "lunch {lunch_target}"],
                        "command": "{build_command}",
                        "parameters_schema": {
                            "build_command": {"default": "./build.sh -UCKApu -J 8"}
                        },
                    },
                    {
                        "id": "mock-clean-template",
                        "name": "Mock Clean Template",
                        "server_id": "mock-build",
                        "init_commands": ["source build/envsetup.sh", "lunch {lunch_target}"],
                        "command": "{build_command}",
                        "parameters_schema": {
                            "build_command": {"default": "./build.sh -UACKApu -J 8"}
                        },
                    },
                ]}
            elif path.endswith("/api/build/discover/workspaces"):
                data = {"items": ["6_Android16_0623", "other_Android16"]}
            elif path.endswith("/api/build/discover/lunch-options"):
                request = json.loads(route.request.post_data or "{}")
                workspace = request.get("workspace", "")
                data = {"items": (
                    ["rk3576_u-userdebug", "rk3576_u-user"]
                    if workspace.endswith("6_Android16_0623")
                    else ["rk3566_rgo-userdebug"]
                )}
            elif path.endswith("/api/build/jobs"):
                data = {"items": []}
            else:
                data = {}
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"success": True, "data": data}),
            )

        page.route("**/api/automation/**", fulfill_automation)
        page.route("**/api/build/**", fulfill_build)
        page.route(
            "**/api/cluster/**",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body='{"enabled":false,"local_worker_id":"ats-worker-controller","workers":[]}',
            ),
        )
        page.route(
            "**/api/test/suites",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body='{"suites":[]}',
            ),
        )
        page.route(
            "**/api/devices/list*",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body="[]",
            ),
        )
        try:
            page.goto(f"{self.base_url}/automation", wait_until="domcontentloaded")
            page.wait_for_function("document.body.dataset.automationReady === 'true'")
            page.evaluate("document.querySelector('button[data-workflow=\"create\"]').click()")
            guided_copy_layout = page.locator("#workflow-pane-create .guided-copy").first.evaluate(
                """element => ({
                    display: getComputedStyle(element).display,
                    whiteSpace: getComputedStyle(element).whiteSpace,
                })"""
            )
            self.assertEqual(guided_copy_layout, {"display": "flex", "whiteSpace": "nowrap"})
            expect(page.locator("#automation-profile")).to_have_value("manual")
            expect(page.locator("#build-server")).to_have_value("mock-build")
            expect(page.locator("#build-template-hint")).to_contain_text("source build/envsetup.sh")
            expect(page.locator(".build-panel-actions")).to_contain_text("仅编译（调试）")
            build_select_edges = page.locator(".build-source-row").evaluate(
                """element => {
                    const server = element.querySelector('#build-server').getBoundingClientRect();
                    const template = element.querySelector('#build-template').getBoundingClientRect();
                    return {serverTop: server.top, serverBottom: server.bottom,
                            templateTop: template.top, templateBottom: template.bottom};
                }"""
            )
            self.assertAlmostEqual(build_select_edges["serverTop"], build_select_edges["templateTop"], delta=0.5)
            self.assertAlmostEqual(build_select_edges["serverBottom"], build_select_edges["templateBottom"], delta=0.5)
            page.select_option("#build-template", "mock-clean-template")
            expect(page.locator("#build-command")).to_have_value("./build.sh -UACKApu -J 8")
            command_and_targets = page.locator("#automation-build-fields").evaluate(
                """element => {
                    const command = element.querySelector('#build-command').getBoundingClientRect();
                    const workspace = element.querySelector('#build-workspace').getBoundingClientRect();
                    const lunch = element.querySelector('#build-lunch-target').getBoundingClientRect();
                    return {commandTop: command.top, workspaceBottom: workspace.bottom,
                            workspaceTop: workspace.top, lunchTop: lunch.top};
                }"""
            )
            self.assertLess(command_and_targets["workspaceBottom"], command_and_targets["commandTop"])
            self.assertAlmostEqual(
                command_and_targets["workspaceTop"], command_and_targets["lunchTop"], delta=0.5
            )
            self.assertEqual(
                page.locator(".workflow-tab").all_text_contents(),
                ["概览", "创建运行", "运行监控", "构建日志", "事件诊断", "测试报告"],
            )
            page.wait_for_function("!document.querySelector('#build-workspace-refresh').disabled")
            page.evaluate("document.querySelector('#build-workspace-refresh').click()")
            expect(page.locator("#build-password-input")).to_have_count(0)

            expect(page.locator("#build-lunch-status")).to_have_class(re.compile(r"\bready\b"))
            self.assertEqual(
                page.locator("#build-lunch-target option").all_text_contents(),
                ["rk3576_u-userdebug", "rk3576_u-user"],
            )
            expect(page.locator("#build-lunch-status")).to_contain_text("当前源码树")

            page.select_option("#build-workspace", "/src/other_Android16")
            page.wait_for_function(
                "document.querySelector('#build-lunch-target').value === 'rk3566_rgo-userdebug'"
            )
            self.assertEqual(
                page.locator("#build-lunch-target option").all_text_contents(),
                ["rk3566_rgo-userdebug"],
            )
            expect(page.locator("#build-lunch-status")).to_contain_text("当前源码树")
        finally:
            page.close()

    def test_automation_shows_local_controller_and_two_column_device_picker(self):
        page = self.new_page()

        def json_response(route, payload):
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(payload),
            )

        def fulfill_automation(route):
            path = route.request.url.split("?", 1)[0]
            if path.endswith("/api/automation/dashboard"):
                data = {"run_total": 0, "run_by_status": {}, "run_by_profile": {}}
            elif path.endswith("/api/automation/profiles"):
                data = {"items": []}
            elif path.endswith("/api/automation/worker/status"):
                data = {"running": False, "interval_seconds": 5, "last_tick_seconds_ago": None}
            else:
                data = {"items": []}
            json_response(route, {"success": True, "data": data})

        def fulfill_cluster(route):
            path = route.request.url.split("?", 1)[0]
            if path.endswith("/api/cluster/status"):
                payload = {
                    "success": True,
                    "enabled": True,
                    "remote_dispatch_enabled": True,
                    "local_worker_id": "ats-worker-controller",
                }
            elif path.endswith("/api/cluster/workers"):
                payload = {
                    "success": True,
                    "workers": [
                        {
                            "id": "ats-worker-controller",
                            "name": "hcq@172.16.14.233",
                            "address": "172.16.14.233",
                            "status": "online",
                            "agent_version": "controller-0.1.0",
                        },
                        {
                            "id": "worker-1",
                            "name": "ATS Worker",
                            "address": "172.16.14.246",
                            "status": "online",
                            "agent_version": "0.3.1",
                        },
                    ],
                }
            elif path.endswith("/api/cluster/devices"):
                payload = {
                    "success": True,
                    "devices": [
                        {
                            "id": f"worker-1:DEVICE-{index}",
                            "state": "available",
                            "transport": "adb_proxy" if index == 1 else "local_usb",
                            "properties": {
                                "adb_proxy_source_worker_id": "worker-source"
                            } if index == 1 else {},
                        }
                        for index in range(1, 13)
                    ],
                }
            elif path.endswith("/api/cluster/suites"):
                payload = {"success": True, "suites": [
                    {"available": True, "suite_type": "CTS", "suite_version": "15_r1",
                     "tools_path": "/suite/android-cts-15_r1/tools"},
                    {"available": True, "suite_type": "CTS", "suite_version": "16_r2",
                     "tools_path": "/suite/android-cts-16_r2/tools"},
                    {"available": True, "suite_type": "GTS", "suite_version": "14.1-R1",
                     "tools_path": "/suite/android-gts-14.1-R1/tools"},
                ]}
            else:
                payload = {"success": True}
            json_response(route, payload)

        page.route("**/api/automation/**", fulfill_automation)
        page.route("**/api/cluster/**", fulfill_cluster)
        try:
            page.goto(f"{self.base_url}/automation", wait_until="domcontentloaded")
            page.wait_for_function("document.body.dataset.automationReady === 'true'")
            page.evaluate("document.querySelector('button[data-workflow=\"create\"]').click()")
            page.wait_for_function(
                "document.querySelectorAll('#automation-device-list input').length === 12"
            )

            local_option = page.locator('#automation-worker option[value="ats-worker-controller"]')
            expect(local_option).to_have_attribute("disabled", "")
            expect(local_option).to_contain_text("172.16.14.233")
            expect(local_option).to_contain_text("未安装 ATS Agent")
            expect(page.locator("#automation-worker")).to_have_value("worker-1")
            expect(page.locator("#automation-devices")).to_have_count(0)
            columns = page.locator("#automation-device-list").evaluate(
                "element => getComputedStyle(element).gridTemplateColumns.split(' ').length"
            )
            self.assertEqual(columns, 2)
            device_list_size = page.locator("#automation-device-list").evaluate(
                "element => ({height: element.clientHeight, scrollHeight: element.scrollHeight})"
            )
            self.assertGreaterEqual(device_list_size["height"], 112)
            self.assertLessEqual(device_list_size["height"], 124)
            self.assertGreater(device_list_size["scrollHeight"], device_list_size["height"])
            device_header_layout = page.locator(".device-field-copy").evaluate(
                """element => ({
                    display: getComputedStyle(element).display,
                    whiteSpace: getComputedStyle(element).whiteSpace,
                })"""
            )
            self.assertEqual(
                device_header_layout,
                {"display": "flex", "whiteSpace": "nowrap"},
            )
            expect(page.locator("#automation-device-list")).to_contain_text(
                "ADB Proxy · worker-source · 仅免刷机测试"
            )
            adb_proxy = page.locator('#automation-device-list input[data-transport="adb_proxy"]')
            expect(adb_proxy).to_be_disabled()
            page.select_option("#automation-flash-mode", "skip")
            expect(adb_proxy).to_be_enabled()
            expect(page.locator("#flash-mode-hint")).to_contain_text("仅测试")
            self.assertEqual(
                page.locator("#automation-test-type option").all_text_contents(),
                ["CTS", "GSI", "GTS", "GTS-ROOT", "STS", "VTS", "APTS"],
            )
            expect(page.locator("#automation-test-suite")).to_have_value(
                "/suite/android-cts-16_r2/tools"
            )
            self.assertIn(
                "/suite/android-gts-14.1-R1/tools",
                page.locator("#automation-test-suite option").all_text_contents(),
            )
            page.select_option("#automation-test-type", "GTS-ROOT")
            expect(page.locator("#automation-test-suite")).to_have_value(
                "/suite/android-gts-14.1-R1/tools"
            )
            create_layout_size = page.locator(".create-layout").evaluate(
                "element => ({height: element.clientHeight, scrollHeight: element.scrollHeight})"
            )
            self.assertLessEqual(
                create_layout_size["scrollHeight"],
                create_layout_size["height"] + 1,
            )
            outer_spacing = page.locator(".ats-shell").evaluate(
                """shell => {
                    const toolbar = shell.querySelector('.ats-toolbar').getBoundingClientRect();
                    const surface = shell.querySelector('.workflow-surface').getBoundingClientRect();
                    const layout = shell.querySelector('.create-layout').getBoundingClientRect();
                    const profile = shell.querySelector('.profile-panel').getBoundingClientRect();
                    const launch = shell.querySelector('.launch-panel').getBoundingClientRect();
                    const shellRect = shell.getBoundingClientRect();
                    return {
                        shellTop: shellRect.top,
                        toolbarTop: toolbar.top,
                        toolbarBottom: toolbar.bottom,
                        surfaceTop: surface.top,
                        layoutTop: layout.top,
                        profileTop: profile.top,
                        launchBottom: launch.bottom,
                        shellBottom: shellRect.bottom,
                        layoutBottom: layout.bottom,
                    };
                }"""
            )
            self.assertLessEqual(
                outer_spacing["toolbarTop"] - outer_spacing["shellTop"],
                8,
            )
            self.assertLessEqual(
                outer_spacing["surfaceTop"] - outer_spacing["toolbarBottom"],
                6,
            )
            self.assertLessEqual(
                outer_spacing["layoutBottom"] - outer_spacing["launchBottom"],
                1,
            )
            for workflow_name in ["overview", "create", "runs", "build", "events", "reports"]:
                page.locator(f'.workflow-tab[data-workflow="{workflow_name}"]').click()
                pane_top_gap = page.locator(f"#workflow-pane-{workflow_name}").evaluate(
                    """pane => {
                        const surface = pane.closest('.workflow-surface').getBoundingClientRect();
                        return pane.getBoundingClientRect().top - surface.top;
                    }"""
                )
                self.assertLessEqual(abs(pane_top_gap), 1)
            page.locator('.workflow-tab[data-workflow="create"]').click()
            page.set_viewport_size({"width": 1440, "height": 720})
            create_panel_edges = page.locator("#workflow-pane-create").evaluate(
                """element => {
                    const testPanel = element.querySelector('.test-panel').getBoundingClientRect();
                    const testContent = element.querySelector('.test-panel .advanced-test-options').getBoundingClientRect();
                    const launchPanel = element.querySelector('.launch-panel').getBoundingClientRect();
                    return {
                        testPanelBottom: testPanel.bottom,
                        testContentBottom: testContent.bottom,
                        launchPanelTop: launchPanel.top,
                    };
                }"""
            )
            self.assertLessEqual(
                create_panel_edges["testContentBottom"],
                create_panel_edges["testPanelBottom"],
            )
            self.assertLess(
                create_panel_edges["testPanelBottom"],
                create_panel_edges["launchPanelTop"],
            )
        finally:
            page.close()

    def test_redmine_dashboard_safe_controls_and_modals(self):
        page = self.new_page()
        def fulfill_redmine(route):
            route.fulfill(
                status=200,
                content_type="application/json",
                body='{"success":true,"running":false,"last_result":{},"data":{},"items":[]}',
            )

        page.route("**/api/redmine/**", fulfill_redmine)
        page.route("**/api/redmine-agent/**", fulfill_redmine)
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

    def test_gerrit_route_check_reuses_test_page_dialog(self):
        page = self.new_page()
        page_errors = []
        ping_requests = []
        page.on("pageerror", lambda exc: page_errors.append(str(exc)))
        page.route(
            "**/api/config/read",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body='{"success":true,"ubuntu_host":"hcq@172.16.14.233"}',
            ),
        )
        page.route(
            "**/api/ssh/ping",
            lambda route: (
                ping_requests.append(route.request.post_data_json),
                route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=(
                        '{"success":true,"reachable":true,"same_network":true,'
                        '"test_host_ip":"172.16.14.233","client_ip":"172.16.14.88",'
                        '"test_network":"172.16.14.0","client_network":"172.16.14.0",'
                        '"latency":"1ms"}'
                    ),
                ),
            )[-1],
        )
        try:
            self.goto_shell(page)
            page.evaluate(
                """() => {
                    state.clusterMode = false;
                    state.clusterEnabled = false;
                    requireControllerHostAction = () => true;
                }"""
            )

            page.locator("#check-routing-btn").click()
            expect(page.locator("#route-check-dialog")).to_be_visible()
            test_dialog = page.locator("#route-check-dialog").evaluate(
                """dialog => ({
                    className: dialog.className,
                    contentClass: dialog.firstElementChild.className,
                    title: dialog.querySelector('h3').textContent.trim(),
                    labels: [...dialog.querySelectorAll('label')].map(node => node.textContent.trim()),
                    actions: [...dialog.querySelectorAll('.route-check-actions button')]
                        .map(node => node.textContent.trim())
                })"""
            )
            page.locator("#route-check-dialog .route-check-close").click()
            expect(page.locator("#route-check-dialog")).to_have_count(0)

            page.locator('.sidebar-item[data-page="gerrit-dashboard"]').click()
            gerrit = self.frame_for(page, "#gerrit-dashboard-frame")
            gerrit.wait_for_function("typeof checkGerritRoute === 'function'")
            gerrit.evaluate(
                """() => {
                    const banner = document.getElementById('connBanner');
                    banner.classList.add('show');
                    banner.querySelector('button').click();
                }"""
            )

            expect(page.locator("#route-check-dialog")).to_be_visible()
            gerrit_dialog = page.locator("#route-check-dialog").evaluate(
                """dialog => ({
                    className: dialog.className,
                    contentClass: dialog.firstElementChild.className,
                    title: dialog.querySelector('h3').textContent.trim(),
                    labels: [...dialog.querySelectorAll('label')].map(node => node.textContent.trim()),
                    actions: [...dialog.querySelectorAll('.route-check-actions button')]
                        .map(node => node.textContent.trim())
                })"""
            )
            self.assertEqual(gerrit_dialog, test_dialog)
            expect(gerrit.locator("#routeCheckModal")).not_to_have_class(
                re.compile(r"\bshow\b")
            )
            expect(page.locator("#test-host-ip")).to_have_value("172.16.14.233")
            page.locator("#client-ip").fill("172.16.14.88")
            page.locator("#ping-test-btn").click()
            expect(page.locator("#ping-result")).to_contain_text("连通性测试通过")
            self.assertEqual(
                ping_requests,
                [{
                    "test_host_ip": "172.16.14.233",
                    "client_ip": "172.16.14.88",
                }],
            )
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
            page.evaluate("switchWorkflowPane('runs')")
            page.wait_for_selector("button[data-status]")
            for status in ["", "queued", "testing", "completed"]:
                page.evaluate("status => setStatusFilter(status)", status)
                selector = 'button[data-status="' + status + '"]'
                expect(page.locator(selector)).to_have_class(re.compile(r"active"))
            page.evaluate("loadAll()")

            self.assert_no_page_errors(page_errors)
        finally:
            page.close()
