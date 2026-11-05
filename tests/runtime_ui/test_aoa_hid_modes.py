"""Input mode follows the profile across single-step debugging and JSON files."""
import json
import tempfile
from pathlib import Path

from playwright.sync_api import expect

from tests.runtime_ui.harness import RuntimeUiHarness


class RuntimeAoaHidModesTests(RuntimeUiHarness):
    def test_legacy_and_direct_modes_preserve_commands_and_roundtrip_metadata(self):
        page = self.new_page()
        submitted = []
        profiles = [
            {"id": "old", "name": "旧模板", "commands": ["click 180 525"], "wait_adb_seconds": 60},
            {"id": "direct", "name": "直接触摸", "commands": ["click 180 580"], "wait_adb_seconds": 60,
             "metadata": {"hid_touch_mode": "direct", "hid_settle_seconds": 4}},
        ]

        def respond(route, payload):
            route.fulfill(status=200, content_type="application/json", body=json.dumps(payload))

        def execute(route):
            body = route.request.post_data_json
            submitted.append(body)
            if body.get('aoa_debug_session_id'):
                # “单条调试保持 HID 连接”默认使能：走调试会话契约，
                # 回执只带受理信息，结果经命令轮询与调试事件回执。
                respond(route, {"accepted": True, "command_id": "cmd-hid-debug",
                                "debug_sequence": 1, "operation_timeout_seconds": 5})
                return
            respond(route, {"success": True, "commands_executed": 1, "native_mode_restored": True})

        debug_state = {"ended": False}

        def debug_status(route):
            status = "completed" if debug_state["ended"] else "running"
            respond(route, {"command": {"status": status, "result": {}}})

        try:
            page.route("**/api/devices/aoa/**", lambda route: respond(route, {"success": True, "profiles": profiles}))
            page.route("**/api/devices/aoa/playback/*/control", lambda route: (
                debug_state.update(ended=True),
                respond(route, {"success": True, "command_id": "cmd-hid-debug"})))
            page.route("**/api/cluster/devices/actions", execute)
            page.route("**/api/cluster/commands/cmd-hid-debug/events?*", lambda route: respond(route, {"events": [
                {"sequence": 1, "event_type": "progress",
                 "payload": {"debug_sequence": 1, "phase": "completed"}}]}))
            page.route("**/api/cluster/commands/cmd-hid-debug", debug_status)
            self.goto_shell(page)
            page.evaluate("switchPage('devices')")
            page.evaluate("aoaViewToggle(true)")
            page.evaluate("""aoaSyncDeviceSources([{serial_no:'TARGET', worker_id:'worker-1',
                transport:'local_usb', state:'usb_only'}])""")
            page.locator('#aoa-replay-worker').select_option('worker-1')
            for profile, mode, wait in (("old", "pointer", 2), ("direct", "direct", 4)):
                page.locator('#aoa-replay-profile').select_option(profile)
                page.get_by_role('button', name='载入选中配置', exact=True).click()
                expect(page.locator('#aoa-hid-touch-mode')).to_have_value(mode)
                expect(page.locator('#aoa-hid-settle')).to_have_value(str(wait))
                page.locator('.aoa-command-execute').click()
                expect(page.locator('.aoa-command-state')).to_have_text('已执行')
                assert submitted[-1]['aoa_hid_touch_mode'] == mode
                assert submitted[-1]['aoa_hid_settle_seconds'] == wait
                # 保持 HID 的调试会话会锁定目标选择器；切换配置前先结束调试。
                debug_state["ended"] = False
                page.locator('#aoa-debug-end').click()
                expect(page.locator('#aoa-replay-profile')).to_be_enabled()
            assert submitted[0]['aoa_commands'] == ['click 180 525']
            assert submitted[1]['aoa_commands'] == ['click 180 580']
            with tempfile.TemporaryDirectory() as directory:
                with page.expect_download() as download:
                    page.get_by_role('button', name='导出 JSON', exact=True).click()
                path = Path(directory) / 'export.json'
                download.value.save_as(path)
                exported = json.loads(path.read_text())
                assert exported['metadata']['hid_touch_mode'] == 'direct'
                assert exported['metadata']['hid_settle_seconds'] == 4
                page.locator('#aoa-hid-touch-mode').select_option('pointer')
                assert len(submitted) == 2  # Changing mode makes no device request.
                page.locator('#aoa-command-file').set_input_files(path)
                expect(page.locator('#aoa-hid-touch-mode')).to_have_value('direct')
                expect(page.locator('#aoa-hid-settle')).to_have_value('4')
        finally:
            page.close()
