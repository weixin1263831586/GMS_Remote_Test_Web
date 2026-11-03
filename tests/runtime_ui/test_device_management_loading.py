"""A delayed initial workspace must not leave the inventory on its loading row."""

from tests.runtime_ui.harness import RuntimeUiHarness


class RuntimeDeviceManagementLoadingTests(RuntimeUiHarness):
    def test_unavailable_cluster_keeps_saved_mode_and_target(self):
        page = self.new_page()
        try:
            self.goto_shell(page)
            result = page.evaluate("""async () => {
                await GmsWorkspace.ready;
                const workspace = window.GmsWorkspace;
                const fetchOriginal = window.fetch;
                const previous = workspace.get();
                const json = data => Promise.resolve(new Response(JSON.stringify(data), {
                    status: 200, headers: {'Content-Type': 'application/json'}
                }));
                const disabled = {success: true, enabled: false, local_worker_id: 'ats-worker-controller'};
                window.GmsWorkspace = {...workspace, loadClusterStatus: async () => disabled};
                window.fetch = (input, options) => {
                    const path = new URL(String(input), location.origin).pathname;
                    if (path === '/api/cluster/status') return json(disabled);
                    if (path === '/api/cluster/hosts') return json({success: true, hosts: []});
                    if (['/api/devices/management', '/api/cluster/devices'].includes(path)) return json({success: true, devices: []});
                    return fetchOriginal(input, options);
                };
                try {
                    workspace.update({scope_mode: 'cluster', worker_id: 'worker-saved'}, {persist: false});
                    _clusterModeInitialized = false;
                    await initializeClusterMode();
                    currentPage = 'devices';
                    let error = '';
                    try { await loadDevicesManagement(); } catch (failure) { error = failure.message; }
                    return {
                        scope: workspace.get().scope_mode, worker: workspaceWorkerId(), error,
                        body: document.body.className,
                        text: document.getElementById('devices-table-body').textContent
                    };
                } finally {
                    stopDevicesAutoRefresh();
                    window.GmsWorkspace = workspace;
                    window.fetch = fetchOriginal;
                    currentPage = 'test';
                    workspace.update(previous, {persist: false});
                    _clusterModeInitialized = false;
                }
            }""")
            self.assertEqual(result['scope'], 'cluster')
            self.assertEqual(result['worker'], 'worker-saved')
            self.assertIn('workspace-scope-cluster', result['body'])
            self.assertIn('集群服务不可用', result['error'])
            self.assertIn('设备列表加载失败', result['text'])
        finally:
            page.close()

    def test_mode_load_failure_does_not_fall_back_to_single_or_request_inventory(self):
        page = self.new_page()
        page.add_init_script("""(() => {
            const original = window.fetch.bind(window);
            window.__inventoryRequests = [];
            window.fetch = (input, options) => {
                const path = new URL(String(input), location.origin).pathname;
                if (path === '/api/users/workspace-context' && (!options?.method || options.method === 'GET')) {
                    return Promise.resolve(new Response('{}', {status: 503}));
                }
                if (['/api/devices/management', '/api/devices/list', '/api/cluster/devices'].includes(path)) {
                    window.__inventoryRequests.push(path);
                }
                return original(input, options);
            };
        })();""")
        try:
            page.goto(self.base_url + '/#devices', wait_until='domcontentloaded')
            page.wait_for_function("document.getElementById('cluster-mode-label').textContent.includes('模式读取失败')")
            result = page.evaluate('({scope: GmsWorkspace.get().scope_mode, requests: window.__inventoryRequests, body: document.body.className})')
            self.assertIsNone(result['scope'])
            self.assertEqual(result['requests'], [])
            self.assertIn('workspace-scope-pending', result['body'])
            page.wait_for_function("document.getElementById('devices-table-body').textContent.includes('设备列表加载失败')")
        finally:
            page.close()

    def test_saved_mode_is_unknown_until_loaded_and_survives_direct_entry_and_reload(self):
        for mode in ('single', 'cluster'):
            with self.subTest(mode=mode):
                page = self.new_page()
                page.add_init_script(
                    """(() => {
                        const fetchOriginal = window.fetch.bind(window);
                        window.__inventoryRequests = [];
                        let release;
                        const gate = new Promise(resolve => { release = resolve; });
                        window.__releaseWorkspace = () => release();
                        const json = data => new Response(JSON.stringify(data), {
                            status: 200, headers: {'Content-Type': 'application/json'}
                        });
                        window.fetch = async (input, options) => {
                            const path = new URL(String(input), location.origin).pathname;
                            if (path === '/api/users/workspace-context' && (!options?.method || options.method === 'GET')) {
                                window.__workspaceRequested = true;
                                await gate;
                                return json({success: true, data: {context: {
                                    scope_mode: MODE, worker_id: MODE === 'cluster' ? 'worker-a' : 'ats-worker-controller', device_ids: []
                                }}});
                            }
                            if (path === '/api/cluster/status') return json({
                                success: true, enabled: true, local_worker_id: 'ats-worker-controller', workers: []
                            });
                            if (['/api/devices/management', '/api/devices/list', '/api/cluster/devices'].includes(path)) {
                                window.__inventoryRequests.push({path, scope: window.GmsWorkspace?.get?.().scope_mode});
                                if (path === '/api/cluster/devices') return json({success: true, devices: [{
                                    id: 'worker-a:REMOTE-SAVED', worker_id: 'worker-a', serial: 'REMOTE-SAVED', state: 'available', properties: {}
                                }]});
                                const devices = [{device_id: 'LOCAL-SAVED', serial_no: 'LOCAL-SAVED', status: 'online', source_type: 'local'}];
                                return path === '/api/devices/list' ? json({success: true, data: devices}) : json({success: true, devices});
                            }
                            if (path === '/api/cluster/hosts') return json({success: true, hosts: []});
                            if (path === '/api/device-groups') return json({success: true, data: {groups: []}});
                            return fetchOriginal(input, options);
                        };
                    })();""".replace('MODE', repr(mode))
                )
                try:
                    page.goto(self.base_url + '/#devices', wait_until='domcontentloaded')
                    for refresh in (False, True):
                        if refresh:
                            page.reload(wait_until='domcontentloaded')
                        page.wait_for_function('window.__workspaceRequested && window.GmsWorkspace')
                        pending = page.evaluate('({scope: GmsWorkspace.get().scope_mode, requests: window.__inventoryRequests})')
                        self.assertIsNone(pending['scope'])
                        self.assertEqual(pending['requests'], [])
                        page.evaluate('window.__releaseWorkspace()')
                        page.wait_for_function("document.getElementById('devices-table-body').textContent.includes('LOCAL-SAVED')")
                        result = page.evaluate("({scope: GmsWorkspace.get().scope_mode, requests: window.__inventoryRequests, text: document.getElementById('devices-table-body').textContent})")
                        self.assertEqual(result['scope'], mode)
                        self.assertTrue(all(request['scope'] == mode for request in result['requests']))
                        self.assertEqual('REMOTE-SAVED' in result['text'], mode == 'cluster')
                        self.assertNotIn('加载中', result['text'])
                finally:
                    page.close()

    def test_device_list_waits_for_initial_server_scope_and_renders_without_an_event(self):
        page = self.new_page()
        try:
            self.goto_shell(page)
            result = page.evaluate(
                """async () => {
                    await GmsWorkspace.ready;
                    stopDevicesAutoRefresh();
                    if (devicesManagementLoadPromise) await devicesManagementLoadPromise;
                    const originalWorkspace = window.GmsWorkspace;
                    const originalFetch = window.fetch;
                    const originalPage = currentPage;
                    let resolveReady;
                    let context = {scope_mode: 'single'};
                    const ready = new Promise(resolve => { resolveReady = resolve; });
                    let managementRequests = 0;
                    let clusterRequests = 0;
                    const json = payload => Promise.resolve(new Response(JSON.stringify(payload), {
                        status: 200, headers: {'Content-Type': 'application/json'}
                    }));
                    window.GmsWorkspace = {...originalWorkspace, ready, get: () => context};
                    window.fetch = (input, options) => {
                        const path = new URL(String(input), location.origin).pathname;
                        if (path === '/api/devices/management') {
                            managementRequests += 1;
                            return json({success: true, devices: [{
                                device_id: 'LOCAL-READY', status: 'online', source_type: 'local'
                            }]});
                        }
                        if (path === '/api/cluster/devices') {
                            clusterRequests += 1;
                            return json({success: true, devices: [{
                                id: 'remote:REMOTE-READY', serial: 'REMOTE-READY',
                                worker_id: 'remote', state: 'available', properties: {}
                            }]});
                        }
                        if (path === '/api/cluster/status') return json({
                            success: true, enabled: true, local_worker_id: 'ats-worker-controller'
                        });
                        if (path === '/api/cluster/hosts') return json({success: true, hosts: []});
                        if (path === '/api/device-groups') return json({success: true, data: {groups: []}});
                        return originalFetch(input, options);
                    };
                    currentPage = 'devices';
                    try {
                        const pending = loadDevicesManagement();
                        await new Promise(resolve => setTimeout(resolve, 25));
                        const beforeReady = managementRequests;
                        // Server initialization replaces context; it does not
                        // dispatch the event used by subsequent mode switches.
                        context = {scope_mode: 'cluster', worker_id: 'remote'};
                        resolveReady(context);
                        await pending;
                        return {
                            beforeReady, managementRequests, clusterRequests,
                            ids: allDevices.map(device => device.device_id),
                            text: document.getElementById('devices-table-body').textContent,
                            refreshing: Boolean(devicesRefreshInterval)
                        };
                    } finally {
                        stopDevicesAutoRefresh();
                        window.fetch = originalFetch;
                        window.GmsWorkspace = originalWorkspace;
                        currentPage = originalPage;
                    }
                }"""
            )
            self.assertEqual(result['beforeReady'], 0)
            self.assertEqual(result['managementRequests'], 1)
            self.assertEqual(result['clusterRequests'], 1)
            self.assertIn('remote:REMOTE-READY', result['ids'])
            self.assertIn('REMOTE-READY', result['text'])
            self.assertNotIn('加载中', result['text'])
            self.assertTrue(result['refreshing'])
        finally:
            page.close()
