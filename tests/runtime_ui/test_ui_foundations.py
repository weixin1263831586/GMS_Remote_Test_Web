"""Shared presentation preserves identity, device targeting and responsive geometry."""

import json
import os
from pathlib import Path

from tests.runtime_ui.harness import RuntimeUiHarness, expect


class RuntimeUiFoundationsTests(RuntimeUiHarness):
    def capture(self, page, name):
        directory = os.getenv('GMS_UI_PREVIEW_DIR')
        if directory:
            target = Path(directory)
            target.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(target / f'{name}.png'))

    def test_original_navigation_icons_match_page_titles(self):
        page = self.new_page()
        try:
            self.goto_shell(page)
            differences = page.evaluate("""() => [...document.querySelectorAll('.sidebar-item')].flatMap(item => {
                const title = document.querySelector(`#page-${item.dataset.page} .page-header-row .section-title`);
                if (!title) return [];
                const nav = item.querySelector('.sidebar-icon');
                const navImage = nav.querySelector('img');
                const titleImage = title.querySelector('img.section-title-icon');
                if (navImage) return navImage.getAttribute('src') === titleImage?.getAttribute('src') ? [] : [item.dataset.page];
                return nav.textContent.trim() === title.querySelector('.page-title-icon')?.textContent.trim()
                    && !nav.querySelector('svg') ? [] : [item.dataset.page];
            })""")
            self.assertEqual(differences, [])
        finally:
            page.close()

    def test_aoa_controls_stay_reachable_across_workspace_sizes(self):
        page = self.new_page()
        try:
            page.route('**/api/devices/aoa/**', lambda route: route.fulfill(
                status=200, content_type='application/json', body='{"success":true,"profiles":[]}'))
            page.route('**/api/devices/management*', lambda route: route.fulfill(
                status=200, content_type='application/json', body='{"success":true,"devices":[]}'))
            self.goto_shell(page)
            page.evaluate("""async () => {
                switchPage('devices', null); await aoaViewToggle(true); stopDevicesAutoRefresh();
                aoaPanelState.commands = Array.from({length: 30}, (_, i) => `sleep ${i * 100}`);
                aoaPanelState.commands[0] = 'click 180 320';
                aoaRenderCommands();
                document.querySelector('.aoa-semantic-tools').open = true;
                for (const select of document.querySelectorAll('#aoa-panel select')) {
                    select.add(new Option('很长的设备或配置名称'.repeat(12), 'geometry-fixture', true, true));
                }
            }""")
            for width, height in ((1920, 1080), (1440, 900), (1200, 800), (900, 800), (390, 844)):
                with self.subTest(width=width, height=height):
                    page.set_viewport_size({'width': width, 'height': height})
                    geometry = page.evaluate("""() => {
                        const panel = document.querySelector('#aoa-panel');
                        panel.scrollTop = 0;
                        const header = panel.querySelector('.aoa-workbench-header').getBoundingClientRect();
                        const badge = panel.querySelector('#aoa-run-status').getBoundingClientRect();
                        const columns = [...panel.querySelectorAll('.aoa-column')].map(n => n.getBoundingClientRect().width);
                        const grid = panel.querySelector('.aoa-workbench-grid');
                        grid.style.gridTemplateColumns = 'minmax(280px, .95fr) minmax(330px, 1.05fr) minmax(330px, 1.08fr)';
                        const previous = [...panel.querySelectorAll('.aoa-column')].map(n => n.getBoundingClientRect().width);
                        grid.style.removeProperty('grid-template-columns');
                        return {
                            columns, previous, top: badge.top - header.top, bottom: header.bottom - badge.bottom,
                            overflow: document.documentElement.scrollWidth - innerWidth,
                        };
                    }""")
                    self.assertGreaterEqual(geometry['top'], 0)
                    self.assertGreaterEqual(geometry['bottom'], 0)
                    self.assertLessEqual(geometry['overflow'], 1)
                    if width >= 1181:
                        self.assertLess(geometry['columns'][0], geometry['previous'][0])
                        self.assertGreater(geometry['columns'][1], geometry['previous'][1])
                        self.assertAlmostEqual(geometry['columns'][2], geometry['previous'][2], delta=1)
                    for mode in ('idle', 'edit', 'calibrate', 'playback'):
                        page.evaluate("""mode => {
                            aoaPanelState.commandEdit = null;
                            aoaCommandCalibrateCancel();
                            aoaPanelState.playbackActive = mode === 'playback';
                            aoaPanelState.playbackCommandId = mode === 'playback' ? 'geometry-fixture' : '';
                            aoaPanelState.playbackStates = ['reconnecting'];
                            aoaRenderCommands(); aoaPlaybackButtonState();
                            if (mode === 'edit') aoaCommandEditStart(0);
                            if (mode === 'calibrate') aoaCommandCalibrateStart(0);
                        }""", mode)
                        failures = page.evaluate("""() => {
                        const panel = document.querySelector('#aoa-panel');
                        const failures = [];
                        for (const node of panel.querySelectorAll('button, input, select, textarea, summary')) {
                            if (!node.checkVisibility({visibilityProperty: true})) continue;
                            node.scrollIntoView({block: 'center', inline: 'nearest', behavior: 'instant'});
                            const r = node.getBoundingClientRect();
                            const column = node.closest('.aoa-column')?.getBoundingClientRect();
                            const name = node.id || node.dataset.click || node.tagName;
                            if (column && (r.left < column.left - 1 || r.right > column.right + 1))
                                failures.push(`${name}: outside column`);
                            const dx = Math.min(4, r.width / 4), dy = Math.min(4, r.height / 4);
                            const corners = [[r.left + dx, r.top + dy], [r.right - dx, r.top + dy],
                                [r.left + dx, r.bottom - dy], [r.right - dx, r.bottom - dy]];
                            if (corners.some(([x, y]) => {
                                const hit = document.elementFromPoint(x, y);
                                return !hit || (hit !== node && !node.contains(hit));
                            })) failures.push(`${name}: clipped or covered`);
                        }
                        return failures;
                        }""")
                        self.assertEqual(failures, [], mode)
        finally:
            page.close()

    def test_inventory_separates_connection_mode_and_claim_and_keeps_action_target(self):
        page = self.new_page()
        try:
            devices = [
                {'device_id': '192.0.2.50:5555', 'serial_no': 'HW-001', 'model': 'RK3588 测试板',
                 'source_type': 'local', 'source_host': 'tester@lab', 'worker_id': 'lab-worker',
                 'status': 'online', 'protocol': 'adb', 'android_version': '14', 'soc_model': 'RK3588', 'battery_level': 0},
                {'device_id': 'FB-002', 'serial_no': 'FB-002', 'model': '烧写设备',
                 'source_type': 'usbip', 'source_host': '<img src=x onerror=alert(1)>', 'status': 'fastboot',
                 'protocol': 'fastboot', 'locked_by': 'qa-user'},
                {'device_id': 'USB-003', 'serial_no': 'USB-003', 'model': '待恢复设备',
                 'source_type': 'adb_proxy', 'status': 'usb_only', 'protocol': 'usb_only'},
            ]
            page.route('**/api/devices/management*', lambda route: route.fulfill(
                status=200, content_type='application/json', body=json.dumps({'success': True, 'devices': devices})))
            self.goto_shell(page)
            page.evaluate("""devices => {
                switchPage('devices', null); stopDevicesAutoRefresh();
                devicesManagementClusterMode = false; state.deviceGroups = [];
                allDevices = devices;
                displayDevicesManagement(allDevices);
            }""", devices)
            rows = page.locator('#devices-table-body tr[data-device-id]')
            expect(rows).to_have_count(3)
            expect(rows.first.locator('td')).to_have_count(7)
            expect(rows.first.locator('[data-device-field="connection"]')).to_contain_text('网络 ADB')
            expect(rows.first.locator('[data-device-field="runtime"]')).to_have_text('ADB 在线')
            expect(rows.first.locator('[data-device-field="occupancy"]')).to_have_text('未占用')
            expect(rows.first.locator('[data-device-field="battery"]')).to_have_text('0%')
            self.assertEqual(rows.first.locator('[data-click="openDeviceShell"]').get_attribute('data-serial'), '192.0.2.50:5555')
            expect(rows.nth(1).locator('[data-device-field="runtime"]')).to_contain_text('Fastboot')
            expect(rows.nth(1).locator('[data-device-field="occupancy"]')).to_contain_text('已占用')
            expect(rows.nth(1).locator('[data-click="openDeviceShell"]')).to_be_disabled()
            expect(rows.nth(1).locator('img')).to_have_count(0)
            expect(rows.nth(2).locator('[data-device-field="runtime"]')).to_contain_text('ADB 未连接')
            self.capture(page, 'devices')
            page.set_viewport_size({'width': 900, 'height': 800})
            geometry = page.evaluate("""() => ({
                body: document.documentElement.scrollWidth, viewport: innerWidth,
                table: document.querySelector('.devices-table-wrap').scrollWidth,
                panel: document.querySelector('.devices-table-wrap').clientWidth,
            })""")
            self.assertLessEqual(geometry['body'], geometry['viewport'])
            self.assertGreater(geometry['table'], geometry['panel'])
        finally:
            page.close()

    def test_agent_timeline_and_aoa_sections_keep_real_controls(self):
        page = self.new_page()
        try:
            self.goto_shell(page)
            page.evaluate("""() => {
                switchPage('agent', null);
                const now = new Date().toISOString();
                renderAgentMessages({messages: [
                    {role: 'user', content: '在空闲设备上运行 CtsWifiTestCases，完成后分析报告。', created_at: now},
                    {role: 'assistant', content: '已检查设备与套件，正在执行测试。', created_at: now},
                ]});
                renderAgentSteps({status: 'monitoring', steps: [
                    {title: '检查设备资源', status: 'done', detail: 'lab-worker · RK3588 · 设备已就绪', created_at: now},
                    {title: '解析测试套件', status: 'done', detail: 'Android 14 CTS / CtsWifiTestCases', created_at: now},
                    {title: '执行测试', status: 'running', detail: '<img src=x onerror=alert(1)> 作为文本展示', created_at: now},
                    {title: '报告分析', status: 'pending', detail: '等待测试结束', created_at: now},
                ]});
            }""")
            expect(page.locator('.agent-step')).to_have_count(4)
            expect(page.locator('.agent-step[data-status="running"] .ui-status')).to_have_text('进行中')
            expect(page.locator('#agent-steps img')).to_have_count(0)
            expect(page.locator('.agent-avatar svg')).to_have_count(2)
            self.capture(page, 'agent')
            page.route('**/api/devices/aoa/**', lambda route: route.fulfill(
                status=200, content_type='application/json', body='{"success":true,"profiles":[]}'))
            page.evaluate("""async () => {
                switchPage('devices', null); await aoaViewToggle(true);
                aoaPanelState.commands = ['tap 180 420', 'sleep 500', 'key HOME'];
                aoaRenderCommands();
            }""")
            expect(page.locator('.aoa-operation-heading')).to_contain_text('录制')
            expect(page.locator('.aoa-save-block')).to_contain_text('保存配置')
            expect(page.locator('.aoa-target-section')).to_contain_text('回放')
            expect(page.locator('#aoa-record-start')).to_have_count(1)
            expect(page.locator('#aoa-record-stop')).to_have_count(1)
            self.capture(page, 'aoa')
        finally:
            page.close()
