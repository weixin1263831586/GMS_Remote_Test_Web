"""Cluster management, worker status and refresh lifecycle."""

import json
import re
import time

from tests.runtime_ui.harness import PlaywrightError, RuntimeUiHarness, expect


class RuntimeClusterTests(RuntimeUiHarness):
    def test_saved_cluster_refresh_has_stable_surface_before_dom_ready(self):
        page = self.new_page()
        try:
            page.add_init_script(
                """
                localStorage.setItem('gms_current_page', 'cluster');
                window.__clusterFirstSurface = null;
                const observer = new MutationObserver(() => {
                  if (window.__clusterFirstSurface) return;
                  const frame = document.querySelector('#cluster-frame');
                  const shell = frame?.closest('.embedded-frame-shell');
                  const loader = shell?.querySelector('.embedded-frame-loading');
                  if (!frame || !shell || !loader) return;
                  window.__clusterFirstSurface = {
                    readyState: document.readyState,
                    state: shell.dataset.frameState,
                    frameVisibility: getComputedStyle(frame).visibility,
                    loaderDisplay: getComputedStyle(loader).display,
                    loaderBackground: getComputedStyle(loader).backgroundColor,
                  };
                  observer.disconnect();
                });
                observer.observe(document, {childList: true, subtree: true});
                """
            )

            for _ in range(2):
                page.goto(self.base_url, wait_until="domcontentloaded")
                page.wait_for_function("Boolean(window.__clusterFirstSurface)")
                first_surface = page.evaluate("window.__clusterFirstSurface")
                self.assertEqual(first_surface["readyState"], "loading", first_surface)
                self.assertEqual(first_surface["state"], "loading", first_surface)
                self.assertEqual(first_surface["frameVisibility"], "hidden", first_surface)
                self.assertEqual(first_surface["loaderDisplay"], "flex", first_surface)
                self.assertNotEqual(
                    first_surface["loaderBackground"], "rgb(2, 6, 23)", first_surface
                )
                page.wait_for_function(
                    "document.querySelector('#cluster-frame').closest("
                    "'.embedded-frame-shell').dataset.frameState === 'ready'"
                )
        finally:
            page.close()

    def test_cluster_management_tab_survives_shell_refresh(self):
        page = self.new_page()
        try:
            self.goto_shell(page)
            page.evaluate("switchPage('cluster', null)")
            page.wait_for_function(
                "document.querySelector('#cluster-frame').closest("
                "'.embedded-frame-shell').dataset.frameState === 'ready'"
            )
            cluster = self.frame_for(page, "#cluster-frame")
            self.close_initial_modals(page)
            cluster.locator('[data-dash-tab="dashboard"]').focus()
            cluster.locator('[data-dash-tab="dashboard"]').press("ArrowRight")
            expect(cluster.locator("#tab-management")).to_be_visible()
            cluster.locator('[data-dash-tab="management"]').press("Home")
            expect(cluster.locator("#tab-dashboard")).to_be_visible()
            cluster.locator('[data-dash-tab="management"]').click()
            expect(cluster.locator("#tab-management")).to_be_visible()
            expect(cluster.locator("#tab-dashboard")).to_be_hidden()

            page.reload(wait_until="domcontentloaded")
            page.wait_for_function(
                "currentPage === 'cluster' && document.querySelector('#page-cluster').classList.contains('active')"
            )
            page.wait_for_function(
                "document.querySelector('#cluster-frame').closest("
                "'.embedded-frame-shell').dataset.frameState === 'ready'"
            )
            cluster = self.frame_for(page, "#cluster-frame")

            expect(cluster.locator('[data-dash-tab="management"]')).to_have_class(
                re.compile(r"\bactive\b")
            )
            expect(cluster.locator('[data-dash-tab="management"]')).to_have_attribute(
                "aria-selected", "true"
            )
            expect(cluster.locator("#tab-management")).to_be_visible()
            expect(cluster.locator("#tab-dashboard")).to_be_hidden()
            self.assertEqual(
                cluster.evaluate(
                    "window.sessionStorage.getItem('gms_cluster_active_tab')"
                ),
                "management",
            )
        finally:
            page.close()

    def test_cluster_reveals_stable_shell_before_slow_data_refresh_finishes(self):
        page = self.new_page()
        page_errors = []
        page.on("pageerror", lambda exc: page_errors.append(str(exc)))
        page.add_init_script(
            """
            const originalFetchForSlowCluster = window.fetch.bind(window);
            window.fetch = (input, init) => {
              const url = String(input && input.url ? input.url : input);
              if (location.pathname === '/cluster' && url.includes('/api/cluster/')) {
                window.__clusterFetchHeld = true;
                return new Promise(() => {});
              }
              return originalFetchForSlowCluster(input, init);
            };
            """
        )
        try:
            self.goto_shell(page)
            started = time.monotonic()
            page.evaluate("switchPage('cluster', null)")
            try:
                page.wait_for_function(
                    "document.querySelector('#cluster-frame').closest("
                    "'.embedded-frame-shell').dataset.frameState === 'ready'",
                    timeout=4000,
                    polling=50,
                )
            except PlaywrightError as error:
                frame = self.frame_for(page, "#cluster-frame")
                diagnostics = {
                    "parent": page.locator("#cluster-frame").evaluate(
                        "frame => ({frame: {...frame.dataset}, shell: {...frame.closest('.embedded-frame-shell').dataset}})"
                    ),
                    "child": frame.evaluate(
                        "() => ({path: location.pathname, held: window.__clusterFetchHeld, ready: typeof GmsEmbeddedWorkspace, refresh: typeof refresh, body: document.body?.innerText?.slice(0, 200)})"
                    ),
                    "errors": page_errors,
                }
                self.fail(f"cluster stable shell did not become ready: {diagnostics}; {error}")
            self.assertLess(time.monotonic() - started, 4.0)

            cluster = self.frame_for(page, "#cluster-frame")
            expect(cluster.locator("#dashboard-stats")).to_have_attribute(
                "aria-busy", "true"
            )
            expect(cluster.locator("#dash-gauges")).to_contain_text(
                "等待首批资源数据"
            )
            expect(cluster.locator("#dashboard-tests")).to_contain_text(
                "等待测试状态"
            )
            expect(
                page.locator(
                    ".embedded-frame-shell:has(#cluster-frame) > .embedded-frame-loading"
                )
            ).to_be_hidden()
            self.assert_no_page_errors(page_errors)
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
            frame.wait_for_function(
                """card => {
                  const tests = card.querySelector('.host-tests');
                  const warning = card.querySelector('.host-warning');
                  return Boolean(
                    tests && warning
                    && (tests.compareDocumentPosition(warning) & Node.DOCUMENT_POSITION_FOLLOWING)
                    && getComputedStyle(tests).borderTopWidth === '1px'
                  );
                }""",
                arg=card.element_handle(),
            )
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
