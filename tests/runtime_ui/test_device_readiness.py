"""Connection labels must describe observed ADB state and reflect failed reads."""
import json

from playwright.sync_api import expect

from tests.runtime_ui.harness import RuntimeUiHarness


def respond(route, payload, status=200):
    route.fulfill(status=status, content_type='application/json', body=json.dumps(payload))


class RuntimeDeviceReadinessTests(RuntimeUiHarness):
    def test_new_device_selection_excludes_an_unresponsive_previous_target(self):
        page = self.new_page()
        previous = '66df75a34ebde4a4'
        selected = 'RK3326GMS1'
        devices = [
            {'device_id': previous, 'status': 'unresponsive', 'protocol': 'adb'},
            {'device_id': selected, 'status': 'online', 'protocol': 'adb'},
        ]
        info_requests, test_requests = [], []

        def info(route):
            info_requests.append(route.request.post_data_json)
            respond(route, {'success': True, 'results': []})

        def start(route):
            test_requests.append(route.request.post_data_json)
            respond(route, {'success': False, 'error': 'mock capacity exhausted'}, 409)

        try:
            page.route('**/api/devices/list*', lambda route: respond(route, devices))
            page.route('**/api/devices/info', info)
            page.route('**/api/test/start', start)
            self.goto_shell(page)
            page.evaluate("""async devices => {
                await GmsWorkspace.ready;
                GmsWorkspace.update({scope_mode: 'single', device_ids: [devices[0].device_id]},
                    {source: 'ui-smoke', persist: false});
                state.devices = devices;
                state.selectedDevices = new Set([devices[0].device_id]);
                state.followFilter = false;
                renderDevices();
            }""", devices)
            old_card = page.locator(f'.device-item[data-device-id="{previous}"]')
            expect(old_card.locator('input')).to_be_disabled()
            expect(old_card.locator('input')).not_to_be_checked()
            page.locator(f'.device-item[data-device-id="{selected}"] input').click()
            self.assertEqual(page.evaluate('Array.from(state.selectedDevices)'), [selected])
            self.assertEqual(page.evaluate('GmsWorkspace.get().device_ids'), [selected])
            self.assertTrue(page.evaluate('validateDeviceSelection()'))
            page.locator('#btn-device-info').click()
            expect(page.locator('#btn-device-info')).to_be_enabled()
            self.assertEqual(info_requests, [{'devices': [selected]}])
            page.evaluate("""async () => {
                const suite = document.getElementById('test-suite');
                suite.replaceChildren(new Option('/mock/suite', '/mock/suite'));
                await startTest();
            }""")
            self.assertEqual(len(test_requests), 1)
            self.assertEqual(test_requests[0]['devices'], [selected])
        finally:
            page.close()

    def test_inventory_refresh_prunes_inactive_targets_without_writes_or_automatic_reselection(self):
        page = self.new_page()
        devices = [
            {'device_id': 'OLD', 'status': 'unresponsive'},
            {'device_id': 'READY', 'status': 'online'},
            {'device_id': 'BUSY', 'status': 'online', 'locked': True},
        ]
        writes = []
        try:
            page.route('**/api/devices/list*', lambda route: respond(route, devices))
            page.on('request', lambda request: writes.append(request.url)
                    if request.method == 'PATCH' and '/workspace-context' in request.url else None)
            self.goto_shell(page)
            await_refresh = """async () => {
                await loadDevices(true, {silent: true});
                return {selected: Array.from(state.selectedDevices), workspace: GmsWorkspace.get().device_ids};
            }"""
            page.evaluate("""async () => {
                await GmsWorkspace.ready;
                GmsWorkspace.update({scope_mode: 'single', device_ids: ['OLD', 'READY', 'BUSY', 'GONE']},
                    {source: 'ui-smoke', persist: false});
                state.selectedDevices = new Set(['OLD', 'READY', 'BUSY', 'GONE']);
                state.followFilter = false;
            }""")
            self.assertEqual(page.evaluate(await_refresh), {'selected': ['READY'], 'workspace': ['READY']})
            devices[0]['status'] = 'online'
            devices[2]['locked'] = False
            self.assertEqual(page.evaluate(await_refresh), {'selected': ['READY'], 'workspace': ['READY']})
            expect(page.locator('.device-item[data-device-id="OLD"] input')).not_to_be_checked()
            expect(page.locator('.device-item[data-device-id="BUSY"] input')).not_to_be_checked()
            page.wait_for_timeout(200)
            self.assertEqual(writes, [])
        finally:
            page.close()

    def test_selection_change_rerenders_checkboxes_without_workspace_change(self):
        page = self.new_page()
        try:
            self.goto_shell(page)
            result = page.evaluate("""async () => {
                await GmsWorkspace.ready;
                GmsWorkspace.update({scope_mode: 'single', device_ids: []},
                    {source: 'ui-smoke', persist: false});
                state.devices = [{device_id: 'A', status: 'online'}, {device_id: 'B', status: 'online'}];
                state.followFilter = false;
                state.selectedDevices = new Set(['A']);
                renderDevices();
                state.selectedDevices = new Set(['B']);
                renderDevices();
                return Array.from(document.querySelectorAll('.device-item input:checked'))
                    .map(input => input.closest('.device-item').dataset.deviceId);
            }""")
            self.assertEqual(result, ['B'])
        finally:
            page.close()

    def test_adb_connection_usb_only_and_unconfirmed_states_are_distinct(self):
        page = self.new_page()
        devices = [
            {'device_id': 'ADB', 'status': 'online', 'protocol': 'adb'},
            {'device_id': 'USB', 'status': 'online', 'protocol': 'usb_only'},
            {'device_id': 'STALE', 'status': 'online', 'stale': True},
            {'device_id': 'UNAUTHORIZED', 'status': 'unauthorized'},
            {'device_id': 'UNCONFIRMED'},
        ]
        try:
            page.route('**/api/devices/list*', lambda route: respond(route, devices))
            self.goto_shell(page)
            page.wait_for_function("typeof renderDevices === 'function'")
            page.evaluate("""devices => {
                state.devices = devices; state.selectedDevices = new Set();
                state.followFilter = false; renderDevices();
            }""", devices)
            expected = {'ADB': 'ADB 已连接', 'USB': 'USB-HID / AOA（待验证）', 'STALE': '状态待确认',
                        'UNAUTHORIZED': '未授权', 'UNCONFIRMED': '状态待确认'}
            for device_id, label in expected.items():
                card = page.locator(f'.device-item[data-device-id="{device_id}"]')
                expect(card.locator('.device-status')).to_have_text(label)
                if device_id == 'ADB':
                    expect(card.locator('input')).to_be_enabled()
                else:
                    expect(card.locator('input')).to_be_disabled()
            self.assertEqual(page.evaluate('selectedTestDeviceIds()'), [])
            self.assertFalse(page.evaluate("isSelectableTestDevice({status: 'online', protocol: 'usb_only'})"))
        finally:
            page.close()

    def test_failed_device_info_read_updates_the_original_device_status(self):
        page = self.new_page()
        devices = [{'device_id': 'SERIAL', 'status': 'online', 'protocol': 'adb'}]
        requests = []

        def info(route):
            requests.append(route.request.post_data_json)
            return respond(route, {'success': False, 'code': 'STATE_CONFLICT',
                                  'error': '设备 ADB 未连接，无法读取设备信息',
                                  'details': {'device': 'SERIAL', 'adb_state': 'absent'}}, 409)

        try:
            page.route('**/api/devices/list*', lambda route: respond(route, devices))
            page.route('**/api/devices/info', info)
            self.goto_shell(page)
            page.wait_for_function("typeof collectDeviceInfo === 'function'")
            page.evaluate("""devices => {
                state.devices = devices; state.selectedDevices = new Set(['SERIAL']);
                state.followFilter = false; renderDevices();
            }""", devices)
            card = page.locator('.device-item[data-device-id="SERIAL"]')
            expect(card.locator('.device-status')).to_have_text('ADB 已连接')
            page.locator('#btn-device-info').click()
            expect(card.locator('.device-status')).to_have_text('已断开')
            expect(card.locator('input')).to_be_disabled()
            self.assertEqual(requests, [{'devices': ['SERIAL']}])
            self.assertEqual(page.evaluate('selectedTestDeviceIds()'), [])
        finally:
            page.close()
