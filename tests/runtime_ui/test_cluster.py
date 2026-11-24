"""Cluster management, worker status and refresh lifecycle."""

import json
import re
import time

from tests.runtime_ui.harness import PlaywrightError, RuntimeUiHarness, expect


class RuntimeClusterTests(RuntimeUiHarness):
    def test_cluster_management_is_monitor_only_without_hidden_create_form(self):
        page = self.new_page()
        try:
            page.goto(f"{self.base_url}/cluster?tab=management", wait_until="domcontentloaded")
            self.assertEqual(page.locator("#job-create-form").count(), 0)
            self.assertEqual(page.locator("#create-job").count(), 0)
            self.assertEqual(page.locator("#job-worker").count(), 0)
            self.assertEqual(page.locator("#jobs").count(), 1)
        finally:
            page.close()

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

    def test_cluster_management_scroll_stays_inside_inventory_panels(self):
        page = self.new_page()
        try:
            page.goto(f"{self.base_url}/cluster?tab=management",
                      wait_until="domcontentloaded")
            page.wait_for_function("typeof render === 'function'")
            for width, height in ((1792, 856), (1440, 768), (1100, 700)):
                with self.subTest(width=width, height=height):
                    page.set_viewport_size({"width": width, "height": height})
                    page.locator('[data-dash-tab="management"]').click()
                    dimensions = page.evaluate("""() => {
                        // Sample the dense fixture before polling can replace it.
                        const row = '<tr><td>worker</td><td>test</td><td>online</td>'
                            + '<td>available</td><td>model</td><td>source</td></tr>';
                        document.querySelector('#devices').innerHTML = row.repeat(60);
                        const pane = document.querySelector('#tab-management');
                        const panels = [...pane.querySelectorAll(
                            '.cluster-management-grid > .cluster-pnl')];
                        const table = pane.querySelector('#cluster-device-table');
                        const scroller = table.closest('.table-wrap');
                        const outer = pane.getBoundingClientRect();
                        return {
                            overflowY: getComputedStyle(pane).overflowY,
                            outerScroll: pane.scrollHeight - pane.clientHeight,
                            bodyScroll: document.documentElement.scrollHeight -
                                document.documentElement.clientHeight,
                            panelBottoms: panels.map(p =>
                                p.getBoundingClientRect().bottom),
                            panelHeights: panels.map(p =>
                                p.getBoundingClientRect().height),
                            outerBottom: outer.bottom,
                            scrollerScrollable: scroller.scrollHeight > scroller.clientHeight
                        };
                    }""")
                    self.assertEqual(dimensions["overflowY"], "hidden", dimensions)
                    self.assertLessEqual(dimensions["outerScroll"], 2, dimensions)
                    self.assertLessEqual(dimensions["bodyScroll"], 2, dimensions)
                    self.assertTrue(all(
                        bottom <= dimensions["outerBottom"] + 1
                        for bottom in dimensions["panelBottoms"]
                    ), dimensions)
                    self.assertTrue(all(
                        panel_height >= 95
                        for panel_height in dimensions["panelHeights"]
                    ), dimensions)
                    self.assertTrue(dimensions["scrollerScrollable"], dimensions)

            # Job details remain accessible without reintroducing outer scroll.
            page.evaluate("document.querySelector('#detail').hidden = false")
            expect(page.locator("#close-job-detail")).to_be_visible()
            overlay = page.locator("#detail").evaluate("""el => ({
                top: el.getBoundingClientRect().top,
                bottom: el.getBoundingClientRect().bottom,
                paneBottom: document.querySelector('#tab-management')
                    .getBoundingClientRect().bottom
            })""")
            self.assertLessEqual(overlay["bottom"], overlay["paneBottom"] + 1)
            page.locator("#close-job-detail").click()
            expect(page.locator("#detail")).to_be_hidden()
        finally:
            page.close()


    def test_cluster_initial_failure_is_not_reported_as_empty_resource_pool(self):
        page = self.new_page()
        try:
            page.goto(f"{self.base_url}/cluster?tab=management",
                      wait_until="domcontentloaded")
            page.wait_for_function(
                "typeof render === 'function' && clusterInitialRefreshSettled "
                "&& refreshPromise === null"
            )
            page.evaluate("""() => {
                state.workers = [];
                state.devices = [];
                clusterInitialRefreshSettled = true;
                clusterLoadedResources.delete('workers');
                clusterLoadedResources.delete('devices');
                clusterLoadErrors.workers = 'network unavailable';
                clusterLoadErrors.devices = 'network unavailable';
                render();
            }""")
            expect(page.locator("#workers .cluster-load-failed")).to_contain_text(
                "Worker 主机列表读取失败"
            )
            expect(page.locator("#devices .cluster-load-failed")).to_contain_text(
                "设备资源池读取失败"
            )
            self.assertEqual(page.locator("#workers .cluster-loading-row").count(), 0)
            page.evaluate("""() => {
                clusterLoadedResources.add('workers');
                clusterLoadedResources.add('devices');
                delete clusterLoadErrors.workers;
                delete clusterLoadErrors.devices;
                render();
            }""")
            expect(page.locator("#workers .cluster-load-failed")).to_have_count(0)
            expect(page.locator("#devices .cluster-load-failed")).to_have_count(0)
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
            self.close_initial_modals(page)
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

    def test_cluster_prepaint_restores_management_without_dashboard_flash(self):
        page = self.new_page()
        try:
            page.goto(f"{self.base_url}/cluster", wait_until="domcontentloaded")
            page.evaluate(
                "sessionStorage.setItem('gms_cluster_active_tab', 'management')"
            )
            # Remove the final controller script so this assertion covers the
            # browser's initial CSS-only paint, not the later hydration.
            page.route("**/cluster/page.js*", lambda route: route.abort())
            page.reload(wait_until="domcontentloaded")
            self.assertEqual(page.evaluate(
                "document.documentElement.dataset.clusterInitialTab"
            ), "management")
            expect(page.locator("#tab-management")).to_be_visible()
            expect(page.locator("#tab-dashboard")).to_be_hidden()
            expect(page.locator("#cluster-search")).to_be_visible()
            expect(page.locator("#show-onboarding")).to_be_visible()
            self.assertEqual(
                page.locator('[data-dash-tab="dashboard"]').evaluate(
                    "el => getComputedStyle(el, '::after').display"
                ), "none"
            )
            self.assertNotEqual(
                page.locator('[data-dash-tab="management"]').evaluate(
                    "el => getComputedStyle(el, '::after').content"
                ), "none"
            )
        finally:
            page.close()

    def test_cluster_hard_refresh_resets_tab_without_breaking_normal_refresh(self):
        page = self.new_page()
        try:
            self.goto_shell(page)
            page.evaluate("switchPage('cluster', null)")
            page.wait_for_function(
                "document.querySelector('#cluster-frame').closest("
                "'.embedded-frame-shell').dataset.frameState === 'ready'"
            )
            cluster = self.frame_for(page, "#cluster-frame")
            cluster.locator('[data-dash-tab="management"]').click()
            expect(cluster.locator("#tab-management")).to_be_visible()

            # Normal in-app refresh and Ctrl+R leave the selected Tab intact.
            cluster.evaluate("""() => window.dispatchEvent(
                new KeyboardEvent('keydown', {key:'r',ctrlKey:true,bubbles:true})
            )""")
            self.assertEqual(cluster.evaluate(
                "sessionStorage.getItem('gms_cluster_active_tab')"
            ), "management")
            cluster.locator("#refresh").click()
            expect(cluster.locator("#tab-management")).to_be_visible()

            # A shortcut targeted at the iframe must clear the same keys as
            # the shell, even though keyboard events never bubble to parent.
            cluster.evaluate("""() => window.dispatchEvent(
                new KeyboardEvent('keydown', {
                    key:'R',ctrlKey:true,shiftKey:true,bubbles:true
                })
            )""")
            self.assertEqual(cluster.evaluate(
                "sessionStorage.getItem('gms_cluster_force_dashboard_once')"
            ), "1")
            page.reload(wait_until="domcontentloaded")
            self.close_initial_modals(page)
            cluster = self.frame_for(page, "#cluster-frame")
            expect(cluster.locator('[data-dash-tab="dashboard"]')).to_have_attribute(
                "aria-selected", "true"
            )
            expect(cluster.locator("#tab-dashboard")).to_be_visible()
            expect(cluster.locator("#tab-management")).to_be_hidden()
            self.assertEqual(cluster.evaluate(
                "sessionStorage.getItem('gms_cluster_force_dashboard_once')"
            ), None)
            self.assertNotIn("tab=management", cluster.evaluate("location.search"))

            # The shell shortcut also resets the cluster iframe on reload.
            page.wait_for_function(
                "document.querySelector('#cluster-frame').closest("
                "'.embedded-frame-shell').dataset.frameState === 'ready'"
            )
            cluster.locator('[data-dash-tab="management"]').click()
            page.evaluate("""() => window.dispatchEvent(
                new KeyboardEvent('keydown', {
                    key:'F5',ctrlKey:true,bubbles:true
                })
            )""")
            page.reload(wait_until="domcontentloaded")
            self.close_initial_modals(page)
            cluster = self.frame_for(page, "#cluster-frame")
            expect(cluster.locator('[data-dash-tab="dashboard"]')).to_have_attribute(
                "aria-selected", "true"
            )
            expect(cluster.locator("#tab-management")).to_be_hidden()
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
            # Slow requests must preserve visible table skeletons inside each
            # panel, without the historical fixed, viewport-sized star orbit.
            cluster.locator('[data-dash-tab="management"]').click()
            expect(cluster.locator("#workers")).to_have_attribute(
                "aria-busy", "true"
            )
            expect(cluster.locator("#workers .worker-card-loading")).to_have_count(3)
            self.assertEqual(cluster.locator(".loading-orbit--stage").count(), 0)
            for table_id in ("devices", "jobs", "library", "suites"):
                expect(cluster.locator(f"#{table_id}")).to_have_attribute(
                    "aria-busy", "true"
                )
                expect(cluster.locator(
                    f"#{table_id} .cluster-loading-row"
                )).to_have_count(5)
                expect(cluster.locator(
                    f"#{table_id} .cluster-loading-message"
                )).to_be_visible()
            self.assertEqual(cluster.locator("#workers .cluster-loading-message").count(), 1)
            expect(cluster.locator("#tab-management")).to_be_visible()
            self.assertEqual(
                cluster.evaluate(
                    "() => document.documentElement.scrollHeight - "
                    "document.documentElement.clientHeight"
                ),
                0,
            )
            cluster.locator('[data-dash-tab="dashboard"]').click()
            expect(cluster.locator("#dashboard-stats")).to_have_attribute(
                "aria-busy", "true"
            )
            expect(cluster.locator("#dash-gauges")).to_contain_text(
                "正在读取主机资源"
            )
            expect(cluster.locator("#dashboard-tests")).to_contain_text(
                "正在读取测试状态"
            )
            expect(
                page.locator(
                    ".embedded-frame-shell:has(#cluster-frame) > .embedded-frame-loading"
                )
            ).to_be_hidden()
            self.assert_no_page_errors(page_errors)
        finally:
            page.close()

    def test_cluster_redesign_renders_live_worker_resources_and_device_bars(self):
        page = self.new_page()

        def json_route(payload):
            return lambda route: route.fulfill(
                status=200, content_type="application/json",
                body=json.dumps(payload),
            )

        workers = [
            {
                "id": "ats-worker-controller", "name": "ats-worker-controller",
                "hostname": "controller", "address": "172.16.14.233",
                "status": "online", "cpu_percent": 58.3,
                "memory_percent": 32.5, "memory_total_gb": 64,
                "memory_available_gb": 43.2, "disk_total_gb": 100,
                "disk_free_gb": 20, "disk_percent": 78, "running_jobs": 0,
                "external_jobs": 0, "max_jobs": 6,
                "capabilities": {"ssh_user": "hcq"},
            },
            {
                "id": "ats-worker-244", "name": "ats-worker-244",
                "hostname": "worker", "address": "172.16.14.244",
                "status": "online", "cpu_percent": 4.1,
                "memory_percent": 12.0, "disk_total_gb": 200,
                "disk_free_gb": 100, "running_jobs": 0,
                "external_jobs": 0, "max_jobs": 6,
                "capabilities": {"ssh_user": "hcq"},
            },
        ]
        devices = [
            {"worker_id": "ats-worker-controller", "serial": "RK3326GMS1",
             "state": "external_busy", "transport": "local_usb",
             "properties": {"model": "RK3326"}},
            {"worker_id": "ats-worker-244", "serial": "WLQRK3588EVB0001",
             "state": "available", "transport": "local_usb",
             "properties": {"model": "RK3588"}},
        ]
        page.route("**/api/cluster/workers", json_route({"workers": workers}))
        page.route("**/api/cluster/devices", json_route({"devices": devices}))
        page.route("**/api/cluster/suites", json_route({"suites": []}))
        page.route("**/api/cluster/jobs?*", json_route({"jobs": []}))
        page.route("**/api/cluster/worker-tests", json_route({"tests": []}))
        page.route("**/api/cluster/suite-library", json_route({"archives": []}))
        page.route("**/api/cluster/metrics/history", json_route({"metrics": []}))
        page.route("**/api/cluster/status", json_route({
            "enabled": True, "remote_dispatch_enabled": True,
            "local_worker_id": "ats-worker-controller",
        }))
        page.route("**/api/cluster/workers/*/vpn-status",
                   json_route({"connected": True}))
        page.route("**/api/vpn/status", json_route({"connected": True}))
        try:
            page.goto(f"{self.base_url}/cluster", wait_until="domcontentloaded")
            page.wait_for_function(
                "document.querySelector('#dashboard-stats').getAttribute('aria-busy') === 'false'"
            )
            expect(page.locator("#dashboard-stats .dash-stat-card")).to_have_count(5)
            expect(page.locator("#dash-gauges-chart .dash-resource-table tbody tr")).to_have_count(2)
            expect(page.locator("#dash-device-pie .dist-row")).to_have_count(2)
            expect(page.locator("#dash-device-pie .dist-track .dist-segment")).to_have_count(2)
            page.locator("#dash-gauges-host-tabs button[data-a0='ats-worker-244']").click()
            expect(page.locator("#dash-gauges-chart .dash-resource-table tbody tr")).to_have_count(1)
            expect(page.locator("#dash-gauges-chart")).to_contain_text("4.1%")
            page.locator("#dash-gauges-host-tabs button[data-a0='ats-worker-controller']").click()
            expect(page.locator("#dash-gauges-detail")).to_contain_text("可用内存 43.2 GiB")
            expect(page.locator("#dash-gauges-chart tbody tr td").last).to_have_text("78%")
            page.locator('[data-dash-tab="management"]').click()
            expect(page.locator("#management-stats")).to_have_count(0)
            expect(page.locator("#workers .worker-card")).to_have_count(2)
            self.assertEqual(
                page.locator("#tab-management .cluster-workers-panel .section-head").count(),
                0,
            )
            self.assertEqual(page.locator("#worker-count").count(), 0)
            self.assertEqual(page.locator("#workers .worker-table").count(), 0)
            controller = page.locator("#workers .worker-card").first
            remote = page.locator("#workers .worker-card").nth(1)
            expect(controller.locator(".worker-cpu")).to_contain_text("58.3%")
            expect(controller.locator(".worker-memory-percent")).to_have_text("32.5%")
            expect(controller.locator(".worker-card-icon")).to_be_visible()
            expect(controller.locator(".worker-meter")).to_have_count(3)
            expect(controller.locator(".worker-card-metrics .worker-card-label")).to_have_text(
                ["CPU", "内存", "磁盘", "设备", "套件", "任务"]
            )
            expect(controller.locator(".worker-cpu [role='progressbar']")).to_have_attribute(
                "aria-valuenow", "58.3"
            )
            expect(controller.locator(".worker-memory [role='progressbar']")).to_have_attribute(
                "aria-valuenow", "32.5"
            )
            expect(controller.locator(".worker-memory-detail")).to_have_text(
                "总 64.0 GiB · 可用 43.2 GiB"
            )
            expect(controller.locator(".worker-disk-percent")).to_have_text("78%")
            expect(controller.locator(".worker-disk-detail")).to_have_text("总 100.0 · 可用 20.0 GiB")
            expect(controller.locator(".worker-devices strong")).to_have_text("1")
            expect(controller.locator(".worker-suites strong")).to_have_text("0")
            expect(controller.locator(".worker-jobs strong")).to_have_text("0/6")
            expect(controller.locator('[data-action="restart-vnc"]')).to_be_visible()
            expect(controller.locator('[data-action="local-software"]')).to_be_visible()
            expect(remote.locator(".worker-memory-percent")).to_have_text("—")
            expect(remote.locator(".worker-memory .worker-meter-unknown")).to_have_count(1)
            expect(remote.locator(".worker-disk-percent")).to_have_text("—")
            expect(remote.locator(".worker-disk-detail")).to_have_text("总 200.0 · 可用 100.0 GiB")
            expect(remote.locator(".worker-disk [role='progressbar']")).to_have_attribute(
                "aria-valuetext", "未上报"
            )
            expect(remote.locator(".worker-memory [role='progressbar']")).to_have_attribute(
                "aria-valuetext", "未知"
            )
            for action in ("worker-config", "restart-vnc",
                           "redeploy-worker", "delete-worker"):
                expect(remote.locator(f'[data-action="{action}"]')).to_be_visible()
            self.assertEqual(controller.locator('[data-action="delete-worker"]').count(), 0)
            self.assertEqual(page.locator("#workers .worker-menu-toggle").count(), 0)
            for viewport_width in (1792, 1440, 1000):
                with self.subTest(viewport_width=viewport_width):
                    page.set_viewport_size({"width": viewport_width, "height": 856})
                    metrics = page.locator("#workers .worker-card-grid").evaluate(
                        """grid => {
                            const cards = [...grid.querySelectorAll('.worker-card')];
                            const bounds = cards.map(card => card.getBoundingClientRect());
                            return {
                                top: bounds.map(r => r.top),
                                widths: bounds.map(r => r.width),
                                maxHeight: Math.max(...bounds.map(r => r.height)),
                                inBounds: cards.map(card => {
                                    const rect = card.getBoundingClientRect();
                                    return [...card.querySelectorAll(
                                        '.worker-card-header, .worker-card-state, '
                                        + '.worker-card-metric, .worker-actions button'
                                    )].every(el => {
                                        const item = el.getBoundingClientRect();
                                        return item.top >= rect.top - 1
                                            && item.bottom <= rect.bottom + 1
                                            && item.right <= rect.right + 1;
                                    });
                                }),
                            };
                        }"""
                    )
                    self.assertLess(abs(metrics["top"][0] - metrics["top"][1]),
                                    1, metrics)
                    self.assertLess(abs(metrics["widths"][0] -
                                        metrics["widths"][1]), 2, metrics)
                    self.assertLessEqual(metrics["maxHeight"], 150, metrics)
                    placement = page.locator("#workers .worker-card").first.evaluate(
                        """card => {
                            const rect = selector => card.querySelector(selector)
                                .getBoundingClientRect();
                            const actions = rect('.worker-actions');
                            const identity = rect('.worker-card-identity');
                            const state = rect('.worker-card-state');
                            const metrics = rect('.worker-card-metrics');
                            return {
                                actionsTop: actions.top,
                                identityTop: identity.top,
                                actionsRight: actions.right,
                                cardRight: card.getBoundingClientRect().right,
                                stateTop: state.top, metricsTop: metrics.top,
                            };
                        }"""
                    )
                    self.assertLessEqual(
                        abs(placement["actionsTop"] - placement["identityTop"]),
                        3, placement
                    )
                    self.assertLessEqual(
                        placement["actionsRight"], placement["cardRight"], placement
                    )
                    self.assertLess(
                        placement["actionsTop"], placement["stateTop"], placement
                    )
                    self.assertLess(
                        placement["stateTop"], placement["metricsTop"], placement
                    )
                    self.assertTrue(all(metrics["inBounds"]), metrics)
                    # Controller has 3 actions, remote Worker has 4: both
                    # groups must end at the same card-right padding, without
                    # an empty fourth slot on the Controller.
                    aligned = page.locator("#workers .worker-card-grid").evaluate(
                        """grid => [...grid.querySelectorAll('.worker-card')].map(
                            card => {
                                const rect = el => el.getBoundingClientRect();
                                const centerY = el => {
                                    const r = rect(el);
                                    return r.top + r.height / 2;
                                };
                                const actions = card.querySelector('.worker-actions');
                                const cpu = card.querySelector('.worker-cpu');
                                const memory = card.querySelector('.worker-memory');
                                const diskDetail = card.querySelector('.worker-disk-detail');
                                const cpuLine = cpu.querySelector('.worker-metric-inline');
                                const memoryLine = memory.querySelector('.worker-metric-inline');
                                const lineCenters = line => [
                                    ...line.children
                                ].map(centerY);
                                return {
                                    actionsDisplay: getComputedStyle(actions).display,
                                    actionCount: actions.querySelectorAll('button').length,
                                    rightGap: rect(card).right - rect(actions).right,
                                    cpuCenters: lineCenters(cpuLine),
                                    memoryCenters: lineCenters(memoryLine),
                                    cpuCellWidth: rect(cpu).width,
                                    memoryCellWidth: rect(memory).width,
                                    cpuLineWidth: rect(cpuLine).width,
                                    memoryLineWidth: rect(memoryLine).width,
                                    cpuBarWidth: rect(cpuLine.querySelector('.worker-meter')).width,
                                    memoryBarWidth: rect(memoryLine.querySelector('.worker-meter')).width,
                                    cpuLineRightGap: rect(cpuLine).right -
                                        rect(cpuLine.querySelector('.worker-meter')).right,
                                    memoryLineRightGap: rect(memoryLine).right -
                                        rect(memoryLine.querySelector('.worker-meter')).right,
                                    cpuToMemoryGap: rect(memory).left -
                                        rect(cpuLine.querySelector('.worker-meter')).right,
                                    memoryToDiskGap: rect(card.querySelector('.worker-disk')).left -
                                        rect(memoryLine.querySelector('.worker-meter')).right,
                                    diskBarWidth: rect(card.querySelector('.worker-disk .worker-meter')).width,
                                    diskDetailWidth: [diskDetail.clientWidth, diskDetail.scrollWidth],
                                    cpuTrackDisplay: getComputedStyle(
                                        cpuLine.querySelector('.worker-meter')
                                    ).display,
                                    memoryTrackDisplay: getComputedStyle(
                                        memoryLine.querySelector('.worker-meter')
                                    ).display,
                                    memoryDetailVisible:
                                        memory.querySelector('.worker-memory-detail')
                                            .getBoundingClientRect().height > 0,
                                    metricsRight: rect(card.querySelector('.worker-card-metrics')).right,
                                    cardRight: rect(card).right,
                                };
                            }
                        )"""
                    )
                    self.assertEqual(
                        [item["actionCount"] for item in aligned], [3, 4], aligned
                    )
                    for item in aligned:
                        self.assertEqual(item["actionsDisplay"], "flex", aligned)
                        self.assertGreaterEqual(item["rightGap"], 8, aligned)
                        self.assertLessEqual(item["rightGap"], 16, aligned)
                        self.assertLessEqual(
                            max(item["cpuCenters"]) - min(item["cpuCenters"]),
                            4, aligned
                        )
                        self.assertLessEqual(
                            max(item["memoryCenters"]) - min(item["memoryCenters"]),
                            4, aligned
                        )
                        self.assertLessEqual(
                            abs(item["cpuCenters"][0] - item["memoryCenters"][0]),
                            2, aligned
                        )
                        # Bar tracks should stay long but keep a consistent
                        # breathing gap before the next metric/label.
                        self.assertGreaterEqual(item["cpuLineRightGap"], 9, aligned)
                        self.assertLessEqual(item["cpuLineRightGap"], 11, aligned)
                        self.assertGreaterEqual(item["memoryLineRightGap"], 9, aligned)
                        self.assertLessEqual(item["memoryLineRightGap"], 11, aligned)
                        self.assertGreaterEqual(item["cpuToMemoryGap"], 14, aligned)
                        self.assertLessEqual(item["cpuToMemoryGap"], 24, aligned)
                        self.assertGreaterEqual(item["memoryToDiskGap"], 14, aligned)
                        self.assertLessEqual(item["memoryToDiskGap"], 24, aligned)
                        self.assertEqual(item["cpuTrackDisplay"], "block", aligned)
                        self.assertEqual(item["memoryTrackDisplay"], "block", aligned)
                        # An implicit grid auto-column can shrink the
                        # entire inline row, leaving an invisible CPU rail
                        # and a ~35px RAM rail even when the bar reaches
                        # the end of that incorrectly shrunken row.
                        self.assertLessEqual(
                            abs(item["cpuCellWidth"] - item["cpuLineWidth"]),
                            2, aligned
                        )
                        self.assertLessEqual(
                            abs(item["memoryCellWidth"] - item["memoryLineWidth"]),
                            2, aligned
                        )
                        for track in ("cpuBarWidth", "memoryBarWidth", "diskBarWidth"):
                            self.assertGreaterEqual(item[track], 35, aligned)
                        self.assertTrue(item["memoryDetailVisible"], aligned)
                        self.assertGreaterEqual(*item["diskDetailWidth"], aligned)
                        self.assertLessEqual(
                            item["metricsRight"], item["cardRight"] + 1, aligned
                        )
            page.set_viewport_size({"width": 1792, "height": 856})
            page.evaluate("""() => {
                state.workers.push({
                    ...state.workers[1], id: 'ats-worker-118',
                    name: 'ats-worker-118', address: '172.16.14.118'
                });
                render();
            }""")
            expect(page.locator("#workers .worker-card")).to_have_count(3)
            page.evaluate("""() => {
                state.workers[0].cpu_percent = 120;
                render();
            }""")
            controller = page.locator("#workers .worker-card").first
            expect(controller).to_have_attribute("data-health", "danger")
            expect(controller.locator(".worker-cpu [role='progressbar']")).to_have_attribute(
                "aria-valuenow", "100"
            )
            three = page.locator("#workers .worker-card-grid").evaluate(
                """grid => [...grid.querySelectorAll('.worker-card')].map(
                    card => card.getBoundingClientRect().top
                )"""
            )
            self.assertLess(max(three) - min(three), 1, three)
            self.assertLessEqual(
                page.locator("#tab-management .cluster-workers-panel")
                    .bounding_box()["height"], 154
            )
            self.assertTrue(page.locator("#workers .worker-card-identity").first.is_visible())
            self.assertEqual(page.locator("#workers .worker-card-identity .worker-subline").first.count(), 1)
            self.assertEqual(page.locator("#management-stats").count(), 0)
            expect(page.locator("#cluster-device-table tbody tr")).to_have_count(2)
        finally:
            page.close()

    def test_cluster_worker_row_distinguishes_hidden_external_test(self):
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
            "memory_total_gb": 64,
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
            # Worker 列表横向排列紧凑卡片；诊断信息仍可展开查看。
            frame.locator('[data-dash-tab="management"]').click()
            row = frame.locator("#workers .worker-card").filter(
                has_text="ats-worker-246"
            )
            expect(row.locator(".status.external_busy")).to_have_text("外部占用")
            expect(row.locator(".worker-summary-state")).to_contain_text("外部测试中")
            expect(row.locator(".worker-memory-detail")).to_contain_text(
                "总 64.0 GiB · 可用 56.4 GiB"
            )
            expect(row.locator('[data-action="delete-worker"]')).to_be_disabled()
            row.locator(".worker-meta-details summary").click()
            expect(row.locator(".worker-extra")).to_contain_text("可用内存 56.4 GiB")
            expect(row.locator(".host-warning")).to_contain_text(
                "Tradefed 已 4小时43分钟 未产生输出"
            )
            expect(row.locator(".host-tests")).to_contain_text(
                "检测到 1 个外部测试，详情暂不可用"
            )
            expect(row.locator(".host-tests")).not_to_contain_text("当前无测试")
            expect(frame.locator("#library-worker-0 option").first).to_have_attribute(
                "value", "ats-worker-controller"
            )
            frame.wait_for_function(
                """row => {
                  const tests = row.querySelector('.host-tests');
                  const warning = row.querySelector('.host-warning');
                  return Boolean(
                    tests && warning
                    && (tests.compareDocumentPosition(warning) & Node.DOCUMENT_POSITION_FOLLOWING)
                    && getComputedStyle(tests).borderTopWidth === '1px'
                  );
                }""",
                arg=row.element_handle(),
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
                  document.querySelector('#refresh').click();
                }"""
            )
            button = page.locator("#refresh")
            expect(button).to_be_disabled()
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
            button = page.locator("#refresh")
            expect(button).to_be_enabled()
            # Busy state changes attributes, not the compact button label or width.
            expect(button).to_have_text(re.compile(r"^\s*刷新\s*$"))
            expect(button).not_to_have_attribute("aria-busy", "true")
        finally:
            page.close()

    def test_cluster_refresh_buttons_use_compact_stable_text_width(self):
        page = self.new_page()
        try:
            page.goto(f"{self.base_url}/cluster", wait_until="domcontentloaded")
            page.wait_for_function("typeof loadLibrary === 'function'")
            top = page.locator("#refresh")
            library = page.locator("#reload-library")
            self.assertEqual(top.inner_text(), "刷新")
            self.assertEqual(library.inner_text(), "刷新")
            self.assertEqual(top.locator("svg").count(), 0)
            self.assertEqual(library.locator("svg").count(), 0)
            self.assertEqual(
                library.get_attribute("aria-label"), "刷新测试套件列表"
            )

            for width, height in ((1792, 856), (1280, 800), (390, 740)):
                with self.subTest(width=width):
                    page.set_viewport_size({"width": width, "height": height})
                    top_width_dashboard = top.bounding_box()["width"]
                    page.locator('[data-dash-tab="management"]').click()
                    expect(library).to_be_visible()
                    layout = page.evaluate("""() => {
                        const top = document.getElementById('refresh');
                        const library = document.getElementById('reload-library');
                        const section = library.closest('.section-head');
                        const rect = el => el.getBoundingClientRect();
                        return {
                            topWidth: rect(top).width,
                            libraryWidth: rect(library).width,
                            topMinWidth: getComputedStyle(top).minWidth,
                            libraryMinWidth: getComputedStyle(library).minWidth,
                            libraryRight: rect(library).right,
                            sectionRight: rect(section).right,
                        };
                    }""")
                    self.assertLess(layout["topWidth"], 70, layout)
                    self.assertLess(layout["libraryWidth"], 70, layout)
                    self.assertAlmostEqual(
                        layout["topWidth"], top_width_dashboard, delta=1
                    )
                    self.assertLessEqual(
                        layout["libraryRight"], layout["sectionRight"] + 1,
                        layout,
                    )
                    self.assertEqual(layout["topMinWidth"], "0px")
                    self.assertEqual(layout["libraryMinWidth"], "0px")
                    page.locator('[data-dash-tab="dashboard"]').click()

            page.locator('[data-dash-tab="management"]').click()
            page.wait_for_function("refreshPromise === null")
            page.evaluate("""() => {
                window.__originalLibraryApi = api;
                window.__releaseLibraryLoad = null;
                api = (...args) => args[0] === '/api/cluster/suite-library'
                    ? new Promise(resolve => { window.__releaseLibraryLoad = resolve; })
                    : window.__originalLibraryApi(...args);
                document.querySelector('#reload-library').click();
            }""")
            expect(library).to_be_disabled()
            expect(library).to_have_attribute("aria-busy", "true")
            self.assertEqual(library.inner_text(), "刷新")
            before = library.bounding_box()["width"]
            page.evaluate(
                "() => window.__releaseLibraryLoad({archives: []})"
            )
            expect(library).to_be_enabled()
            expect(library).not_to_have_attribute("aria-busy", "true")
            self.assertEqual(library.inner_text(), "刷新")
            self.assertAlmostEqual(
                library.bounding_box()["width"], before, delta=1
            )
            page.evaluate("() => { api = window.__originalLibraryApi; }")
        finally:
            page.close()

    def test_cluster_tab_row_actions_follow_active_tab(self):
        page = self.new_page()
        try:
            page.goto(f"{self.base_url}/cluster", wait_until="domcontentloaded")
            page.wait_for_function("refreshPromise === null")
            # 概览 Tab：顶部只有刷新；搜索/添加主机隐藏。
            expect(page.locator("#refresh")).to_be_visible()
            expect(page.locator("#cluster-search")).to_be_hidden()
            expect(page.locator("#show-onboarding")).to_be_hidden()
            # 管理 Tab：搜索与添加主机移入 Tab 行并显示。
            page.locator('[data-dash-tab="management"]').click()
            expect(page.locator("#cluster-search")).to_be_visible()
            expect(page.locator("#show-onboarding")).to_be_visible()
            expect(page.locator("#refresh")).to_be_visible()
        finally:
            page.close()
