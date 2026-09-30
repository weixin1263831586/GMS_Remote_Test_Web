"""Test execution controls, logs and suite selection."""

import json
import re
import time

from tests.runtime_ui.harness import RuntimeUiHarness, expect


class RuntimeTestExecutionTests(RuntimeUiHarness):
    def test_test_log_tab_survives_shell_refresh(self):
        page = self.new_page()
        try:
            self.goto_shell(page)
            page.wait_for_function("typeof switchLogTab === 'function'")
            page.evaluate("switchLogTab('module')")
            expect(page.locator('[data-log-tab="module"]')).to_have_class(
                re.compile(r"\bactive\b")
            )

            page.reload(wait_until="domcontentloaded")
            page.wait_for_function(
                """currentPage === 'test'
                && document.querySelector('[data-log-tab="module"]')
                    .classList.contains('active')"""
            )

            expect(page.locator('[data-log-tab="module"]')).to_have_attribute(
                "aria-selected", "true"
            )
            self.assertEqual(
                page.evaluate("sessionStorage.getItem('gms_test_log_tab')"),
                "module",
            )
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
                    // 与设备前缀一致地设置 workspace worker，否则 startTest
                    // 的跨 Worker invariant 会先拒绝该组合。
                    state.clusterStatus = {...(state.clusterStatus || {}), enabled: true};
                    window.GmsWorkspace?.update({
                        scope_mode: 'cluster',
                        worker_id: 'ats-worker-246'
                    }, {source: 'ui-smoke', persist: false});
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

    def test_test_host_stays_disabled_until_initial_worker_list_is_ready(self):
        page = self.new_page()
        cluster_status_calls = []

        def json_response(route, payload):
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(payload),
            )

        def cluster_status(route):
            cluster_status_calls.append(route.request.url)
            json_response(route, {
                "success": True,
                "enabled": True,
                "local_worker_id": "ats-worker-controller",
            })

        page.route("**/api/cluster/status", cluster_status)
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
            window.__testWorkerRequestFinished = false;
            window.__deviceRequestedBeforeWorkersFinished = false;
            window.fetch = (input, options = {}) => {
                const url = String(input);
                if (url.includes('/api/cluster/workers')) {
                    return new Promise((resolve, reject) => setTimeout(
                        () => nativeFetch(input, options).then(response => {
                            window.__testWorkerRequestFinished = true;
                            resolve(response);
                        }, reject),
                        1500
                    ));
                }
                if ((url.includes('/api/devices/list') || url.includes('/api/cluster/devices'))
                        && !window.__testWorkerRequestFinished) {
                    window.__deviceRequestedBeforeWorkersFinished = true;
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
            page.wait_for_function("window.__deviceRequestedBeforeWorkersFinished === true")

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
            self.assertEqual(len(cluster_status_calls), 1)
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
