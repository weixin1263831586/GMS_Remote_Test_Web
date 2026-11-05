"""The burn dialog carries recovery options through every upload path."""
import json

from playwright.sync_api import expect

from tests.runtime_ui.harness import RuntimeUiHarness


class RuntimeFirmwareAoaTests(RuntimeUiHarness):
    def prepare(self, page):
        page.route("**/api/cluster/firmware/preflight", lambda route: route.fulfill(
            content_type="application/json", body=json.dumps({"success": True, "ready": True})))
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

    def test_missing_declaration_stops_every_upload_path_and_keeps_dialog_open(self):
        for mode in ("server", "chunks", "worker"):
            with self.subTest(mode=mode):
                page = self.new_page()
                requests = []
                try:
                    self.prepare(page)
                    def reject(route, _request, requests=requests):
                        requests.append(route.request.post_data_json)
                        route.fulfill(status=422, content_type="application/json", body=json.dumps({
                            "success": False, "code": "INVALID_SEMANTICS", "error": "所选 AOA 配置缺少目标固件兼容声明"}))
                    page.route("**/api/cluster/firmware/preflight", reject)
                    page.evaluate("""mode => {
                        window.selectedClusterWorker = () => mode === 'worker' ? 'worker-1' : '';
                        window.uploadStarted = false;
                        window.uploadChunksWithElevationRecovery = async () => { window.uploadStarted = true; throw new Error('unexpected upload'); };
                    }""", mode)
                    page.on('request', lambda request, requests=requests: requests.append(request.url)
                            if '/api/burn/firmware' in request.url or request.url.endswith('/firmware/stage') else None)
                    if mode == "server":
                        page.locator("#firmware-path").fill("/tmp/update.img")
                    else:
                        page.evaluate("browseLocalFileForFirmware()")
                        page.locator("#firmware-file-input").set_input_files({
                            "name": "update.img", "mimeType": "application/octet-stream", "buffer": b"firmware"})
                    page.locator("#firmware-aoa-enabled").check()
                    page.locator("#firmware-aoa-profile").select_option("wizard")
                    page.locator('[data-click="submitFirmwareBurn"]').click()
                    expect(page.locator("#firmware-aoa-hint")).to_contain_text("尚未上传或烧写")
                    expect(page.locator("#firmware-modal")).to_be_visible()
                    expect(page.locator('[data-click="submitFirmwareBurn"]')).to_be_enabled()
                    assert len(requests) == 1, requests
                    assert requests[0]["devices"] == ["D1"]
                    assert page.evaluate("window.uploadStarted") is False
                finally:
                    page.close()

    def test_declaration_editor_preserves_existing_measured_source_mapping(self):
        from tests.aoa_fixtures import firmware_declaration, verified_mapping

        page, writes = self.new_page(), []
        mapping = verified_mapping().to_dict()
        profile = {"id": "wizard", "name": "Measured wizard", "commands": ["click 180 320"],
                   "metadata": {"hid_touch_mode": "direct", "calibration": {"mapping": mapping}}}
        declaration = firmware_declaration({**mapping["context"], "fingerprint": "post-flash-build"})
        try:
            self.prepare(page)
            page.route('**/api/devices/aoa/profiles', lambda route: route.fulfill(
                content_type='application/json', body=json.dumps({"success": True, "profiles": [profile]})))
            page.route('**/api/devices/management*', lambda route: route.fulfill(
                content_type='application/json', body=json.dumps({"success": True, "devices": [], "groups": []})))
            page.route('**/api/cluster/devices*', lambda route: route.fulfill(
                content_type='application/json', body=json.dumps({"success": True, "devices": []})))
            def save(route):
                writes.append(route.request.post_data_json)
                route.fulfill(content_type='application/json', body=json.dumps({"success": True, "profile": {**profile, **writes[-1]}}))
            page.route('**/api/devices/aoa/profiles/wizard', save)
            page.locator('#firmware-aoa-enabled').check()
            page.locator('#firmware-aoa-profile').select_option('wizard')
            page.locator('[data-click="openFirmwareAoaProfile"]').click()
            expect(page.locator('#aoa-firmware-declaration')).to_be_visible()
            draft = json.loads(page.locator('#aoa-firmware-declaration').input_value())
            # 无既有声明且设备清单为空：生成空模板草稿（触摸模式随当前 Profile）。
            assert draft['sha256'] == '' and draft['validated_at']
            assert draft['context']['model'] == ''
            assert draft['context']['hid_touch_mode'] == 'direct'
            page.locator('#aoa-firmware-declaration').fill(json.dumps(declaration))
            page.locator('[data-click="aoaFirmwareDeclarationApply"]').click()
            page.locator('[data-click="aoaSaveRecording"]').click()
            expect(page.locator('#aoa-run-status')).to_contain_text('已保存')
            assert len(writes) == 1
            assert writes[0]['metadata']['firmware_compatibility'] == declaration
            assert writes[0]['metadata']['calibration']['mapping'] == mapping
        finally:
            page.close()

    def test_closing_dialog_during_preflight_cancels_submission_without_upload(self):
        page, pending, burns = self.new_page(), [], []
        try:
            self.prepare(page)
            page.route('**/api/cluster/firmware/preflight', lambda route: pending.append(route))
            page.on('request', lambda request: burns.append(request.url) if '/api/burn/firmware' in request.url else None)
            page.locator('#firmware-path').fill('/tmp/update.img')
            page.locator('#firmware-aoa-enabled').check()
            page.locator('#firmware-aoa-profile').select_option('wizard')
            page.evaluate('void submitFirmwareBurn(); void submitFirmwareBurn()')
            expect(page.locator('#firmware-aoa-hint')).to_contain_text('正在检查')
            assert len(pending) == 1
            page.evaluate('closeFirmwareModal()')
            pending[0].fulfill(content_type='application/json', body=json.dumps({'success': True, 'ready': True}))
            page.wait_for_function('firmwareBurnSubmitting === false')
            assert burns == []
        finally:
            page.close()
