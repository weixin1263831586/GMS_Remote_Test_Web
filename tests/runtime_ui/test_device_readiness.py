"""Connection labels must describe observed ADB state and reflect failed reads."""
import json

from playwright.sync_api import expect

from tests.runtime_ui.harness import RuntimeUiHarness


def respond(route, payload, status=200):
    route.fulfill(status=status, content_type='application/json', body=json.dumps(payload))


class RuntimeDeviceReadinessTests(RuntimeUiHarness):
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
