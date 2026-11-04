"""Browser contract for the three-column Web AOA recording workbench."""

import json

from playwright.sync_api import expect

from tests.runtime_ui.harness import RuntimeUiHarness


PNG_1X1 = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUB"
    "AScY42YAAAAASUVORK5CYII="
)


def json_response(route, payload):
    route.fulfill(
        status=200,
        content_type="application/json",
        body=json.dumps(payload),
    )


class RuntimeAoaWorkbenchTests(RuntimeUiHarness):
    def test_screen_action_records_command_saves_and_replays_to_target(self):
        page = self.new_page()
        page_errors = []
        requests = []
        profiles = []
        page.on("pageerror", lambda error: page_errors.append(str(error)))

        def aoa_api(route):
            path = route.request.url.split("?", 1)[0]
            method = route.request.method
            body = route.request.post_data_json if method != "GET" else None
            if path.endswith("/profiles") and method == "GET":
                return json_response(route, {"success": True, "profiles": profiles})
            if path.endswith("/record/input"):
                requests.append({"kind": "input", "body": body})
                return json_response(route, {
                    "success": True,
                    "command": "click 180 320",
                    "display": [800, 1280],
                })
            if path.endswith("/profiles/from-recording"):
                requests.append({"kind": "save", "body": body})
                profile = {
                    "id": "profile-1",
                    "name": body["name"],
                    "commands": body["commands"],
                    "wait_adb_seconds": body["wait_adb_seconds"],
                    "created_at": "2026-11-02T10:00:00Z",
                    "updated_at": "2026-11-02T10:00:00Z",
                }
                profiles[:] = [profile]
                return json_response(route, {"success": True, "profile": profile})
            if path.endswith("/recover"):
                requests.append({"kind": "replay", "body": body})
                return json_response(route, {
                    "success": True,
                    "commands_executed": 2,
                    "commands_total": 2,
                    "adb_recovered": True,
                    "adb_state": "device",
                })
            if path.endswith("/probe"):
                return json_response(route, {
                    "success": True, "aoa_supported": True, "aoa_protocol": 2,
                })
            return json_response(route, {"success": True})

        def cluster_action(route):
            body = route.request.post_data_json
            requests.append({"kind": "cluster", "body": body})
            if body.get("action") == "screenshot":
                return json_response(route, {"success": True, "image": PNG_1X1})
            return json_response(route, {"success": True})

        page.route("**/api/devices/aoa/**", aoa_api)
        page.route("**/api/cluster/devices/actions", cluster_action)
        try:
            self.goto_shell(page)
            page.evaluate("switchPage('devices')")
            page.wait_for_function("typeof aoaViewToggle === 'function'")
            page.evaluate("aoaViewToggle(true)")
            expect(page.locator("#aoa-panel")).to_be_visible()
            expect(page.locator(".aoa-workbench-grid > .aoa-column")).to_have_count(3)

            # Opening the workbench is read-only: it loads profiles but does
            # not take a screenshot or send a device input until the user acts.
            self.assertEqual(requests, [])
            page.evaluate("""aoaSyncDeviceSources([
                {serial_no: 'REC-1', worker_id: 'worker-a', transport: 'local_usb',
                 state: 'available', model: 'Recorder'},
                {serial_no: 'TARGET-2', worker_id: 'worker-a', transport: 'local_usb',
                 state: 'usb_only', model: 'User build'}
            ])""")
            self.assertEqual(requests, [])
            page.locator("#aoa-replay-device").select_option("TARGET-2")
            page.locator("#aoa-record-start").click()
            expect(page.locator("#aoa-screen-image")).to_be_visible()
            page.evaluate("aoaExecuteRecorderInput({action: 'tap', x: 400, y: 640})")
            expect(page.locator("#aoa-command-count")).to_have_text("2")
            expect(page.locator("#aoa-command-list")).to_contain_text("click 180 320")
            expect(page.locator("#aoa-command-list")).to_contain_text("sleep 800")
            first_row = page.locator("#aoa-command-list li").first
            expect(first_row.locator(".aoa-command-index")).to_have_text("1")
            row_layout = page.evaluate("""() => {
                const li = document.querySelector('#aoa-command-list li');
                const code = li.querySelector('code');
                const idx = li.querySelector('.aoa-command-index');
                return code.getClientRects().length === 1
                    && Math.abs(code.getBoundingClientRect().top
                                - idx.getBoundingClientRect().top) < 2;
            }""")
            self.assertTrue(row_layout, "index and command must render on one line")

            page.locator("#aoa-profile-name").fill("RK3562 USB 调试恢复")
            page.get_by_role("button", name="保存当前命令").click()
            expect(page.locator("#aoa-replay-profile")).to_have_value("profile-1")

            page.evaluate("window.showConfirmDialog = async () => true")
            page.get_by_role("button", name="回放选中配置").click()
            expect(page.locator("#aoa-result")).to_contain_text("ADB 已恢复")

            input_request = next(item for item in requests if item["kind"] == "input")
            self.assertEqual(input_request["body"]["worker_id"], "worker-a")
            self.assertEqual(input_request["body"]["device"], "REC-1")
            self.assertEqual(input_request["body"]["input"]["action"], "tap")
            save_request = next(item for item in requests if item["kind"] == "save")
            self.assertEqual(save_request["body"]["commands"], [
                "click 180 320", "sleep 800",
            ])
            replay_request = next(item for item in requests if item["kind"] == "replay")
            self.assertEqual(replay_request["body"]["device"], "TARGET-2")
            self.assertEqual(replay_request["body"]["commands"], ["click 180 320", "sleep 800"])
            self.assertEqual(page_errors, [])
        finally:
            page.close()

    def test_replay_progress_events_highlight_executing_row(self):
        """回放期间 Worker 上报的 progress 事件应高亮对应命令行并自动滚动。"""
        page = self.new_page()
        state = {"status_polls": 0}

        def command_status(route):
            state["status_polls"] += 1
            status = "completed" if state["status_polls"] >= 2 else "running"
            return route.fulfill(
                status=200, content_type="application/json",
                body=json.dumps({"success": True,
                                 "command": {"id": "cmd-prog", "status": status,
                                             "result": {"commands_executed": 3,
                                                        "commands_total": 3,
                                                        "adb_recovered": True}}}))

        def command_events(route):
            after = route.request.url.split("after=")[-1]
            events = []
            if after == "-1":
                events = [{"sequence": 1, "event_type": "progress", "level": "info",
                           "message": "3/3 home",
                           "payload": {"index": 3, "total": 3, "command": "home"}}]
            return route.fulfill(
                status=200, content_type="application/json",
                body=json.dumps({"success": True,
                                 "command": {"id": "cmd-prog", "status": "running"},
                                 "events": events}))

        try:
            page.route("**/api/devices/aoa/**", lambda route: route.fulfill(
                status=200, content_type="application/json",
                body=json.dumps({"success": True, "profiles": []})))
            page.route("**/api/cluster/commands/cmd-prog/events?*", command_events)
            page.route("**/api/cluster/commands/cmd-prog", command_status)
            self.goto_shell(page)
            page.evaluate("switchPage('devices')")
            page.wait_for_function("typeof aoaViewToggle === 'function'")
            page.evaluate("aoaViewToggle(true)")
            expect(page.locator("#aoa-panel")).to_be_visible()
            page.evaluate("""() => {
                aoaPanelState.commands = ['home', 'sleep 800', 'back'];
                aoaRenderCommands();
                window.__awaitPromise = aoaAwaitCommand(
                    {accepted: true, command_id: 'cmd-prog'});
            }""")
            # progress 事件到达：第 3 行高亮 + 状态栏显示实时进度
            expect(page.locator("#aoa-command-list li").nth(2)).to_have_class("aoa-command-active")
            expect(page.locator("#aoa-run-status")).to_contain_text("3/3")
            # 到达终态后高亮清除
            expect(page.locator("#aoa-command-list li.aoa-command-active")).to_have_count(0)
            result = page.evaluate("window.__awaitPromise")
            self.assertEqual(result["commands_executed"], 3)
        finally:
            page.close()

    def test_transient_status_failure_keeps_following_original_command(self):
        page = self.new_page()
        polls = []

        def status(route):
            polls.append(route.request.method)
            if len(polls) == 1:
                return route.fulfill(status=503, content_type="application/json",
                                     body=json.dumps({"success": False, "error": "temporarily unavailable"}))
            return json_response(route, {"success": True, "command": {"status": "completed",
                "result": {"success": True, "commands_executed": 1}}})

        try:
            page.route("**/api/devices/aoa/**", lambda route: json_response(route, {"success": True, "profiles": []}))
            page.route("**/api/cluster/commands/cmd-reconnect/events?*", lambda route: json_response(route, {"success": True, "events": []}))
            page.route("**/api/cluster/commands/cmd-reconnect", status)
            self.goto_shell(page)
            page.evaluate("switchPage('devices')")
            page.wait_for_function("typeof aoaAwaitCommand === 'function'")
            page.evaluate("""() => {
                window.__reconnectResult = null;
                aoaAwaitCommand({accepted: true, command_id: 'cmd-reconnect'})
                    .then(result => { window.__reconnectResult = result; });
            }""")
            page.wait_for_function("window.__reconnectResult?.commands_executed === 1", timeout=10000)
            self.assertEqual(polls, ["GET", "GET"])
        finally:
            page.close()

    def test_full_playback_list_tracks_steps_and_pauses_without_redispatch(self):
        page = self.new_page()
        commands = [f"click 180 {i}" for i in range(100)]
        profile = {"id": "full-profile", "name": "完整回放", "commands": commands, "wait_adb_seconds": 60}
        state = {"events": [], "finished": False, "recoveries": [], "controls": []}

        def aoa_api(route):
            path = route.request.url.split("?", 1)[0]
            if path.endswith("/profiles"):
                return json_response(route, {"success": True, "profiles": [profile]})
            body = route.request.post_data_json
            if path.endswith("/recover"):
                state["recoveries"].append(body)
                return json_response(route, {"success": True, "accepted": True, "command_id": "cmd-full"})
            if path.endswith("/control"):
                state["controls"].append(body["action"])
                return json_response(route, {"success": True, "accepted": True, "action": body["action"]})
            return json_response(route, {"success": True})

        def command_events(route):
            cursor = int(route.request.url.split("after=")[-1])
            return json_response(route, {"success": True, "events": [e for e in state["events"] if e["sequence"] > cursor]})

        def status(route):
            return json_response(route, {"success": True, "command": {
                "status": "completed" if state["finished"] else "running",
                "result": {"success": True, "commands_executed": 100, "commands_total": 100, "adb_recovered": True},
            }})

        def emit(phase, index):
            state["events"].append({"sequence": len(state["events"]) + 1, "event_type": "progress",
                "payload": {"phase": phase, "index": index, "total": 100, "command": commands[index - 1]}})

        try:
            page.route("**/api/devices/aoa/**", aoa_api)
            page.route("**/api/cluster/commands/cmd-full/events?*", command_events)
            page.route("**/api/cluster/commands/cmd-full", status)
            self.goto_shell(page)
            page.evaluate("switchPage('devices')")
            page.wait_for_function("typeof aoaViewToggle === 'function'")
            page.evaluate("aoaViewToggle(true)")
            expect(page.locator("#aoa-replay-profile")).to_have_value("full-profile")
            page.evaluate("""() => {
                aoaSyncDeviceSources([{serial_no: 'TARGET', worker_id: 'worker-a', transport: 'local_usb', state: 'usb_only'}]);
                window.showConfirmDialog = async () => true;
            }""")
            command_list = page.locator('#aoa-command-list')
            list_top = command_list.evaluate('(node) => node.offsetTop')
            page.get_by_role("button", name="回放选中配置").click()
            rows = page.locator("#aoa-command-list li")
            expect(rows).to_have_count(100)
            expect(rows.last.locator("code")).to_have_text("click 180 99")
            expect(rows.first.locator(".aoa-command-state")).to_have_text("待执行")
            expect(page.locator("#aoa-playback-pause")).to_be_enabled()
            self.assertEqual(command_list.evaluate('(node) => node.offsetTop'), list_top)
            expect(rows.first.get_by_role("button", name="删除")).to_be_disabled()
            expect(rows.first.locator('.aoa-command-execute')).to_be_disabled()
            expect(rows.first.locator('.aoa-command-edit')).to_be_disabled()
            expect(page.locator("#aoa-record-start")).to_be_disabled()
            emit("running", 1)
            expect(rows.first.locator(".aoa-command-state")).to_have_text("执行中")
            emit("completed", 1)
            emit("reconnecting", 2)
            expect(rows.nth(1).locator(".aoa-command-state")).to_have_text("USB 重连中")
            expect(page.locator("#aoa-run-status")).to_contain_text("USB 重连中")
            emit("running", 2)
            expect(rows.first.locator(".aoa-command-state")).to_have_text("已完成")
            expect(rows.nth(1).locator(".aoa-command-state")).to_have_text("执行中")
            page.locator("#aoa-playback-pause").click()
            expect(page.locator("#aoa-run-status")).to_contain_text("暂停请求已发送")
            expect(page.locator("#aoa-playback-resume")).to_be_hidden()
            emit("completed", 2)
            emit("paused", 3)
            expect(rows.nth(2).locator(".aoa-command-state")).to_have_text("已暂停")
            expect(page.locator("#aoa-playback-resume")).to_be_enabled()
            self.assertEqual(command_list.evaluate('(node) => node.offsetTop'), list_top)
            expect(rows.first.locator('.aoa-command-edit')).to_be_disabled()
            page.screenshot(path="/tmp/gms-aoa-playback-paused.png", full_page=True)
            page.locator("#aoa-playback-resume").click()
            emit("resumed", 3)
            emit("running", 3)
            expect(rows.nth(2).locator(".aoa-command-state")).to_have_text("执行中")
            expect(page.locator("#aoa-playback-pause")).to_be_enabled()
            self.assertEqual(state["controls"], ["pause", "resume"])
            self.assertEqual(len(state["recoveries"]), 1)
            self.assertEqual(state["recoveries"][0]["commands"], commands)
            emit("completed", 100)
            state["finished"] = True
            expect(page.locator("#aoa-playback-progress")).to_contain_text("已完成 100 条")
            expect(page.locator("#aoa-playback-pause")).to_be_hidden()
            expect(page.locator("#aoa-playback-resume")).to_be_hidden()
            self.assertEqual(command_list.evaluate('(node) => node.offsetTop'), list_top)
        finally:
            page.close()

    def test_execute_one_command_uses_target_preserves_list_and_reports_result(self):
        page = self.new_page()
        commands = ['wake', 'click 180 522', 'sleep 250']
        requests = []
        state = {'finished': False, 'failed': False}

        def aoa_api(route):
            if route.request.url.split('?', 1)[0].endswith('/profiles'):
                return json_response(route, {'success': True, 'profiles': [{
                    'id': 'debug-profile', 'name': '单条调试', 'commands': commands, 'wait_adb_seconds': 60,
                }]})
            return json_response(route, {'success': True})

        def submit(route):
            requests.append(route.request.post_data_json)
            return json_response(route, {'success': True, 'accepted': True, 'command_id': 'cmd-debug'})

        def events(route):
            cursor = int(route.request.url.split('after=')[-1])
            return json_response(route, {'success': True, 'events': [] if cursor >= 1 else [{
                'sequence': 1, 'event_type': 'progress',
                'payload': {'phase': 'running', 'index': 1, 'total': 1, 'command': requests[-1]['aoa_commands'][0]},
            }]})

        def status(route):
            if state['failed']:
                return json_response(route, {'success': True, 'command': {'status': 'failed', 'error': 'USB HID input rejected'}})
            return json_response(route, {'success': True, 'command': {
                'status': 'completed' if state['finished'] else 'running',
                'result': {'success': True, 'commands_executed': 1, 'commands_total': 1, 'native_mode_restored': True},
            }})

        try:
            page.route('**/api/devices/aoa/**', aoa_api)
            page.route('**/api/cluster/devices/actions', submit)
            page.route('**/api/cluster/commands/cmd-debug/events?*', events)
            page.route('**/api/cluster/commands/cmd-debug', status)
            self.goto_shell(page)
            page.evaluate("switchPage('devices')")
            page.wait_for_function("typeof aoaViewToggle === 'function'")
            page.evaluate('aoaViewToggle(true)')
            expect(page.locator('#aoa-replay-profile')).to_have_value('debug-profile')
            page.evaluate("""aoaSyncDeviceSources([
                {serial_no: 'REC', worker_id: 'record-worker', transport: 'local_usb', state: 'available'},
                {serial_no: 'TARGET', worker_id: 'target-worker', transport: 'local_usb', state: 'usb_only'}
            ])""")
            page.locator('#aoa-replay-worker').select_option('target-worker')
            page.get_by_role('button', name='载入选中配置', exact=True).click()
            rows = page.locator('#aoa-command-list li')
            expect(rows).to_have_count(3)
            expect(rows.locator('.aoa-command-execute')).to_have_count(3)
            command_list = page.locator('#aoa-command-list')
            list_top = command_list.bounding_box()['y']
            rows.nth(1).get_by_role('button', name='编辑第 2 条命令', exact=True).click()
            rows.nth(1).get_by_role('textbox', name='第 2 条命令内容').fill('click 200 600')
            # Execute commits the visible draft before dispatching a single command.
            rows.nth(1).get_by_role('button', name='执行第 2 条命令', exact=True).click()
            expect(rows.nth(1).locator('.aoa-command-state')).to_have_text('执行中')
            self.assertAlmostEqual(command_list.bounding_box()['y'], list_top, delta=1)
            expect(rows.first.locator('.aoa-command-execute')).to_be_disabled()
            expect(rows.last.locator('.aoa-command-execute')).to_be_disabled()
            expect(rows.first.locator('.aoa-command-edit')).to_be_disabled()
            expect(page.locator('#aoa-playback-pause')).to_be_hidden()
            page.evaluate('aoaCommandExecute(1)')
            page.wait_for_timeout(650)
            self.assertEqual(requests, [{
                'worker_id': 'target-worker', 'devices': ['TARGET'], 'action': 'aoa_sequence',
                'aoa_commands': ['click 200 600'], 'wait_adb_seconds': 0,
                'aoa_hid_touch_mode': 'pointer', 'aoa_hid_settle_seconds': 2,
            }])
            expect(rows.first.locator('.aoa-command-state')).to_be_hidden()
            state['finished'] = True
            expect(rows.nth(1).locator('.aoa-command-state')).to_have_text('已执行')
            self.assertAlmostEqual(command_list.bounding_box()['y'], list_top, delta=1)
            expect(rows.nth(1).locator('.aoa-command-execute')).to_be_enabled()
            expect(page.locator('#aoa-result')).to_contain_text('单条执行完成 · 第 2 条 · TARGET')
            edited_commands = ['wake', 'click 200 600', 'sleep 250']
            self.assertEqual(page.evaluate('aoaPanelState.commands'), edited_commands)
            state['failed'] = True
            rows.first.get_by_role('button', name='执行第 1 条命令', exact=True).click()
            expect(rows.first.locator('.aoa-command-state')).to_have_text('执行失败')
            self.assertAlmostEqual(command_list.bounding_box()['y'], list_top, delta=1)
            expect(rows.first.locator('.aoa-command-execute')).to_be_enabled()
            expect(page.locator('#aoa-result')).to_contain_text('USB HID input rejected')
            self.assertEqual(requests[-1]['aoa_commands'], ['wake'])
            self.assertEqual(len(requests), 2)
            self.assertEqual(page.evaluate('aoaPanelState.commands'), edited_commands)
        finally:
            page.close()

    def test_edit_command_cancel_validation_replay_and_persistence(self):
        page = self.new_page()
        profile = {'id': 'editable', 'name': '可编辑配置', 'commands': ['click 180 522', 'sleep 800']}
        writes = []

        def aoa_api(route):
            path = route.request.url.split('?', 1)[0]
            if route.request.method == 'GET':
                return json_response(route, {'success': True, 'profiles': [profile]})
            body = route.request.post_data_json
            writes.append({'path': path, 'body': body})
            if path.endswith('/recover'):
                return json_response(route, {'success': True, 'commands_executed': 2,
                                             'commands_total': 2, 'adb_recovered': True})
            profile.update(body)
            return json_response(route, {'success': True, 'profile': profile})

        try:
            page.route('**/api/devices/aoa/**', aoa_api)
            self.goto_shell(page)
            page.evaluate("switchPage('devices')")
            page.wait_for_function("typeof aoaViewToggle === 'function'")
            page.evaluate('aoaViewToggle(true)')
            expect(page.locator('#aoa-replay-profile')).to_have_value('editable')
            page.evaluate("""aoaSyncDeviceSources([
                {serial_no: 'TARGET', worker_id: 'worker-a', transport: 'local_usb', state: 'usb_only'}
            ]); window.showConfirmDialog = async () => true;""")
            page.get_by_role('button', name='载入选中配置', exact=True).click()
            rows = page.locator('#aoa-command-list li')
            edit = rows.first.get_by_role('button', name='编辑第 1 条命令', exact=True)
            editor = rows.first.get_by_role('textbox', name='第 1 条命令内容')
            edit.click()
            editor.fill('click 1 1')
            rows.first.get_by_role('button', name='取消', exact=True).click()
            expect(rows.first.locator('code')).to_have_text('click 180 522')
            edit.click()
            editor.fill('click 2 2')
            editor.press('Escape')
            expect(rows.first.locator('code')).to_have_text('click 180 522')
            edit.click()
            editor.fill('   ')
            editor.press('Enter')
            expect(page.locator('#aoa-run-status')).to_contain_text('命令不能为空')
            rows.first.get_by_role('button', name='执行第 1 条命令', exact=True).click()
            page.get_by_role('button', name='保存当前命令', exact=True).click()
            page.get_by_role('button', name='回放选中配置', exact=True).click()
            self.assertEqual(writes, [])
            editor.fill('  click 200 600  ')
            editor.press('Enter')
            expect(rows.first.locator('code')).to_have_text('click 200 600')
            self.assertEqual(writes, [])  # Editing alone never persists to the server.
            # Reopening the workbench does not duplicate edit handlers or lose changes.
            page.evaluate("""async () => {
                await aoaViewToggle(false); await aoaViewToggle(true);
                aoaSyncDeviceSources([{serial_no: 'TARGET', worker_id: 'worker-a',
                    transport: 'local_usb', state: 'usb_only'}]);
            }""")
            expect(rows.locator('.aoa-command-edit')).to_have_count(2)
            page.get_by_role('button', name='回放选中配置', exact=True).click()
            expect(page.locator('#aoa-result')).to_contain_text('回放完成')
            self.assertEqual(writes[0]['body']['commands'], ['click 200 600', 'sleep 800'])
            self.assertEqual(profile['commands'], ['click 180 522', 'sleep 800'])
            # Saving the profile also commits an open editor before building the payload.
            edit.click()
            editor.fill('click 240 650')
            page.get_by_role('button', name='保存当前命令', exact=True).click()
            page.wait_for_function("aoaPanelState.profiles[0].commands[0] === 'click 240 650'")
            self.assertTrue(writes[1]['path'].endswith('/api/devices/aoa/profiles/editable'))
            self.assertEqual(writes[1]['body']['commands'], ['click 240 650', 'sleep 800'])
        finally:
            page.close()

    def test_single_command_requires_explicit_target(self):
        page = self.new_page()
        submissions = []
        try:
            page.route('**/api/devices/aoa/**', lambda route: json_response(route, {'success': True, 'profiles': []}))
            page.route('**/api/cluster/devices/actions', lambda route: submissions.append(route.request.post_data_json) or json_response(route, {'success': True}))
            self.goto_shell(page)
            page.evaluate("switchPage('devices')")
            page.wait_for_function("typeof aoaViewToggle === 'function'")
            page.evaluate("""async () => {
                await aoaViewToggle(true);
                aoaSyncDeviceSources([]);
                // Clear the visible target and click in the same browser turn;
                // initial inventory requests must not repopulate it between setup and click.
                document.querySelector('#aoa-replay-worker').replaceChildren(new Option('请选择', ''));
                document.querySelector('#aoa-replay-device').replaceChildren(new Option('请选择', ''));
                aoaPanelState.commands = ['home'];
                aoaRenderCommands();
                document.querySelector('.aoa-command-execute').click();
            }""")
            expect(page.locator('#aoa-run-status')).to_have_text('请先选择目标 Worker 和目标设备，再执行单条命令')
            self.assertEqual(submissions, [])
        finally:
            page.close()

    def test_long_playback_error_keeps_workbench_layout_contained(self):
        page = self.new_page()
        error = (
            'device vanished while sending HID to RK3576GMS1; reconcile required, input was not replayed; '
            'cleanup diagnostics: ' + str([{'step': 'release_touch', 'error': 'release_touch did not complete'}] * 20)
        )
        profile = {'id': 'error-profile', 'name': 'USB 回放', 'commands': ['click 180 522']}

        def aoa_api(route):
            if route.request.method == 'GET':
                return json_response(route, {'success': True, 'profiles': [profile]})
            return route.fulfill(status=502, content_type='application/json',
                                 body=json.dumps({'success': False, 'error': error}))

        def layout():
            return page.evaluate("""() => {
                const rect = selector => {
                    const r = document.querySelector(selector).getBoundingClientRect();
                    return {x: r.x, y: r.y, width: r.width, height: r.height, right: r.right};
                };
                return {
                    header: rect('.aoa-workbench-header'), intro: rect('.aoa-workbench-header > div'),
                    status: rect('#aoa-run-status'), grid: rect('.aoa-workbench-grid'),
                    title: rect('.aoa-workbench-title'), description: rect('.aoa-workbench-header .aoa-muted'),
                };
            }""")

        try:
            page.route('**/api/devices/aoa/**', aoa_api)
            self.goto_shell(page)
            page.evaluate("switchPage('devices')")
            page.wait_for_function("typeof aoaViewToggle === 'function'")
            page.evaluate('aoaViewToggle(true)')
            expect(page.locator('#aoa-replay-profile')).to_have_value('error-profile')
            page.evaluate("window.showConfirmDialog = async () => true")
            for width in (1440, 1000, 640):
                page.set_viewport_size({'width': width, 'height': 1000})
                page.evaluate("""() => {
                    aoaSyncDeviceSources([{serial_no: 'RK3576GMS1', worker_id: 'worker-a',
                        transport: 'local_usb', state: 'usb_only'}]);
                    aoaSetStatus('待机'); document.querySelector('#aoa-result').hidden = true;
                }""")
                before = layout()
                page.evaluate('void aoaReplaySelected()')
                expect(page.locator('#aoa-run-status')).to_contain_text('device vanished')
                expect(page.locator('#aoa-result')).to_contain_text(error)
                after = layout()
                self.assertAlmostEqual(after['intro']['width'], before['intro']['width'], delta=1)
                self.assertAlmostEqual(after['header']['height'], before['header']['height'], delta=1)
                self.assertAlmostEqual(after['grid']['y'], before['grid']['y'], delta=1)
                self.assertLessEqual(after['status']['right'], after['header']['right'] + 1)
                self.assertAlmostEqual(after['title']['y'] + after['title']['height'] / 2,
                                       after['description']['y'] + after['description']['height'] / 2, delta=1)
                self.assertLessEqual(after['title']['right'], after['description']['x'])
                self.assertEqual(page.locator('#aoa-run-status').get_attribute('title'), '回放失败：' + error)
                self.assertTrue(page.locator('#aoa-result').evaluate(
                    '(node) => node.clientHeight <= 180 && node.scrollHeight > node.clientHeight '
                    '&& node.scrollWidth <= node.clientWidth + 1'))
                if width == 1440:
                    page.screenshot(path='/tmp/gms-aoa-long-error-fixed.png', full_page=True)
        finally:
            page.close()

    def test_screen_states_keep_columns_and_bottom_controls_in_place(self):
        page = self.new_page()

        def layout():
            return page.evaluate("""() => {
                const rect = selector => {
                    const r = document.querySelector(selector).getBoundingClientRect();
                    return {x: r.x, y: r.y, width: r.width, height: r.height, bottom: r.bottom};
                };
                return ['#aoa-panel', '.aoa-screen-column', '.aoa-workbench-grid > section:nth-child(2)',
                    '.aoa-command-column', '#aoa-screen-stage', '.aoa-screen-toolbar', '.aoa-text-input']
                    .map(rect);
            }""")

        try:
            page.route('**/api/devices/aoa/**', lambda route: json_response(route, {'success': True, 'profiles': []}))
            self.goto_shell(page)
            page.evaluate("switchPage('devices')")
            page.wait_for_function("typeof aoaViewToggle === 'function'")
            page.evaluate("""async () => {
                await aoaViewToggle(true); aoaSyncDeviceSources([]);
                window.testStreamActive = false;
                window.aoaLiveScreen = {
                    start: async () => { window.testStreamActive = true; },
                    stop: () => { window.testStreamActive = false; },
                    isActive: () => window.testStreamActive,
                };
            }""")
            before = layout()
            # The three columns fill the panel instead of leaving an unused strip below.
            for column in before[1:4]:
                self.assertAlmostEqual(column['bottom'], before[0]['bottom'], delta=1)
            page.evaluate("""aoaSyncDeviceSources([
                {serial_no: 'REC', worker_id: 'worker-a', transport: 'local_usb', state: 'available'}
            ]); localStorage.setItem('gms-aoa-calibration:worker-a:REC', JSON.stringify({
                measured: true, scale_x: 2.2, scale_y: 2.2,
                context: {version: 1, model: 'Box', display: [800, 1280], rotation: 0}
            }));""")
            page.locator('#aoa-record-device').select_option('REC')
            expect(page.locator('#aoa-screen-status')).to_contain_text('正在连接')
            self.assertIsNone(page.evaluate('aoaPanelState.calibration'))
            self.assertEqual(layout(), before)
            for width, height in ((800, 1280), (1920, 1080)):
                page.evaluate("""([width, height]) => {
                    const canvas = document.querySelector('#aoa-screen-canvas');
                    canvas.width = width; canvas.height = height; canvas.hidden = false;
                    document.querySelector('#aoa-screen-empty').hidden = true;
                }""", [width, height])
                self.assertEqual(layout(), before)
                surface = page.locator('#aoa-screen-canvas').bounding_box()
                self.assertAlmostEqual(surface['width'] / surface['height'], width / height, delta=0.01)
                stage = before[4]
                self.assertLessEqual(surface['width'], stage['width'])
                self.assertLessEqual(surface['height'], stage['height'])
                point = page.evaluate("""() => {
                    const r = document.querySelector('#aoa-screen-canvas').getBoundingClientRect();
                    return aoaPointFromPointer({clientX: r.x + r.width / 2, clientY: r.y + r.height / 2});
                }""")
                self.assertEqual(point, {'x': width // 2, 'y': height // 2})
            page.evaluate("""async () => {
                window.testStreamActive = false;
                document.querySelector('#aoa-screen-canvas').hidden = true;
                const image = document.querySelector('#aoa-screen-image');
                image.src = 'data:image/svg+xml,' + encodeURIComponent(
                    '<svg xmlns="http://www.w3.org/2000/svg" width="720" height="1280"></svg>');
                await image.decode(); image.hidden = false;
            }""")
            expect(page.locator('#aoa-screen-image')).to_be_visible()
            self.assertEqual(layout(), before)
            page.evaluate('aoaSyncDeviceSources([]); aoaRecorderDeviceChanged()')
            expect(page.locator('#aoa-screen-empty')).to_be_visible()
            self.assertEqual(layout(), before)
            page.evaluate('async () => { await aoaViewToggle(false); await aoaViewToggle(true); }')
            self.assertEqual(layout(), before)
            page.screenshot(path='/tmp/gms-aoa-stable-screen.png', full_page=True)
        finally:
            page.close()
