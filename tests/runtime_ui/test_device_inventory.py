"""Inventory filters and pagination keep selections local and device targets intact."""

import json
import os
from pathlib import Path

from tests.runtime_ui.harness import RuntimeUiHarness, expect


class RuntimeDeviceInventoryTests(RuntimeUiHarness):
    def open_inventory(self, page):
        devices = [{
            'device_id': '192.0.2.50:5555' if index == 0 else f'USB-{index:03}',
            'serial_no': f'HW-{index:03}', 'model': f'RK3588 开发板 {index + 1}',
            'worker_id': 'ats-worker-controller', 'host_display_name': 'ats-worker-controller',
            'source_type': 'usbip' if index % 3 == 0 else 'local', 'source_host': 'hcq@172.16.14.233',
            'android_version': '14' if index % 2 == 0 else '15', 'soc_model': 'RK3588',
            'status': 'online' if index < 28 else 'fastboot',
            'protocol': 'adb' if index < 28 else 'fastboot', 'battery_level': index * 3,
        } for index in range(34)]
        devices[1]['locked_by'] = 'qa-user'
        devices[0]['name'] = '测试设备 A'
        groups = [{'id': 'qa', 'name': '实验室 / RK3588', 'color': '#8b74e8',
                   'device_ids': [device['device_id'] for device in devices[:3]]}]
        writes = []

        def group_api(route):
            if route.request.method != 'GET':
                writes.append(route.request.post_data_json)
            route.fulfill(status=200, content_type='application/json',
                          body=json.dumps({'success': True, 'data': {'groups': groups}}))

        page.route('**/api/device-groups', group_api)
        page.route('**/api/devices/management*', lambda route: route.fulfill(
            status=200, content_type='application/json', body=json.dumps({'success': True, 'devices': devices})))
        self.goto_shell(page)
        page.evaluate("""async ({devices, groups}) => {
            switchPage('devices', null); await loadDevicesManagement(); stopDevicesAutoRefresh();
            devicesManagementClusterMode = false;
            state.deviceGroups = groups; state.groupFilter = ''; state.collapsedGroups.clear();
            allDevices = devices; displayDevicesManagement(allDevices);
        }""", {'devices': devices, 'groups': groups})
        return devices, writes

    def test_filters_and_pagination_show_real_matching_inventory(self):
        page = self.new_page()
        try:
            _, writes = self.open_inventory(page)
            rows = page.locator('#devices-table-body tr[data-device-id]')
            expect(rows).to_have_count(20)
            expect(page.locator('#inventory-total')).to_have_text('共 34 台，已筛选 34 台')
            page.get_by_label('每页设备数').select_option('50')
            expect(rows).to_have_count(34)
            page.get_by_label('每页设备数').select_option('20')
            expect(rows).to_have_count(20)
            page.get_by_role('navigation', name='设备列表分页').get_by_role('button', name='2', exact=True).click()
            expect(rows).to_have_count(14)
            page.locator('#inventory-connection').select_option('usbip')
            expect(rows).to_have_count(12)
            expect(rows.first.locator('[data-device-field="group"]')).to_have_text('实验室 / RK3588')
            page.locator('#inventory-system').select_option('Android 15')
            expect(rows).to_have_count(6)
            page.locator('#inventory-runtime').select_option('fastboot')
            expect(rows).to_have_count(1)
            expect(rows.first.locator('.device-serial')).to_have_text('HW-033')
            page.get_by_role('button', name='重置筛选').click()
            expect(rows).to_have_count(20)
            page.locator('#inventory-occupancy').select_option('occupied')
            expect(rows).to_have_count(1)
            expect(rows.first.locator('.device-serial')).to_have_text('HW-001')
            page.get_by_role('button', name='重置筛选').click()
            page.locator('#inventory-search').fill('HW-000')
            expect(rows).to_have_count(1)
            self.assertEqual(rows.first.locator('[data-click="openDeviceShell"]').get_attribute('data-serial'),
                             '192.0.2.50:5555')
            page.locator('#inventory-search').fill('没有匹配的设备')
            expect(rows).to_have_count(0)
            expect(page.locator('#devices-table-body')).to_contain_text('暂无符合条件的设备')
            expect(page.locator('#inventory-select-page')).to_be_disabled()
            self.assertEqual(writes, [])
        finally:
            page.close()

    def test_selection_across_pages_prefills_group_without_implicit_writes(self):
        page = self.new_page()
        try:
            devices, writes = self.open_inventory(page)
            page.evaluate("state.selectedDevices = new Set(['test-page-selection'])")
            page.locator('#inventory-select-page').check()
            expect(page.locator('#inventory-selected-count')).to_have_text('20')
            page.get_by_role('navigation', name='设备列表分页').get_by_role('button', name='2', exact=True).click()
            expect(page.locator('#inventory-select-page')).not_to_be_checked()
            page.locator('#inventory-select-page').check()
            expect(page.locator('#inventory-selected-count')).to_have_text('34')
            page.locator('#inventory-search').fill('HW-000')
            expect(page.locator('#inventory-selected-count')).to_have_text('0')
            page.locator('.inventory-device-select').check()
            expect(page.locator('#inventory-selected-count')).to_have_text('1')
            page.locator('#inventory-create-group').click()
            expect(page.locator('#device-group-modal')).to_be_visible()
            picked = page.locator('#device-group-picklist input:checked')
            expect(picked).to_have_count(1)
            self.assertEqual(picked.input_value(), devices[0]['device_id'])
            self.assertEqual(page.evaluate('[...state.selectedDevices]'), ['test-page-selection'])
            self.assertEqual(writes, [])
            page.locator('#device-group-name').fill('所选设备')
            page.locator('[data-click="submitDeviceGroup"]').click()
            expect(page.locator('#device-group-modal')).not_to_be_visible()
            self.assertEqual(writes, [{'action': 'create', 'name': '所选设备',
                                       'device_ids': ['192.0.2.50:5555']}])
        finally:
            page.close()

    def test_list_controls_stay_within_page_and_header_is_unchanged(self):
        page = self.new_page()
        try:
            self.open_inventory(page)
            page.evaluate("devicesManagementClusterMode = true; displayDevicesManagement(allDevices)")
            header = page.locator('#page-devices > .page-header-row')
            before = header.inner_html()
            for sort in ('serial_no', 'model', 'battery_level'):
                expect(page.locator(f'#sort-{sort}').locator('..').locator('..').locator('button')).to_have_count(1)
            host = page.locator('#devices-table-body [data-device-field="host"]').first
            expect(host).to_have_text('hcq@172.16.14.233')
            self.assertIn('ats-worker-controller', host.locator('[title]').get_attribute('title'))
            expect(page.locator('#devices-table-body')).not_to_contain_text('ats-worker-controller')
            directory = os.getenv('GMS_UI_PREVIEW_DIR')
            if directory:
                Path(directory).mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(Path(directory) / 'device-inventory.png'))
            for width in (1920, 1440, 900, 390):
                with self.subTest(width=width):
                    page.set_viewport_size({'width': width, 'height': 960})
                    overflow = page.evaluate('document.documentElement.scrollWidth - innerWidth')
                    self.assertLessEqual(overflow, 1)
                    self.assertEqual(header.inner_html(), before)
                    rows_are_single_line = page.evaluate("""() =>
                        [...document.querySelectorAll('#devices-table-body tr[data-device-id]')].every(row =>
                            row.getBoundingClientRect().height <= 45 && [...row.querySelectorAll('.ui-stack')].every(stack => {
                                const children = [...stack.children].map(child => child.getBoundingClientRect());
                                return children.every(r => Math.abs(r.top + r.height / 2
                                    - children[0].top - children[0].height / 2) < 1);
                            }))""")
                    self.assertTrue(rows_are_single_line)
                    if width >= 1440:
                        actions_fit = page.evaluate("""() =>
                            [...document.querySelectorAll('#devices-table-body tr[data-device-id]')].every(row => {
                                const cell = row.lastElementChild.getBoundingClientRect();
                                return cell.right <= innerWidth && [...row.querySelectorAll('.device-row-actions button')]
                                    .every(button => button.getBoundingClientRect().right <= cell.right - 4);
                            })""")
                        self.assertTrue(actions_fit)
                    for control in page.locator('.inventory-filters input, .inventory-filters select').all():
                        self.assertTrue(control.evaluate("""node => {
                            const r = node.getBoundingClientRect();
                            return r.left >= 0 && r.right <= innerWidth && r.width > 0;
                        }"""))
                    expect(page.locator('.inventory-footer')).to_be_in_viewport()
        finally:
            page.close()
