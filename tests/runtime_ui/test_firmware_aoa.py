"""The burn dialog carries recovery options through every upload path."""
import json

from playwright.sync_api import expect

from tests.runtime_ui.harness import RuntimeUiHarness


class RuntimeFirmwareAoaTests(RuntimeUiHarness):
    def prepare(self, page):
        page.route("**/api/devices/aoa/profiles", lambda route: route.fulfill(
            status=200, content_type="application/json", body=json.dumps({"success": True,
                "profiles": [{"id": "wizard", "name": "RK3576 向导", "commands": ["home"], "wait_adb_seconds": 60}]})))
        self.goto_shell(page)
        page.wait_for_function("typeof burnFirmware === 'function'")
        page.evaluate("""() => {
            window.selectedBurnableDeviceIds = () => ['D1'];
            window.validateLocalUsbDeviceSelection = () => true;
            window.requestElevatedAccess = async () => true;
            window.refreshDevicesAfterBurn = async () => {};
        }""")
        page.evaluate("burnFirmware()")
        expect(page.locator("#firmware-aoa-profile option")).to_have_count(2)

    def test_reopening_dialog_does_not_duplicate_profiles_or_reset_disabled_state(self):
        page = self.new_page()
        try:
            self.prepare(page)
            expect(page.locator("#firmware-aoa-profile")).to_be_disabled()
            expect(page.locator("#firmware-aoa-boot-wait")).to_be_disabled()
            page.locator("#firmware-aoa-enabled").check()
            expect(page.locator("#firmware-aoa-profile")).to_be_enabled()
            page.locator("#firmware-aoa-profile").select_option("wizard")
            page.evaluate("closeFirmwareModal(); burnFirmware()")
            expect(page.locator("#firmware-aoa-profile option")).to_have_count(2)
            expect(page.locator("#firmware-aoa-profile")).to_have_value("wizard")
            page.locator("#firmware-aoa-enabled").uncheck()
            expect(page.locator("#firmware-aoa-profile")).to_be_disabled()
        finally:
            page.close()

    def test_recovery_configuration_reaches_server_path_chunk_finalize_and_worker_stage(self):
        for mode in ("server", "chunks", "worker"):
            with self.subTest(mode=mode):
                page = self.new_page()
                requests = []
                try:
                    self.prepare(page)

                    def handle_burn(route, _request, requests=requests, mode=mode):
                        requests.append(route.request.post_data or "")
                        payload = {"success": True}
                        if mode == "worker":
                            payload["command_id"] = "firmware-command"
                        route.fulfill(status=200, content_type="application/json", body=json.dumps(payload))

                    page.route("**/api/burn/firmware?*", handle_burn)
                    page.route("**/api/cluster/firmware/stage", handle_burn)
                    page.route("**/api/cluster/commands/firmware-command", lambda route: route.fulfill(
                        status=200, content_type="application/json", body=json.dumps({"success": True,
                            "command": {"status": "completed", "result": {"device": "D1"}}})))
                    page.evaluate("""mode => {
                        window.selectedClusterWorker = () => mode === 'worker' ? 'worker-1' : '';
                        window.streamCommandEvents = async () => {};
                        window.uploadChunksWithElevationRecovery = async () => ({success: true, staged: true});
                    }""", mode)
                    if mode != "server":
                        page.evaluate("browseLocalFileForFirmware()")
                        page.locator("#firmware-file-input").set_input_files({
                            "name": "update.img", "mimeType": "application/octet-stream", "buffer": b"firmware"})
                    else:
                        page.locator("#firmware-path").fill("/tmp/update.img")
                    page.locator("#firmware-aoa-enabled").check()
                    page.locator("#firmware-aoa-profile").select_option("wizard")
                    page.locator("#firmware-aoa-boot-wait").fill("45")
                    with page.expect_response(lambda response: "/api/burn/firmware?" in response.url
                                              or response.url.endswith("/api/cluster/firmware/stage")):
                        page.locator('[data-click="submitFirmwareBurn"]').click()
                    assert len(requests) == 1, requests
                    assert 'name="aoa_profile_id"\r\n\r\nwizard' in requests[0]
                    assert 'name="aoa_boot_wait_seconds"\r\n\r\n45' in requests[0]
                finally:
                    page.close()
