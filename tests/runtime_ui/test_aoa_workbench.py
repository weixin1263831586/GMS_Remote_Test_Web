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
            self.assertEqual(replay_request["body"]["profile_id"], "profile-1")
            self.assertEqual(page_errors, [])
        finally:
            page.close()
