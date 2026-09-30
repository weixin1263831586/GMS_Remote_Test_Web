"""Terminal and serial-console sessions, input and permissions."""

import json
import re
import time

from tests.runtime_ui.harness import RuntimeUiHarness, expect


class RuntimeTerminalTests(RuntimeUiHarness):
    def test_devices_console_tabs_support_keyboard_navigation(self):
        page = self.new_page()

        def fulfill_ports(route):
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"success": True, "data": {"ports": [{
                    "port_key": "ttyUSB0",
                    "devname": "/dev/ttyUSB0",
                    "online": True,
                    "binding": {
                        "label": "Keyboard test device",
                        "baudrate": 115200,
                        "capture_enabled": False,
                    },
                }]}}),
            )

        page.route("**/api/devices/console/ports", fulfill_ports)
        page.route(
            "**/api/devices/console/ports/ttyUSB0/logs?*",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body='{"success":true,"data":{"content":"","available_dates":[]}}',
            ),
        )
        try:
            page.goto(f"{self.base_url}/devices-console", wait_until="domcontentloaded")
            page.get_by_role("button", name="打开控制台").click()
            expect(page.locator("#console-section")).to_be_visible()

            page.locator(".console-tab").focus()
            page.locator(".console-tab").press("ArrowLeft")
            expect(page.locator("#ports-section")).to_be_visible()
            page.locator("#ports-tab").press("ArrowRight")
            expect(page.locator("#console-section")).to_be_visible()
            page.locator(".console-tab").press("Home")
            expect(page.locator("#ports-section")).to_be_visible()
        finally:
            page.close()

    def test_devices_console_tabs_keep_multiple_console_sessions(self):
        page = self.new_page()

        def fulfill_ports(route):
            ports = [
                {
                    "port_key": key,
                    "devname": f"/dev/{key}",
                    "online": True,
                    "binding": {
                        "label": f"Device {key}",
                        "baudrate": 115200,
                        "capture_enabled": False,
                    },
                }
                for key in ("ttyUSB0", "ttyUSB1")
            ]
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"success": True, "data": {"ports": ports}}),
            )

        page.route("**/api/devices/console/ports", fulfill_ports)
        page.route(
            "**/api/devices/console/ports/*/logs?*",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body='{"success":true,"data":{"content":"","available_dates":[]}}',
            ),
        )
        try:
            page.goto(f"{self.base_url}/devices-console", wait_until="domcontentloaded")
            expect(page.get_by_role("button", name="打开控制台")).to_have_count(2)
            page.locator(".port-card").nth(0).get_by_role(
                "button", name="打开控制台"
            ).click()
            expect(page.locator(".console-tab")).to_have_count(1)
            # 控制台1激活时本机串口视图被隐藏，先切回再打开第二个控制台。
            page.locator("#ports-tab").click()
            expect(page.locator("#ports-section")).to_be_visible()
            page.locator(".port-card").nth(1).get_by_role(
                "button", name="打开控制台"
            ).click()
            tabs = page.locator(".console-tab")
            expect(tabs).to_have_count(2)
            expect(tabs.nth(0)).to_contain_text("控制台1")
            expect(tabs.nth(1)).to_contain_text("控制台2")
            expect(tabs.nth(1)).to_have_class(re.compile(r"\bactive\b"))
            # 切回控制台1：各会话面板独立保留，切换不互相覆盖。
            tabs.nth(0).click()
            expect(tabs.nth(0)).to_have_class(re.compile(r"\bactive\b"))
            expect(page.locator("#console-pane-1")).to_be_visible()
            expect(page.locator("#console-pane-2")).to_be_hidden()
            # 关闭当前 tab 后自动切到剩余控制台。
            page.locator("#console-pane-1 .close-console").click()
            expect(page.locator(".console-tab")).to_have_count(1)
            expect(page.locator(".console-tab").nth(0)).to_have_class(
                re.compile(r"\bactive\b")
            )
            expect(page.locator("#console-pane-2")).to_be_visible()
            # 关闭最后一个控制台回到本机串口视图。
            page.locator("#console-pane-2 .close-console").click()
            expect(page.locator(".console-tab")).to_have_count(0)
            expect(page.locator("#ports-section")).to_be_visible()
        finally:
            page.close()

    def test_devices_console_prompts_permission_for_plain_user_role(self):
        page = self.new_page()
        # 普通 user 角色没有 devices.inventory：绑定/采集按钮保持可见，点击
        # 时给出明确的权限提示而非裸 403；打开控制台与终端只读输出保持可用，
        # 清空日志仍禁用（带原因 title）。服务端 403 始终是安全边界。
        page.route(
            "**/api/auth/status",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({
                    "authenticated": True,
                    "auth_required": True,
                    "elevated": False,
                    "user": {
                        "id": "u1", "username": "op", "role": "user",
                        "permissions": ["tests.execute", "jobs.read"],
                    },
                }),
            ),
        )

        def fulfill_ports(route):
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"success": True, "data": {"ports": [{
                    "port_key": "ttyUSB0",
                    "devname": "/dev/ttyUSB0",
                    "online": True,
                    "binding": {
                        "label": "RO device",
                        "baudrate": 115200,
                        "capture_enabled": False,
                    },
                }]}}),
            )

        page.route("**/api/devices/console/ports", fulfill_ports)
        page.route(
            "**/api/devices/console/ports/*/logs?*",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body='{"success":true,"data":{"content":"","available_dates":[]}}',
            ),
        )
        try:
            page.goto(f"{self.base_url}/devices-console", wait_until="domcontentloaded")
            card = page.locator(".port-card").first
            expect(card.get_by_role("button", name="打开控制台")).to_have_count(1)
            expect(card.get_by_role("button", name="启动采集")).to_have_count(1)
            expect(card.get_by_role("button", name="编辑绑定")).to_have_count(1)
            # 无权限点击写操作：弹明确提示，不打开绑定弹框。
            card.get_by_role("button", name="编辑绑定").click()
            page_notice = page.locator("#page-notice")
            expect(page_notice).to_be_visible()
            expect(page_notice).to_contain_text("权限")
            expect(page.locator("#binding-modal")).to_be_hidden()
            card.get_by_role("button", name="打开控制台").click()
            expect(page.locator(".console-tab")).to_have_count(1)
            clear_logs = page.locator(".clear-logs")
            expect(clear_logs).to_be_disabled()
        finally:
            page.close()

    def test_serial_console_tabs_and_bottom_input_layout(self):
        page = self.new_page()
        legacy_order = [
            'test', 'desktop', 'terminal', 'users', 'devices', 'reports',
            'report-analysis', 'apk-analysis', 'test-suites', 'api-docs',
            'architecture', 'websites', 'tools', 'security-audit', 'gms-assistant',
            'automation', 'cluster', 'devices-console', 'redmine-agent',
            'gerrit-dashboard', 'notes', 'agent',
        ]
        page.route(
            "**/api/sidebar-order",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({
                    "success": True,
                    "data": {"order": legacy_order, "visible_pages": legacy_order},
                }),
            ),
        )
        port = {
            "port_key": "usb-FTDI_TEST-if00-port0",
            "identity_stable": True,
            "devname": "/dev/ttyUSB0",
            "by_id": "/dev/serial/by-id/usb-FTDI_TEST-if00-port0",
            "vendor_product": "0403:6001",
            "driver": "ftdi_sio",
            "online": True,
            "capture_enabled": False,
            "capture_active": False,
            "last_output_at": "",
            "error": "",
            "binding": {
                "label": "RK3562GMS3",
                "device_id": "RK3562GMS3",
                "worker_id": "ats-worker-controller",
                "identity_verified": True,
                "binding_version": 2,
                "baudrate": 1500000,
                "capture_enabled": False,
                "newline": "cr",
                "note": "",
            },
        }
        page.route(
            "**/api/devices/console/ports",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"success": True, "data": {"ports": [port]}}),
            ),
        )
        page.route(
            "**/api/devices/management",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({
                    "success": True,
                    "source": "local",
                    "devices": [{"device_id": "RK3562GMS3", "model": "rk3562"}],
                }),
            ),
        )
        page.route(
            "**/api/devices/console/ports/*/logs?*",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({
                    "success": True,
                    "data": {"date": "20260910", "content": "", "available_dates": []},
                }),
            ),
        )
        page.route(
            "**/api/users/detect",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body='{"success":true,"username":"ui-smoke"}',
            ),
        )
        deleted_bindings = []

        def delete_serial_binding(route):
            deleted_bindings.append(route.request.url)
            port["binding"] = None
            route.fulfill(
                status=200,
                content_type="application/json",
                body='{"success":true,"data":{"port_key":"usb-FTDI_TEST-if00-port0"}}',
            )

        page.route("**/api/devices/console/bindings/*", delete_serial_binding)
        try:
            self.goto_shell(page)
            page.evaluate("switchPage('devices-console', null)")
            frame = self.frame_for(page, "#devices-console-frame")
            expect(frame.locator("#ports-tab")).to_have_class(re.compile(r"active"))
            expect(frame.locator("#ports-section")).to_be_visible()
            toast_style = frame.locator("#page-notice").evaluate(
                """element => {
                    const style = getComputedStyle(element);
                    return {position: style.position, top: style.top, left: style.left};
                }"""
            )
            self.assertEqual(toast_style["position"], "fixed")
            self.assertEqual(toast_style["top"], "50%")
            self.assertEqual(toast_style["left"], "50%")
            frame.get_by_role("button", name="编辑绑定").click()
            expect(frame.locator("#binding-label")).to_have_value("RK3562GMS3")
            self.assertEqual(frame.locator("#binding-label").evaluate("node => node.tagName"), "SELECT")
            frame.locator("#cancel-binding").click()
            frame.get_by_role("button", name="打开控制台").click()
            expect(frame.locator("#console-section")).to_be_visible()
            expect(frame.locator(".console-tab")).to_have_class(re.compile(r"active"))
            self.assertTrue(frame.locator(".terminal-column").evaluate(
                """column => {
                    const dock = column.querySelector('.input-dock');
                    const columnBox = column.getBoundingClientRect();
                    const dockBox = dock.getBoundingClientRect();
                    return Math.abs(columnBox.bottom - dockBox.bottom) <= 2;
                }"""
            ))
            self.assertEqual(
                frame.evaluate(
                    "JSON.parse(sessionStorage.getItem('gms_serial_console_workspace_v1')).portKeys"
                ),
                [port["port_key"]],
            )
            page.reload(wait_until="domcontentloaded")
            frame = self.frame_for(page, "#devices-console-frame")
            expect(frame.locator(".console-tab")).to_have_count(1)
            expect(frame.locator("#console-section")).to_be_visible()
            frame.locator("#ports-tab").click()
            expect(frame.locator("#ports-section")).to_be_visible()
            expect(frame.locator("#console-section")).to_be_hidden()

            port["online"] = False
            frame.locator("#refresh-ports").click()
            expect(frame.locator(".port-state")).to_have_text("离线")
            frame.get_by_role("button", name="编辑绑定").click()
            page.once("dialog", lambda dialog: dialog.accept())
            frame.locator("#delete-binding").click()
            expect(frame.locator(".console-tab")).to_have_count(0)
            expect(frame.get_by_role("button", name="绑定", exact=True)).to_be_visible()
            self.assertEqual(len(deleted_bindings), 1)
            self.assertEqual(
                frame.evaluate(
                    "JSON.parse(sessionStorage.getItem('gms_serial_console_workspace_v1')).portKeys"
                ),
                [],
            )

            page.locator(".sidebar-brand").click()
            values = page.locator(
                "#sidebar-visibility-list input[type=checkbox]"
            ).evaluate_all("nodes => nodes.map(node => node.value)")
            self.assertEqual(values[values.index("devices") + 1], "devices-console")
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

    def test_expired_terminal_elevation_prompts_and_reconnects_after_auth(self):
        page = self.new_page()
        elevation_requests = []

        def grant_elevation(route):
            elevation_requests.append(route.request.post_data_json)
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(
                    {
                        "success": True,
                        "elevated": True,
                        "elevated_until": "2099-01-01T00:00:00+00:00",
                        "admin_verified": True,
                        "user": {
                            "id": "ui-admin",
                            "username": "ui-admin",
                            "role": "admin",
                            "display_name": "UI Smoke Admin",
                        },
                        "client_id": "ui-admin",
                    }
                ),
            )

        page.route("**/api/auth/elevate", grant_elevation)
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
            self.assertEqual(len(elevation_requests), 1)
            self.assertEqual(elevation_requests[0].get("username"), "ui-admin")
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

    def test_adb_terminal_startup_output_is_revealed_once(self):
        page = self.new_page()
        try:
            self.goto_shell(page)
            result = page.evaluate(
                """() => {
                  const writes = [];
                  const instance = {
                    mode: 'adb',
                    serialNo: 'DEVICE-1',
                    shellReady: false,
                    initialData: '',
                    terminal: {write: data => writes.push(data)},
                  };
                  writeTerminalWorkspaceData(
                    instance,
                    'Welcome to host\\r\\nhost@controller:~$ clear\\r\\n'
                      + '\\x1b[H\\x1b[2Jhost@controller:~$ adb -s DEVICE-1 shell\\r\\n'
                  );
                  const writesBeforeDevicePrompt = writes.length;
                  writeTerminalWorkspaceData(instance, 'device_name:/ $ ');
                  writeTerminalWorkspaceData(instance, 'id\\r\\nuid=2000(shell)\\r\\ndevice_name:/ $ ');
                  return {
                    writes,
                    writesBeforeDevicePrompt,
                    shellReady: instance.shellReady,
                    initialData: instance.initialData,
                  };
                }"""
            )

            self.assertEqual(result["writesBeforeDevicePrompt"], 0)
            self.assertTrue(result["shellReady"])
            self.assertEqual(result["initialData"], "")
            self.assertEqual(len(result["writes"]), 2)
            startup_output = result["writes"][0]
            self.assertIn("ADB Shell · DEVICE-1", startup_output)
            self.assertIn("device_name:/ $", startup_output)
            self.assertNotIn("Welcome to host", startup_output)
            self.assertNotIn("adb -s", startup_output)
            self.assertEqual(
                result["writes"][1],
                "id\r\nuid=2000(shell)\r\ndevice_name:/ $ ",
            )
        finally:
            page.close()

    def test_adb_terminal_startup_failure_is_visible_and_closes_host_shell(self):
        page = self.new_page()
        try:
            self.goto_shell(page)
            result = page.evaluate(
                """() => {
                  const writes = [];
                  let closeCount = 0;
                  const instance = {
                    mode: 'adb',
                    paneIndex: 0,
                    serialNo: 'DEVICE-1',
                    shellReady: false,
                    startupFailed: false,
                    initialData: '',
                    socket: {
                      readyState: WebSocket.OPEN,
                      close: () => { closeCount += 1; },
                    },
                    terminal: {write: data => writes.push(data)},
                  };
                  writeTerminalWorkspaceData(
                    instance,
                    'Welcome to host\\r\\nhost@controller:~$ '
                      + 'adb -s DEVICE-1 shell\\r\\n'
                      + 'error: device unauthorized\\r\\nhost@controller:~$ '
                  );
                  writeTerminalWorkspaceData(instance, 'host-only-output');
                  return {
                    writes,
                    closeCount,
                    startupFailed: instance.startupFailed,
                    shellReady: instance.shellReady,
                    initialData: instance.initialData,
                    startupFailureStatus: instance.startupFailureStatus,
                  };
                }"""
            )

            self.assertTrue(result["startupFailed"])
            self.assertFalse(result["shellReady"])
            self.assertEqual(result["initialData"], "")
            self.assertEqual(result["closeCount"], 1)
            self.assertEqual(len(result["writes"]), 1)
            self.assertIn("ADB Shell 启动失败", result["writes"][0])
            self.assertIn("device unauthorized", result["writes"][0])
            self.assertIn("ADB Shell 启动失败", result["startupFailureStatus"])
            self.assertIn("device unauthorized", result["startupFailureStatus"])
            self.assertNotIn("Welcome to host", result["writes"][0])
            self.assertNotIn("host@controller", result["writes"][0])
            self.assertNotIn("host-only-output", result["writes"][0])
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

    def test_terminal_render_does_not_duplicate_mount_while_xterm_loads(self):
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
            pending = page.evaluate(
                """() => {
                  state.elevated = true;
                  state.elevatedUntil = Date.now() + 60000;
                  const originalLoad = loadXTermScripts;
                  let releaseLoad;
                  const gate = new Promise(resolve => { releaseLoad = resolve; });
                  window.__terminalLoadCalls = 0;
                  window.__releaseTerminalLoad = releaseLoad;
                  window.__restoreTerminalLoad = () => { loadXTermScripts = originalLoad; };
                  loadXTermScripts = async () => {
                    window.__terminalLoadCalls += 1;
                    await gate;
                    return originalLoad();
                  };
                  desktopHosts = [{
                    id: 'default',
                    worker_id: 'ats-worker-controller',
                    name: 'Local',
                    connection: 'local@127.0.0.1',
                    offline: false,
                  }];
                  currentHost = desktopHosts[0];
                  currentPage = 'terminal';
                  terminalWorkspace.layout = 'single';
                  terminalWorkspace.panes = [{hostId: 'default'}];
                  terminalWorkspace.instances.clear();
                  terminalWorkspace.mountingPanes.clear();
                  terminalWorkspace.paneGenerations.clear();
                  terminalWorkspace.renderedSignature = null;
                  document.getElementById('terminal-workspace-grid').replaceChildren();
                  renderTerminalWorkspace();
                  renderTerminalWorkspace();
                  renderTerminalWorkspace();
                  return {
                    loadCalls: window.__terminalLoadCalls,
                    pendingMounts: terminalWorkspace.mountingPanes.size,
                    paneGeneration: terminalWorkspace.paneGenerations.get(0) || 0,
                  };
                }"""
            )
            self.assertEqual(pending["loadCalls"], 1)
            self.assertEqual(pending["pendingMounts"], 1)
            self.assertEqual(pending["paneGeneration"], 0)

            page.evaluate("window.__releaseTerminalLoad()")
            page.wait_for_function("terminalWorkspace.instances.has(0)")
            page.wait_for_timeout(250)
            self.assertEqual(page.evaluate("window.__terminalLoadCalls"), 1)
            self.assertEqual(page.evaluate("terminalWorkspace.mountingPanes.size"), 0)
            self.assertEqual(len(terminal_websockets), 1)
            page.evaluate("window.__restoreTerminalLoad()")
        finally:
            page.close()

    def test_terminal_mouse_selection_keeps_selected_text_readable(self):
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
                "state.elevated = true; state.elevatedUntil = Date.now() + 60000"
            )
            page.evaluate("switchPage('terminal', null)")
            page.wait_for_function(
                """() => {
                  const instance = terminalWorkspace.instances.get(0);
                  return instance?.socket?.readyState === WebSocket.OPEN
                    && instance.shellReady;
                }"""
            )

            marker = "/home/hcq/Launcher3QuickStepGo.apk"
            metrics = page.evaluate(
                """marker => {
                  const term = terminalWorkspace.instances.get(0).terminal;
                  term.reset();
                  term.write(`\\x1b[32m${marker}\\x1b[0m`);
                  const rect = term.element.querySelector('.xterm-screen')
                    .getBoundingClientRect();
                  const cell = term._core._renderService.dimensions.css.cell;
                  return {
                    x: rect.x,
                    y: rect.y,
                    cellWidth: cell.width,
                    cellHeight: cell.height,
                    selectionBackground: term.options.theme.selectionBackground,
                    selectionForeground: term.options.theme.selectionForeground,
                  };
                }""",
                marker,
            )
            page.wait_for_timeout(100)

            page.mouse.move(
                metrics["x"] + metrics["cellWidth"] * 0.25,
                metrics["y"] + metrics["cellHeight"] * 0.5,
            )
            page.mouse.down()
            page.mouse.move(
                metrics["x"] + metrics["cellWidth"] * (len(marker) - 0.25),
                metrics["y"] + metrics["cellHeight"] * 0.5,
                steps=8,
            )
            page.mouse.up()

            selection = page.evaluate(
                """() => {
                  const term = terminalWorkspace.instances.get(0).terminal;
                  return {hasSelection: term.hasSelection(), text: term.getSelection()};
                }"""
            )
            self.assertTrue(selection["hasSelection"])
            self.assertEqual(selection["text"], marker)
            self.assertEqual(metrics["selectionForeground"], "#ffffff")
            self.assertEqual(
                metrics["selectionBackground"], "rgba(124, 92, 255, 0.55)"
            )
        finally:
            page.close()

    def test_terminal_arrow_keys_send_once_without_leaving_terminal_page(self):
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
                """() => {
                  state.elevated = true;
                  state.elevatedUntil = Date.now() + 60000;
                  window.__terminalInputFrames = [];
                  const nativeSend = WebSocket.prototype.send;
                  WebSocket.prototype.send = function(payload) {
                    if (this.url.includes('/api/system/websocket/terminal_workspace_')) {
                      const frame = JSON.parse(payload);
                      if (frame.type === 'terminal_input') {
                        window.__terminalInputFrames.push(frame);
                      }
                    }
                    return nativeSend.call(this, payload);
                  };
                }"""
            )
            page.evaluate("switchPage('terminal', null)")
            page.wait_for_function(
                """() => {
                  const instance = terminalWorkspace.instances.get(0);
                  return instance?.socket?.readyState === WebSocket.OPEN
                    && instance.shellReady;
                }"""
            )
            page.evaluate(
                """() => {
                  window.__terminalInputFrames = [];
                  terminalWorkspace.instances.get(0).terminal.focus();
                }"""
            )

            for key in ("ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight"):
                page.keyboard.press(key)
            page.wait_for_function("window.__terminalInputFrames.length === 4")

            result = page.evaluate(
                """() => ({
                  page: currentPage,
                  frames: window.__terminalInputFrames,
                })"""
            )
            self.assertEqual(result["page"], "terminal")
            self.assertEqual(
                [frame["input"] for frame in result["frames"]],
                ["\x1b[A", "\x1b[B", "\x1b[D", "\x1b[C"],
            )
        finally:
            page.close()

    def test_terminal_resize_before_socket_open_is_sent_after_connect(self):
        page = self.new_page()
        try:
            self.goto_shell(page)
            result = page.evaluate(
                """() => {
                  const pageSurface = document.createElement('div');
                  pageSurface.className = 'page-content active';
                  const paneBody = document.createElement('div');
                  paneBody.className = 'host-workspace-pane-body';
                  Object.defineProperties(paneBody, {
                    clientWidth: {value: 900},
                    clientHeight: {value: 500},
                  });
                  const terminalElement = document.createElement('div');
                  paneBody.appendChild(terminalElement);
                  pageSurface.appendChild(paneBody);
                  document.body.appendChild(pageSurface);

                  const sent = [];
                  const socket = {
                    readyState: WebSocket.CONNECTING,
                    send: payload => sent.push(JSON.parse(payload)),
                  };
                  const instance = {
                    disposed: false,
                    fit: {fit() {}},
                    terminal: {element: terminalElement, cols: 137, rows: 31},
                    socket,
                    lastResizeCols: 0,
                    lastResizeRows: 0,
                  };

                  resizeHostWorkspaceTerminal(instance);
                  const beforeOpen = {
                    sent: [...sent],
                    cols: instance.lastResizeCols,
                    rows: instance.lastResizeRows,
                  };
                  socket.readyState = WebSocket.OPEN;
                  resizeHostWorkspaceTerminal(instance);
                  const afterOpen = {
                    sent: [...sent],
                    cols: instance.lastResizeCols,
                    rows: instance.lastResizeRows,
                  };
                  pageSurface.remove();
                  return {beforeOpen, afterOpen};
                }"""
            )

            self.assertEqual(result["beforeOpen"], {"sent": [], "cols": 0, "rows": 0})
            self.assertEqual(result["afterOpen"], {
                "sent": [{"type": "terminal_resize", "cols": 137, "rows": 31}],
                "cols": 137,
                "rows": 31,
            })
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

            # A later click must replace the one-shot ADB target.  The
            # renderer previously retained any active ADB instance while a
            # new pane was opening, even if its serial was different; every
            # device button then continued to operate the first device.
            page.evaluate(
                """async () => {
                  const localWorkerId = workspaceLocalWorkerId();
                  allDevices.push({
                    device_id: 'LOCAL-ADB-2',
                    serial_no: 'LOCAL-ADB-2',
                    worker_id: localWorkerId,
                    status: 'online',
                  });
                  await openDeviceShell('LOCAL-ADB-2');
                }"""
            )
            page.wait_for_function(
                "() => terminalWorkspace.instances.get(0)?.serialNo === 'LOCAL-ADB-2'"
            )
            second_connect_messages = page.evaluate(
                """() => (window.__terminalFrames || [])
                  .filter(frame => typeof frame === 'string' && frame.includes('\\"type\\":\\"terminal_connect\\"'))
                  .map(frame => JSON.parse(frame))"""
            )
            self.assertEqual(second_connect_messages[-1]["serial_no"], "LOCAL-ADB-2")
            persisted = page.evaluate(
                "JSON.parse(localStorage.getItem('gms_terminal_workspace'))"
            )
            self.assertEqual(persisted["panes"], [{"hostId": "default"}])

            # The isolated PTY used by this smoke test may trigger the
            # unrelated client-username detector while the rejected fixture
            # serial closes. It must not block the return-mode assertion.
            page.evaluate("ModalManager.close('username-detect-modal')")

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

    def test_device_shell_waits_for_host_directory_before_mounting_adb(self):
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
            result = page.evaluate(
                """async () => {
                  state.elevated = true;
                  state.elevatedUntil = Date.now() + 60000;
                  const originalInitDesktopHosts = initDesktopHosts;
                  const localWorkerId = workspaceLocalWorkerId();
                  let resolveHosts;
                  try {
                    // initDesktopHosts creates the local placeholder first,
                    // then its asynchronous directory merge assigns worker_id.
                    // Keep that merge pending to exercise the click-time race.
                    desktopHosts = [];
                    window.desktopHostsInitialized = false;
                    initDesktopHosts = () => {
                      desktopHosts = [{
                        id: 'default',
                        name: 'Controller',
                        connection: 'ui-smoke@127.0.0.1',
                      }];
                      return new Promise(resolve => {
                        resolveHosts = () => {
                          desktopHosts[0].worker_id = localWorkerId;
                          resolve();
                        };
                      });
                    };
                    allDevices = [{
                      device_id: 'RACE-ADB-1',
                      serial_no: 'RACE-ADB-1',
                      worker_id: localWorkerId,
                      status: 'online',
                    }];
                    const opening = openDeviceShell('RACE-ADB-1');
                    await new Promise(resolve => setTimeout(resolve, 0));
                    const beforeDirectoryReady = {
                      currentPage,
                      pane: terminalWorkspace.panes[0] || null,
                    };
                    resolveHosts();
                    await opening;
                    return {
                      beforeDirectoryReady,
                      currentPage,
                      pane: terminalWorkspace.panes[0],
                    };
                  } finally {
                    initDesktopHosts = originalInitDesktopHosts;
                  }
                }"""
            )
            self.assertNotEqual(result["beforeDirectoryReady"]["currentPage"], "terminal")
            self.assertEqual(result["currentPage"], "terminal")
            self.assertEqual(
                result["pane"],
                {
                    "hostId": "default",
                    "mode": "adb",
                    "serialNo": "RACE-ADB-1",
                    "workerId": page.evaluate("workspaceLocalWorkerId()"),
                },
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
