"""A single-step debug session retains its identity, HID and editable commands."""
from playwright.sync_api import expect

from tests.runtime_ui.harness import RuntimeUiHarness
from tests.runtime_ui.test_aoa_workbench import json_response


class RuntimeAoaDebugSessionTests(RuntimeUiHarness):
    def open_workbench(self, page):
        self.goto_shell(page)
        page.evaluate("switchPage('devices')")
        page.evaluate("aoaViewToggle(true)")
        page.evaluate("""aoaSyncDeviceSources([{serial_no:'TARGET',worker_id:'worker-1',transport:'local_usb',state:'usb_only'}]);
            aoaPanelState.commands=['click 180 525','back']; aoaRenderCommands();""")
        page.locator('#aoa-replay-worker').select_option('worker-1')
        page.locator('#aoa-debug-keep').check()

    def test_single_step_reuses_session_and_receipt_then_explicitly_ends(self):
        page = self.new_page()
        requests, errors = [], []
        state = {"sequence": 0, "status": "running"}
        page.on("pageerror", lambda error: errors.append(str(error)))

        def action(route):
            body = route.request.post_data_json
            if body.get("action") == "screenshot":
                # 默认自动实时投屏的截图轮询回退（后台帧拉取）：不是
                # 本用例守护的“打开面板不发输入动作”。
                json_response(route, {"success": True, "image": ""})
                return
            requests.append(body)
            state["sequence"] += 1
            json_response(route, {"success": True, "accepted": True,
                "command_id": "debug-receipt", "debug_sequence": state["sequence"]})

        def events(route):
            json_response(route, {"events": [{"sequence": state["sequence"], "event_type": "progress",
                "payload": {"phase": "completed", "debug_sequence": state["sequence"], "index": state["sequence"]}}]})

        def control(route):
            assert route.request.post_data_json["action"] == "cancel"
            state["status"] = "cancelled"
            json_response(route, {"success": True, "accepted": True, "command_id": "debug-receipt"})

        try:
            page.route("**/api/devices/aoa/**", lambda route: json_response(route, {"success": True, "profiles": []}))
            page.route("**/api/devices/aoa/playback/*/control", control)
            page.route("**/api/cluster/devices/actions", action)
            page.route("**/api/cluster/commands/debug-receipt", lambda route: json_response(route, {
                "command": {"status": state["status"], "result": {"cleanup_errors": []}}}))
            page.route("**/api/cluster/commands/debug-receipt/events?*", events)
            self.open_workbench(page)
            assert not requests  # Opening/changing options must not send input.
            page.locator('.aoa-command-execute').nth(0).click()
            expect(page.locator('.aoa-command-state').nth(0)).to_have_text('已执行')
            expect(page.locator('#aoa-run-status')).to_contain_text('HID 保持连接')
            expect(page.locator('#aoa-replay-device')).to_be_disabled()
            expect(page.locator('#aoa-hid-touch-mode')).to_be_disabled()
            expect(page.locator('.aoa-command-edit').nth(1)).to_be_enabled()
            for inventory in (
                [{'serial_no': 'OTHER', 'worker_id': 'worker-1', 'transport': 'local_usb'}],
                [{'serial_no': 'OTHER', 'worker_id': 'worker-2', 'transport': 'local_usb'}],
                [],
                [{'serial_no': 'OTHER', 'worker_id': 'worker-1', 'transport': 'local_usb'},
                 {'serial_no': 'TARGET', 'worker_id': 'worker-1', 'transport': 'local_usb'}],
            ):
                page.evaluate('devices => aoaSyncDeviceSources(devices)', inventory)
                expect(page.locator('#aoa-replay-worker')).to_have_value('worker-1')
                expect(page.locator('#aoa-replay-device')).to_have_value('TARGET')
                expect(page.locator('#aoa-replay-device')).to_be_disabled()
                assert len(requests) == 1  # Inventory refresh never sends or repeats input.
                if not any(item['serial_no'] == 'TARGET' for item in inventory):
                    expect(page.locator('#aoa-replay-device option:checked')).to_contain_text('暂未枚举')
            expect(page.locator('#aoa-replay-device option:checked')).not_to_contain_text('暂未枚举')
            # Even a stale/programmatic DOM write cannot redirect a bound input.
            page.evaluate("""for (const [id,value] of [['aoa-replay-worker','worker-2'],['aoa-replay-device','OTHER']]) {
                const select=document.getElementById(id); select.add(new Option(value,value)); select.value=value;
            }""")
            page.locator('.aoa-command-execute').nth(1).click()
            expect(page.locator('.aoa-command-state').nth(1)).to_have_text('已执行')
            assert len(requests) == 2
            assert requests[0]['aoa_commands'] == ['click 180 525', 'back']
            assert requests[0]['aoa_debug_command'] == 'click 180 525'
            assert requests[1]['aoa_debug_command'] == 'back'
            assert requests[0]['aoa_debug_session_id'] == requests[1]['aoa_debug_session_id']
            assert requests[0]['aoa_debug_request_id'] != requests[1]['aoa_debug_request_id']
            assert all(item['worker_id'] == 'worker-1' and item['devices'] == ['TARGET'] for item in requests)
            expect(page.locator('#aoa-replay-worker')).to_have_value('worker-1')
            expect(page.locator('#aoa-replay-device')).to_have_value('TARGET')
            page.locator('#aoa-debug-end').click()
            expect(page.locator('#aoa-run-status')).to_contain_text('HID 连接已释放')
            expect(page.locator('#aoa-debug-end')).to_be_disabled()
            expect(page.locator('#aoa-replay-device')).to_be_enabled()
            for _ in range(2):
                page.evaluate("aoaViewToggle(false)")
                page.evaluate("aoaViewToggle(true)")
            assert len(requests) == 2
            assert not errors
        finally:
            page.close()

    def test_inventory_refresh_retains_missing_target_and_explicit_worker_change_still_works(self):
        page = self.new_page()
        try:
            page.route('**/api/devices/aoa/**', lambda route: json_response(route, {'success': True, 'profiles': []}))
            self.open_workbench(page)
            devices = [{'serial_no': 'OTHER', 'worker_id': 'worker-1', 'transport': 'local_usb'},
                       {'serial_no': 'REMOTE', 'worker_id': 'worker-2', 'transport': 'local_usb'}]
            for inventory in (devices, [], devices):
                page.evaluate('devices => aoaSyncDeviceSources(devices)', inventory)
                expect(page.locator('#aoa-replay-worker')).to_have_value('worker-1')
                expect(page.locator('#aoa-replay-device')).to_have_value('TARGET')
                expect(page.locator('#aoa-replay-device option:checked')).to_have_text('TARGET（暂未枚举）')
            expect(page.locator('#aoa-replay-device')).to_be_enabled()
            page.locator('#aoa-replay-worker').select_option('worker-2')
            expect(page.locator('#aoa-replay-device')).to_have_value('REMOTE')
            page.locator('#aoa-replay-worker').select_option('worker-1')
            expect(page.locator('#aoa-replay-device')).to_have_value('OTHER')
        finally:
            page.close()

    def test_rejected_connection_unlocks_options_and_never_retries_input(self):
        for network_failure in (False, True):
            page = self.new_page()
            requests = []

            def action(route, _request, *, requests=requests, network_failure=network_failure):
                body = route.request.post_data_json
                if body.get("action") == "screenshot":
                    # 默认自动实时投屏的截图轮询回退：非输入动作，不计数。
                    route.fulfill(status=200, content_type='application/json',
                                  body='{"success":true,"image":""}')
                    return
                requests.append(body)
                if network_failure:
                    route.abort('failed')
                else:
                    route.fulfill(status=409, content_type='application/json',
                        body='{"success":false,"error":"本机 Worker 才支持保持连接"}')

            try:
                page.route("**/api/devices/aoa/**", lambda route: json_response(route, {"success": True, "profiles": []}))
                page.route("**/api/cluster/devices/actions", action)
                self.open_workbench(page)
                page.locator('.aoa-command-execute').nth(0).click()
                expect(page.locator('.aoa-command-state').nth(0)).to_have_text('执行失败')
                if network_failure:
                    expect(page.locator('#aoa-replay-device')).to_be_disabled()
                else:
                    expect(page.locator('#aoa-replay-device')).to_be_enabled()
                page.locator('.aoa-command-execute').nth(1).click()
                expect(page.locator('.aoa-command-state').nth(1)).to_have_text('执行失败')
                # Explicit rejection permits a new request; ambiguous transport loss does not.
                assert len(requests) == (1 if network_failure else 2)
            finally:
                page.close()

    def test_ending_an_inflight_step_retains_the_cleanup_message(self):
        page = self.new_page()
        state = {"status": "running"}

        def control(route):
            state['status'] = 'cancelled'
            json_response(route, {'success': True, 'command_id': 'debug-receipt'})

        try:
            page.route("**/api/devices/aoa/**", lambda route: json_response(route, {"success": True, "profiles": []}))
            page.route("**/api/devices/aoa/playback/*/control", control)
            page.route("**/api/cluster/devices/actions", lambda route: json_response(route, {
                'success': True, 'accepted': True, 'command_id': 'debug-receipt', 'debug_sequence': 1}))
            page.route("**/api/cluster/commands/debug-receipt", lambda route: json_response(route, {
                'command': {'status': state['status'], 'result': {'cleanup_errors': []}}}))
            page.route("**/api/cluster/commands/debug-receipt/events?*", lambda route: json_response(route, {'events': []}))
            self.open_workbench(page)
            page.locator('.aoa-command-execute').nth(0).click()
            expect(page.locator('#aoa-debug-end')).to_be_enabled()
            page.locator('#aoa-debug-end').click()
            expect(page.locator('#aoa-run-status')).to_contain_text('HID 连接已释放')
            expect(page.locator('.aoa-command-state').nth(0)).to_have_text('已停止')
            expect(page.locator('#aoa-run-status')).to_contain_text('HID 连接已释放')
        finally:
            page.close()
