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
                const titleImage = title.querySelector('img.section-title-icon, img.page-title-icon');
                if (navImage) return navImage.getAttribute('src') === titleImage?.getAttribute('src') ? [] : [item.dataset.page];
                return nav.textContent.trim() === title.querySelector('.page-title-icon')?.textContent.trim()
                    && !nav.querySelector('svg') ? [] : [item.dataset.page];
            })""")
            self.assertEqual(differences, [])
        finally:
            page.close()

    def test_file_picker_buttons_display_colored_folders(self):
        page = self.new_page()
        try:
            self.goto_shell(page)
            self.assertEqual(
                page.locator('#page-test button[data-click="browseRemoteFile"][data-a0="retry"] .test-action-icon').inner_text(),
                '📁',
            )
            self.assertEqual(page.locator('#gsi-modal button[data-click="browseLocalFileForGsiScript"]').inner_text(), '📁')
            self.assertEqual(page.locator('#gsi-modal button[data-click="browseLocalFileForGsiSystem"]').inner_text(), '📁')
            page.evaluate('selectReportSource()')
            self.assertEqual(
                page.locator('#report-source-modal button[data-click="selectReportFolder"] .report-choice-folder-icon').inner_text(),
                '📁',
            )
            page.evaluate('closeReportSourceModal()')
        finally:
            page.close()

    def test_test_page_actions_preserve_colorful_semantic_icons(self):
        page = self.new_page()
        try:
            self.goto_shell(page)
            data = page.evaluate(r"""() => {
                const root = document.querySelector('#page-test');
                const actions = [...root.querySelectorAll(
                    '.adb-button-frame button, .vnc-button-frame button, .vpn-btn-frame button'
                )];
                return actions.map(button => {
                    const use = button.querySelector('svg.ui-icon > use');
                    const svg = use?.closest('svg');
                    const emoji = button.querySelector('.test-action-icon');
                    const power = button.querySelector('.reboot-icon');
                    const icon = emoji || svg || power;
                    const originalPower = button.dataset.click === 'rebootDevices'
                        && power?.textContent.trim() === '⏻';
                    return {
                        label: button.textContent.replace(emoji?.textContent || '', '').replace(/^\s*⏻\s*/, '').trim(),
                        disabled: button.disabled,
                        symbol: use?.getAttribute('href')?.split('#')[1] || null,
                        emoji: emoji?.textContent.trim() || null,
                        decorative: !!icon && icon.getAttribute('aria-hidden') === 'true',
                        originalPower,
                        color: icon ? getComputedStyle(icon).color : null,
                        labelColor: getComputedStyle(button).color,
                        width: icon?.getBoundingClientRect().width || 0,
                    };
                });
            }""")
            self.assertGreaterEqual(len(data), 20)
            self.assertTrue(all((item['decorative'] and item['width'] > 5)
                                or item['originalPower'] for item in data), data)
            icons = {item['label']: item for item in data}
            for label, glyph in {
                '刷新设备': '🔄', '全选设备': '✅', '恢复出厂': '🧹',
                'Remount': '⚡',
                '连接 Wi-Fi': '🛜', '锁定设备': '🔒', '解锁设备': '🔓',
                '烧写固件': '🔥', '烧写 GSI': '🔥', '启动 VNC': '🚀',
                '设备投屏': '📺', 'ADB 接入': '🔌', 'USB/IP 接入': '📱',
                '复制部署命令': '📋',
            }.items():
                self.assertEqual(icons[label]['emoji'], glyph, (label, data))
            self.assertIsNone(icons['Remount']['symbol'])
            self.assertTrue(icons['重启设备']['originalPower'])
            self.assertIsNone(icons['重启设备']['symbol'])
            self.assertNotEqual(icons['重启设备']['color'],
                                icons['重启设备']['labelColor'])
            # The device toolbar now uses color Emoji plus the red power glyph,
            # so no SVG color requirement remains for these three action rows.
            self.assertEqual(icons['Remount']['emoji'], '⚡')
        finally:
            page.close()

    def test_test_device_action_toolbar_is_one_row_without_label_clipping(self):
        page = self.new_page()
        try:
            self.goto_shell(page)
            for width, height in ((1792, 856), (1440, 900), (1200, 800), (390, 844)):
                with self.subTest(width=width):
                    page.set_viewport_size({'width': width, 'height': height})
                    metrics = page.evaluate("""async () => {
                        await document.fonts.ready;
                        const bar = document.querySelector('#page-test .adb-button-frame');
                        const buttons = [...bar.querySelectorAll('button')];
                        const rects = buttons.map(button => button.getBoundingClientRect());
                        return {
                            actionCount: buttons.length,
                            toolbarWidth: bar.clientWidth,
                            contentWidth: bar.scrollWidth,
                            wrapping: rects.some(rect => Math.abs(rect.top - rects[0].top) > 1),
                            truncated: buttons.some(button => button.scrollWidth > button.clientWidth + 1),
                        };
                    }""")
                    self.assertEqual(metrics['actionCount'], 10, metrics)
                    self.assertFalse(metrics['wrapping'], (width, metrics))
                    self.assertFalse(metrics['truncated'], (width, metrics))
                    if width == 1792:
                        self.assertLessEqual(metrics['contentWidth'],
                                             metrics['toolbarWidth'] + 2,
                                             (width, metrics))
                    if width == 1792:
                        self.capture(page, 'test-adb-toolbar-single-row')
        finally:
            page.close()

    def test_test_console_compact_layout_gives_more_space_to_logs(self):
        page = self.new_page()
        try:
            self.goto_shell(page)
            for width, height in ((1792, 856), (1440, 900)):
                with self.subTest(width=width):
                    page.set_viewport_size({'width': width, 'height': height})
                    metrics = page.evaluate("""() => {
                        const root = document.querySelector('#page-test');
                        const rect = selector => root.querySelector(selector).getBoundingClientRect();
                        const progress = root.querySelector('.upload-frame .progress-container');
                        const adbButtons = [...root.querySelectorAll('.adb-button-frame button')];
                        return {
                            headers: ['.adb-frame > .page-title-row',
                                      '.control-frame > .section-title',
                                      '.log-frame-header'].map(x => rect(x).height),
                            canvasHeight: rect('.device-canvas').height,
                            logTop: rect('.log-frame').top,
                            middleTop: rect('.middle-row').top,
                            logHeight: rect('.log-text').height,
                            // Header used to draw a border directly above the
                            // log-text border; there must be exactly one line.
                            logHeaderBorderBottom: getComputedStyle(
                                root.querySelector('.log-frame-header')
                            ).borderBottomWidth,
                            logContentBorderTop: getComputedStyle(
                                root.querySelector('#log-tab-system .log-text')
                            ).borderTopWidth,
                            logHeaderBottom: rect('.log-frame-header').bottom,
                            logContentTop: rect('#log-tab-system .log-text').top,
                            deviceButtons: adbButtons.length,
                            progressDisplay: getComputedStyle(progress).display,
                        };
                    }""")
                    self.assertTrue(all(0 < x <= 34 for x in metrics['headers']), metrics)
                    self.assertGreaterEqual(metrics['canvasHeight'], 99, metrics)
                    self.assertLessEqual(metrics['canvasHeight'], 101, metrics)
                    self.assertEqual(metrics['deviceButtons'], 10, metrics)
                    self.assertEqual(metrics['progressDisplay'], 'none', metrics)
                    self.assertLess(metrics['logTop'] - metrics['middleTop'], 275, metrics)
                    self.assertGreater(metrics['logHeight'], 150, metrics)
                    self.assertEqual(metrics['logHeaderBorderBottom'], '0px', metrics)
                    self.assertEqual(metrics['logContentBorderTop'], '1px', metrics)
                    self.assertGreaterEqual(metrics['logContentTop'], metrics['logHeaderBottom'])
                    self.assertLessEqual(metrics['logContentTop'] - metrics['logHeaderBottom'], 4, metrics)
                    if width == 1792:
                        self.capture(page, 'test-console-compact-logs')
            progress = page.locator('#page-test .upload-frame .progress-container')
            info = page.locator('#progress-info')
            page.evaluate("document.getElementById('progress-info').textContent = '上传中 20%'")
            self.assertNotEqual(progress.evaluate("e => getComputedStyle(e).display"), 'none')
            info.evaluate("e => { e.textContent = ''; }")
            self.assertEqual(progress.evaluate("e => getComputedStyle(e).display"), 'none')
        finally:
            page.close()

    def test_device_list_shows_three_rows_then_scrolls_more_devices(self):
        page = self.new_page()
        try:
            self.goto_shell(page)
            for width, height in ((1792, 856), (1440, 900), (390, 844)):
                with self.subTest(width=width):
                    page.set_viewport_size({'width': width, 'height': height})
                    result = page.evaluate("""() => {
                        const canvas = document.getElementById('device-canvas');
                        const list = document.getElementById('device-list-left');
                        canvas.classList.remove('device-canvas-empty');
                        list.style.gridTemplateColumns = 'minmax(0, 1fr)';
                        list.innerHTML = Array.from({length: 7}, (_, index) =>
                            '<div class="device-item"><span>设备' + index + '</span></div>'
                        ).join('');
                        list.scrollTop = 0;
                        const cards = [...list.querySelectorAll('.device-item')];
                        const positions = cards.map(el => el.getBoundingClientRect());
                        const top = list.getBoundingClientRect().top;
                        const bottom = list.getBoundingClientRect().bottom;
                        const before = {
                            canvasHeight: canvas.getBoundingClientRect().height,
                            clientHeight: list.clientHeight,
                            scrollHeight: list.scrollHeight,
                            overflow: getComputedStyle(list).overflowY,
                            thirdBottom: positions[2].bottom,
                            fourthTop: positions[3].top,
                            fourthBottom: positions[3].bottom,
                            viewportBottom: bottom,
                            viewportTop: top,
                        };
                        list.scrollTop = list.scrollHeight;
                        return {
                            ...before,
                            scrollTop: list.scrollTop,
                            seventhBottom: cards[6].getBoundingClientRect().bottom,
                            visibleBottom: list.getBoundingClientRect().bottom,
                        };
                    }""")
                    self.assertGreaterEqual(result['canvasHeight'], 99, result)
                    self.assertLessEqual(result['canvasHeight'], 101, result)
                    self.assertLessEqual(result['thirdBottom'], result['viewportBottom'] - 0.5, result)
                    # Assert row 4 STARTS below the viewport, not merely that it ENDS
                    # below it (the older assertion allowed a partial fourth row).
                    self.assertGreaterEqual(result['fourthTop'],
                                            result['viewportBottom'] + 0.5, result)
                    self.assertGreater(result['fourthBottom'], result['viewportBottom'], result)
                    self.assertGreater(result['scrollHeight'], result['clientHeight'], result)
                    self.assertEqual(result['overflow'], 'auto', result)
                    self.assertGreater(result['scrollTop'], 0, result)
                    self.assertLessEqual(result['seventhBottom'], result['visibleBottom'] + 1, result)
                    if width == 1792:
                        self.capture(page, 'test-three-device-rows-scroll')
        finally:
            page.close()

    def test_suite_refresh_and_test_toggle_have_requested_icons_and_bounds(self):
        page = self.new_page()
        try:
            self.goto_shell(page)
            suite = page.locator('#refresh-suites-btn')
            self.assertEqual(suite.locator('.test-action-icon').inner_text(), '🔄')
            states = (
                (False, False, False, '开始测试', 'btn-primary', False),
                (False, True, False, '启动中', 'btn-primary', True),
                (True, False, False, '停止测试', 'btn-danger', False),
                (True, False, True, '停止中', 'btn-danger', True),
            )
            for testing, starting, stopping, label, css_class, disabled in states:
                with self.subTest(label=label):
                    result = page.evaluate("""({testing, starting, stopping}) => {
                        state.testStarting = starting;
                        state.testStopping = stopping;
                        updateTestToggleButton(testing);
                        const btn = document.getElementById('test-toggle-btn');
                        return {
                            label: btn.textContent.trim(),
                            icons: btn.querySelectorAll('svg, .test-action-icon').length,
                            disabled: btn.disabled,
                            className: btn.className,
                            width: btn.getBoundingClientRect().width,
                            height: btn.getBoundingClientRect().height,
                        };
                    }""", {'testing': testing, 'starting': starting, 'stopping': stopping})
                    self.assertEqual(result['label'], label, result)
                    self.assertEqual(result['icons'], 0, result)
                    self.assertEqual(result['disabled'], disabled, result)
                    self.assertIn(css_class, result['className'])
                    self.assertLessEqual(result['width'], 100, result)
                    self.assertGreaterEqual(result['height'], 34, result)
            page.evaluate("""() => {
                state.testStarting = false;
                state.testStopping = false;
                updateTestToggleButton(false);
            }""")
        finally:
            page.close()

    def test_device_connection_dialogs_use_color_plug_title_icons(self):
        page = self.new_page()
        try:
            self.goto_shell(page)
            for modal_id, title in (('adb-proxy-modal', 'ADB设备接入'),
                                    ('usbip-attach-modal', 'USB/IP设备接入')):
                with self.subTest(modal_id=modal_id):
                    data = page.locator(f'#{modal_id} .tech-title-group').evaluate("""node => ({
                        icon: node.querySelector('.tech-title-glyph')?.textContent.trim(),
                        svgCount: node.querySelectorAll('.tech-title-glyph svg').length,
                        title: node.querySelector('.modal-title')?.textContent.trim(),
                    })""")
                    self.assertEqual(data, {'icon': '🔌', 'svgCount': 0, 'title': title})
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
                            headerHeight: header.height,
                            headerGap: getComputedStyle(panel.querySelector('.aoa-workbench-header')).gap,
                            headerBottomMargin: getComputedStyle(panel.querySelector('.aoa-workbench-header')).marginBottom,
                            statusTopPadding: getComputedStyle(panel.querySelector('#aoa-run-status')).paddingTop,
                            statusBottomPadding: getComputedStyle(panel.querySelector('#aoa-run-status')).paddingBottom,
                        };
                    }""")
                    self.assertGreaterEqual(geometry['top'], 0)
                    self.assertGreaterEqual(geometry['bottom'], 0)
                    self.assertLessEqual(geometry['overflow'], 1)
                    self.assertEqual(geometry['headerBottomMargin'], '6px')
                    self.assertEqual(geometry['statusTopPadding'], '3px')
                    self.assertEqual(geometry['statusBottomPadding'], '3px')
                    self.assertEqual(geometry['headerGap'], '4px' if width <= 760 else '10px')
                    self.assertLessEqual(geometry['headerHeight'], 68 if width <= 760 else 36)
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
            expect(rows.first.locator('td')).to_have_count(12)
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
