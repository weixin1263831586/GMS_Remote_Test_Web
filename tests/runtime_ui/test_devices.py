"""Device inventory, targeting, ownership and hardware actions."""

import json
import re

from tests.runtime_ui.harness import RuntimeUiHarness, expect


class RuntimeDevicesTests(RuntimeUiHarness):
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
            # 初始数据预取（loadInitialTestData 的后台 loadDevices(false)）
            # 与操作序列无关：等它落定后丢弃已记录的请求，断言只覆盖
            # 本次操作触发的设备刷新（必须全部强制刷新）。
            page.wait_for_timeout(300)
            requests.clear()

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
            # 请求序列按阶段分组校验：并发/防抖让个别查询（source-os 探测、
            # 完成后的 adb-forward/status 复查）在相邻位置漂移，逐项精确
            # 匹配会因时序抖动失败。阶段顺序与关键端点是稳定契约。
            sequence = [
                (item["method"], item["path"])
                for item in requests
                if not item["path"].startswith("/api/devices/list?")
            ]
            # 阶段断言：其余设备操作紧随 reboot，usbip 流程在其后、adb 在最后。
            # 杂项白名单：来源 OS 探测与同组的其它设备操作端点。
            def is_device_op(item):
                return item[0] == "POST" and item[1] in {
                    "/api/devices/reboot", "/api/devices/remount",
                    "/api/devices/wifi", "/api/devices/scrcpy",
                    "/api/devices/bootloader-lock",
                }

            def is_source_os_probe(item):
                return item[0] == "GET" and item[1].startswith("/api/usbip/source-os")

            stage_matchers = [
                is_device_op,
                lambda item: item[0] == "GET" and item[1].startswith("/api/usbip/status"),
                lambda item: item == ("POST", "/api/usbip/connect"),
                lambda item: item == ("GET", "/api/adb-forward/status"),
                lambda item: item == ("POST", "/api/adb-forward/start"),
                is_source_os_probe,
            ]
            unexpected = [
                item for item in sequence
                if not any(matches(item) for matches in stage_matchers)
            ]
            self.assertEqual(unexpected, [])
            device_op_index = next(
                i for i, item in enumerate(sequence) if is_device_op(item)
            )
            usbip_status_index = next(
                i for i, item in enumerate(sequence)
                if item[0] == "GET" and item[1].startswith("/api/usbip/status")
            )
            usbip_connect_index = next(
                i for i, item in enumerate(sequence) if item == ("POST", "/api/usbip/connect")
            )
            adb_status_index = next(
                i for i, item in enumerate(sequence) if item == ("GET", "/api/adb-forward/status")
            )
            adb_start_index = next(
                i for i, item in enumerate(sequence) if item == ("POST", "/api/adb-forward/start")
            )
            self.assertLess(device_op_index, usbip_status_index)
            self.assertLess(usbip_status_index, usbip_connect_index)
            self.assertLess(usbip_connect_index, adb_status_index)
            self.assertLess(adb_status_index, adb_start_index)
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

            # 不再清空 requests：后续 /api/usbip/status mock 依赖历史中的
            # connect 记录来返回 connected=true；清空会让 mock 状态回退。
            manage_scenario_start = len(requests)
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
                    for item in requests[manage_scenario_start:]
                ):
                    break
                page.wait_for_timeout(50)
            relevant = [
                (index, item)
                for index, item in enumerate(requests)
                if index >= manage_scenario_start
                and (
                    item["path"] == "/api/usbip/disconnect"
                    or item["path"].startswith("/api/usbip/status?device_host=")
                )
            ]
            disconnect_index = next(
                index for index, item in enumerate(requests)
                if index >= manage_scenario_start
                and item["path"] == "/api/usbip/disconnect"
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

    def test_hidden_unselectable_device_is_excluded_from_burn_request(self):
        page = self.new_page()
        try:
            self.goto_shell(page)
            result = page.evaluate(
                """async () => {
                    state.devices = [
                        {device_id: 'READY-1', status: 'online', locked: false},
                        {device_id: 'BUSY-1', status: 'online', locked: true}
                    ];
                    state.selectedDevices = new Set(['READY-1', 'BUSY-1']);
                    renderDevices();
                    const originalApiCall = window.apiCall;
                    const originalLoadDevices = window.loadDevices;
                    const originalElevatedAccess = window.requestElevatedAccess;
                    let requestDevices = [];
                    window.apiCall = async (_endpoint, _method, body) => {
                        requestDevices = body.devices;
                        return {success: true, results: []};
                    };
                    window.loadDevices = async () => state.devices;
                    window.requestElevatedAccess = async () => true;
                    try {
                        await executeBurnOperation(
                            '/api/burn/serial', {sn_code: 'SN001'}, '烧写SN码'
                        );
                        return {
                            requestDevices,
                            selectedDevices: Array.from(state.selectedDevices),
                            busyChecked: document.querySelector(
                                '.device-item[data-device-id="BUSY-1"] input'
                            ).checked
                        };
                    } finally {
                        window.apiCall = originalApiCall;
                        window.loadDevices = originalLoadDevices;
                        window.requestElevatedAccess = originalElevatedAccess;
                    }
                }"""
            )
            self.assertEqual(result["requestDevices"], ["READY-1"])
            self.assertEqual(result["selectedDevices"], ["READY-1", "BUSY-1"])
            self.assertFalse(result["busyChecked"])
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
