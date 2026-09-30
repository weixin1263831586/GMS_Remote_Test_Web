"""Host workspace selection, scope, desktop and connection lifecycle."""

import json
import re

from tests.runtime_ui.harness import RuntimeUiHarness, expect


class RuntimeWorkspacesTests(RuntimeUiHarness):
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

    def test_enabling_cluster_refreshes_stale_desktop_host_directory_without_navigation(self):
        page = self.new_page()
        host_requests = []

        def serve_cluster_hosts(route):
            host_requests.append(route.request.url)
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({
                    "success": True,
                    "hosts": [{
                        "worker_id": "ats-worker-controller",
                        "address": "127.0.0.1",
                        "ssh_user": "ui-smoke",
                        "status": "online",
                    }, {
                        "worker_id": "worker-a",
                        "address": "192.0.2.10",
                        "ssh_user": "worker-a",
                        "status": "online",
                    }],
                }),
            )

        page.route("**/api/cluster/hosts", serve_cluster_hosts)
        try:
            self.goto_shell(page)
            page.wait_for_function(
                "typeof toggleClusterMode === 'function' "
                "&& typeof refreshClusterHostDirectory === 'function'"
            )
            host_requests.clear()
            result = page.evaluate(
                """
                async () => {
                    const originals = {
                        loadClusterWorkers,
                        loadDevices,
                        loadTestSuites,
                        loadTestReports,
                        mountHostWorkspacePane,
                        showToast,
                    };
                    let workerRefreshForce = null;
                    try {
                        loadClusterWorkers = async force => {
                            workerRefreshForce = force;
                            return [];
                        };
                        loadDevices = async () => [];
                        loadTestSuites = async () => [];
                        loadTestReports = async () => [];
                        mountHostWorkspacePane = () => {};
                        showToast = () => {};

                        state.clusterStatus = {
                            enabled: true,
                            local_worker_id: 'ats-worker-controller',
                        };
                        window.GmsWorkspace.update({
                            scope_mode: 'single',
                            worker_id: 'ats-worker-controller',
                            device_ids: [],
                        }, {source: 'test-setup', persist: false});
                        desktopHosts = [{
                            id: 'default',
                            worker_id: 'ats-worker-controller',
                            name: 'Controller',
                            connection: 'ui-smoke@127.0.0.1',
                        }];
                        currentHost = desktopHosts[0];
                        clusterHostDirectory = {
                            hosts: [{
                                worker_id: 'ats-worker-controller',
                                address: '127.0.0.1',
                                ssh_user: 'ui-smoke',
                                status: 'online',
                            }],
                            loadedAt: Date.now(),
                            promise: null,
                        };
                        currentPage = 'desktop';
                        window.hostWorkspaceInitialized = true;
                        window.terminalWorkspaceInitialized = false;
                        hostWorkspaceScopeModeInitialized = true;
                        hostWorkspaceClusterEnabled = false;
                        hostWorkspace.layout = 'single';
                        hostWorkspace.panes = [{type: 'desktop', hostId: 'default'}];
                        hostWorkspace.maximized = null;
                        hostWorkspace.clusterState = {
                            layout: 'single',
                            panes: [{type: 'desktop', hostId: 'default'}],
                            maximized: null,
                        };
                        hostWorkspace.instances.clear();
                        hostWorkspace.renderedSignature = null;
                        document.getElementById('host-workspace-grid').replaceChildren();
                        renderHostWorkspace();

                        const paneBefore = document.querySelector('[data-workspace-pane="0"]');
                        const body = document.getElementById('host-workspace-body-0');
                        const retainedFrame = document.createElement('iframe');
                        body.replaceChildren(retainedFrame);
                        hostWorkspace.instances.set(0, {
                            type: 'desktop',
                            hostId: 'default',
                            frame: retainedFrame,
                            disposed: false,
                        });

                        await toggleClusterMode();

                        const select = document.querySelector(
                            '[data-workspace-pane="0"] select[aria-label="主机"]'
                        );
                        return {
                            workerRefreshForce,
                            scopeMode: window.GmsWorkspace.get().scope_mode,
                            optionValues: Array.from(select.options).map(option => option.value),
                            paneRetained: paneBefore === document.querySelector('[data-workspace-pane="0"]'),
                            frameRetained: retainedFrame === document.querySelector('#host-workspace-body-0 iframe'),
                        };
                    } finally {
                        loadClusterWorkers = originals.loadClusterWorkers;
                        loadDevices = originals.loadDevices;
                        loadTestSuites = originals.loadTestSuites;
                        loadTestReports = originals.loadTestReports;
                        mountHostWorkspacePane = originals.mountHostWorkspacePane;
                        showToast = originals.showToast;
                    }
                }
                """
            )

            self.assertTrue(result["workerRefreshForce"])
            self.assertEqual(result["scopeMode"], "cluster")
            self.assertIn("cluster:worker-a", result["optionValues"])
            self.assertTrue(result["paneRetained"])
            self.assertTrue(result["frameRetained"])
            self.assertEqual(len(host_requests), 1)
        finally:
            page.close()

    def test_cluster_worker_heartbeat_invalidates_missing_host_directory_entry(self):
        page = self.new_page()
        try:
            self.goto_shell(page)
            page.wait_for_function("typeof handleServerEvent === 'function'")
            calls = page.evaluate(
                """
                () => {
                    const originalRefresh = window.refreshClusterHostDirectoryForWorker;
                    const originalPage = currentPage;
                    const originalContext = window.GmsWorkspace.get();
                    const refreshCalls = [];
                    try {
                        window.GmsWorkspace.update({
                            scope_mode: 'cluster',
                            worker_id: 'ats-worker-controller',
                        }, {source: 'test-setup', persist: false});
                        currentPage = 'reports';
                        window.refreshClusterHostDirectoryForWorker = async (...args) => {
                            refreshCalls.push(args);
                        };
                        handleServerEvent('worker.updated', {
                            worker_id: 'worker-late',
                            status: 'online',
                        });
                        return refreshCalls;
                    } finally {
                        window.refreshClusterHostDirectoryForWorker = originalRefresh;
                        currentPage = originalPage;
                        window.GmsWorkspace.update(originalContext, {
                            source: 'test-cleanup',
                            persist: false,
                        });
                    }
                }
                """
            )
            self.assertEqual(calls, [["worker-late", "online"]])
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
                            f"{page_selector} .host-workspace-single-bar "
                            ".host-workspace-refresh-btn"
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
                        cluster_header = page.locator(
                            f"{page_selector} .host-workspace-pane-header"
                        ).first
                        header_box = cluster_header.bounding_box()
                        select_box = cluster_header.locator("select").bounding_box()
                        maximize_box = cluster_header.locator("button").last.bounding_box()
                        self.assertIsNotNone(header_box)
                        self.assertIsNotNone(select_box)
                        self.assertIsNotNone(maximize_box)
                        # ba3be31 为触屏可达性把簇模式 header 控件从 22px 提到
                        # 24px（header = 24 内容 + 4 padding + 1 边框 = 29），
                        # 同步契约：header ≤ 29.5、select/按钮高 24。
                        self.assertLessEqual(header_box["height"], 29.5)
                        self.assertAlmostEqual(select_box["height"], 24, delta=0.5)
                        self.assertAlmostEqual(maximize_box["height"], 24, delta=0.5)
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

    def test_test_suite_cluster_controls_wait_for_scope_before_becoming_visible(self):
        def workspace_context_handler(selected_scope, json_response):
            def handler(route):
                json_response(route, {
                    "success": True,
                    "data": {"context": {
                        "scope_mode": selected_scope,
                        "worker_id": (
                            "worker-a"
                            if selected_scope == "cluster"
                            else "ats-worker-controller"
                        ),
                        "device_ids": [],
                    }},
                })

            return handler

        for scope_mode in ("single", "cluster"):
            with self.subTest(scope_mode=scope_mode):
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
                    "**/api/users/workspace-context",
                    workspace_context_handler(scope_mode, json_response),
                )
                page.add_init_script(
                    """
                    localStorage.setItem('gms_current_page','test-suites');
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
                )
                try:
                    elevated = page.request.post(
                        f"{self.base_url}/api/auth/elevate",
                        data={"username": "ui-admin", "password": "UiSmokeAdmin-2026!"},
                    )
                    self.assertTrue(elevated.ok, elevated.text())
                    page.goto(self.base_url, wait_until="domcontentloaded")

                    expect(page.locator("#page-test-suites")).to_have_class(
                        re.compile(r"\bactive\b")
                    )
                    expect(page.locator("body")).to_have_class(
                        re.compile(r"workspace-scope-pending")
                    )
                    expect(page.locator("#btn-copy-test-report")).to_be_hidden()
                    expect(page.locator("#suite-worker-select")).to_be_hidden()

                    expect(page.locator("body")).to_have_class(
                        re.compile(rf"workspace-scope-{scope_mode}")
                    )
                    if scope_mode == "cluster":
                        expect(page.locator("#btn-copy-test-report")).to_be_visible()
                        expect(page.locator("#suite-worker-select")).to_be_visible()
                    else:
                        expect(page.locator("#btn-copy-test-report")).to_be_hidden()
                        expect(page.locator("#suite-worker-select")).to_be_hidden()
                finally:
                    page.close()

    def test_scope_initialization_preserves_pending_adb_target(self):
        page = self.new_page()
        try:
            self.goto_shell(page)
            result = page.evaluate(
                """() => {
                  const localWorkerId = workspaceLocalWorkerId();
                  desktopHosts = [{
                    id: 'default',
                    worker_id: localWorkerId,
                    connection: 'ui-smoke@127.0.0.1',
                  }];
                  hostWorkspaceScopeModeInitialized = false;
                  hostWorkspaceClusterEnabled = false;
                  window.terminalWorkspaceInitialized = false;
                  terminalWorkspace.panes = [{
                    hostId: 'default',
                    mode: 'adb',
                    serialNo: 'PENDING-ADB-1',
                    workerId: localWorkerId,
                  }];
                  terminalWorkspace.clusterState = {
                    layout: 'single',
                    panes: [{hostId: 'default'}],
                    maximized: null,
                  };
                  applyHostWorkspaceScopeMode(true);
                  return terminalWorkspace.panes[0];
                }"""
            )
            self.assertEqual(
                result,
                {
                    "hostId": "default",
                    "mode": "adb",
                    "serialNo": "PENDING-ADB-1",
                    "workerId": page.evaluate("workspaceLocalWorkerId()"),
                },
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
