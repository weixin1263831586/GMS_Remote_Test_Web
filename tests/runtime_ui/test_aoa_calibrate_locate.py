"""AOA 命令坐标标定（②列表「定位」按钮）与投屏常亮标注。

标定模式在投屏叠加层画红色准星（AOA HID 触摸屏没有悬停报文，设备端
做不到"只移动不点击"，可视化全部发生在前端），点击画面重新取点并按轴
换算回写命令坐标；全程不执行任何设备输入。
"""
import json

from playwright.sync_api import expect

from tests.runtime_ui.harness import RuntimeUiHarness


class RuntimeAoaCalibrateLocateTests(RuntimeUiHarness):
    def prepare(self, page, inputs=None):
        def base_reply(route):
            if inputs is not None and route.request.url.endswith('/record/input'):
                inputs.append(route.request.post_data_json)
            route.fulfill(content_type='application/json', body=json.dumps({
                'success': True, 'profiles': [], 'command': 'click 200 540'}))
        page.route('**/api/devices/aoa/**', base_reply)
        # 页面初始化会真实调用 loadDevicesManagement（10s 自动刷新同源），
        # 其响应晚于测试 evaluate 的钉状态时会把清单清空（异步初始化竞态，
        # 网格随画面一起被重同步隐藏）。桩直接返回 REC 设备：无论在飞
        # 请求何时 resolve，清单都保持一致。
        page.route('**/api/devices/management*', lambda route: route.fulfill(
            content_type='application/json', body=json.dumps({
                'success': True, 'devices': [
                    {'serial_no': 'REC', 'worker_id': 'worker-a',
                     'transport': 'local_usb', 'state': 'available',
                     'status': 'available'}]})))
        page.route('**/api/cluster/devices*', lambda route: route.fulfill(
            content_type='application/json', body='{"success":true,"devices":[]}'))
        page.add_init_script(
            "try { localStorage.removeItem('aoa-screen-grid'); } catch (e) {}")
        self.goto_shell(page)
        page.evaluate("""async () => {
            switchPage('devices'); await aoaViewToggle(true);
            window.aoaLiveScreen = {start: async () => {}, stop: () => {}, isActive: () => true};
            aoaSyncDeviceSources([{serial_no:'REC',worker_id:'worker-a',transport:'local_usb',state:'available'}]);
            window.loadDevicesManagement = async () => {};
            aoaPanelState.liveTargetKey = 'worker-a:REC';
            const canvas = aoaEl('aoa-screen-canvas');
            canvas.width = 720; canvas.height = 1280; canvas.hidden = false;
            canvas.dataset.aoaFrameReady = '1';
            aoaEl('aoa-screen-image').hidden = true; aoaEl('aoa-screen-empty').hidden = true;
            // 直接注入两条命令（跳过录制）：click + sleep。
            aoaPanelState.commands = ['click 180 320', 'sleep 1000'];
            aoaRenderCommands();
        }""")
        # switchPage('devices') 触发的 loadDevicesManagement 在飞结算后，
        # 只读重同步会按清单刷新投屏区；等它先落地，再重钉画布状态，
        # 消除顺序竞态（与 test_aoa_screen_grid 的 prepare 同一模式）。
        page.wait_for_function(
            "() => (aoaPanelState.devices || []).some(d => d.serial_no === 'REC')")
        page.evaluate("""() => {
            aoaPanelState.liveTargetKey = 'worker-a:REC';
            const canvas = aoaEl('aoa-screen-canvas');
            canvas.width = 720; canvas.height = 1280; canvas.hidden = false;
            canvas.dataset.aoaFrameReady = '1';
            aoaEl('aoa-screen-image').hidden = true; aoaEl('aoa-screen-empty').hidden = true;
            aoaPanelState.commands = ['click 180 320', 'sleep 1000'];
            aoaRenderCommands();
        }""")

    def test_locate_marks_command_coordinate_and_rewrites_on_save(self):
        page, inputs = self.new_page(), []
        try:
            self.prepare(page, inputs)
            rows = page.locator('#aoa-command-list > li')
            expect(rows).to_have_count(2)
            # 网格默认开启：先关掉，验证标定标记不依赖网格。
            page.locator('#aoa-grid-toggle').click()
            expect(page.locator('#aoa-screen-grid')).to_be_hidden()
            rows.nth(0).get_by_role('button', name='定位第 1 条命令坐标').click()
            # 准星出现在 (180, 320)（命令坐标）。
            assert page.evaluate("aoaScreenGridState().calibrate") == {
                'index': 0, 'x': 180, 'y': 320}
            expect(page.locator('#aoa-screen-grid')).to_be_visible()
            expect(rows.nth(0).get_by_role('button', name='保存第 1 条命令标定坐标')
                   ).to_be_visible()
            # 布局回归：标定态 5 个按钮必须完整可见——删除按钮曾因第 5 列
            # 固定 156px 被挤出格子遮挡。
            row_layout = page.evaluate("""() => {
                const actions = document.querySelector('#aoa-command-list li .aoa-command-actions');
                const row = actions.closest('li');
                return {overflow: actions.scrollWidth > actions.clientWidth + 1,
                        inside: actions.getBoundingClientRect().right
                            <= row.getBoundingClientRect().right + 1,
                        delete: [...actions.querySelectorAll('button')]
                            .some(button => button.textContent === '删除'
                                && button.getBoundingClientRect().width > 0)};
            }""")
            assert not row_layout['overflow'], row_layout
            assert row_layout['inside'] and row_layout['delete'], row_layout
            # 点击画面右下角四分之一点：只取点，绝不走 /record/input。
            box = page.locator('#aoa-screen-canvas').bounding_box()
            page.mouse.click(box['x'] + box['width'] * 0.75,
                             box['y'] + box['height'] * 0.75)
            mark = page.evaluate("aoaScreenGridState().calibrate")
            # direct 模式按轴换算（宽 ×361/540、高 ×641/960）：
            # 540px→271、960px→481。
            assert (mark['x'], mark['y']) == (271, 481), mark
            assert inputs == [], inputs    # 标定取点不发设备输入
            # 保存：命令改写为新坐标，准星清除。
            rows.nth(0).get_by_role('button', name='保存第 1 条命令标定坐标').click()
            assert page.evaluate("aoaPanelState.commands[0]") == 'click 271 481'
            assert page.evaluate("aoaScreenGridState().calibrate") is None
            assert inputs == [], inputs
            # 状态提示带标定结果。
            status = page.evaluate("() => document.getElementById('aoa-run-status').textContent")
            assert '已标定' in status, status
        finally:
            page.close()

    def test_locate_preserves_longclick_kind_on_save(self):
        # 回归：标定只调整坐标，不允许把 longClick 命令回写成普通点击
        # （曾统一保存为 `click X Y`，长按回放静默变义）。
        page = self.new_page()
        try:
            self.prepare(page)
            rows = page.locator('#aoa-command-list > li')
            page.evaluate("""() => {
                aoaPanelState.commands = ['longClick 180 320', 'sleep 1000'];
                aoaRenderCommands();
            }""")
            rows.nth(0).get_by_role('button', name='定位第 1 条命令坐标').click()
            assert page.evaluate("aoaPanelState.commandCalibrate") is not None
            box = page.locator('#aoa-screen-canvas').bounding_box()
            page.mouse.click(box['x'] + box['width'] * 0.75,
                             box['y'] + box['height'] * 0.75)
            rows.nth(0).get_by_role('button', name='保存第 1 条命令标定坐标').click()
            assert page.evaluate("aoaPanelState.commands[0]") == 'longClick 271 481'
            assert page.evaluate("aoaScreenGridState().calibrate") is None
            status = page.evaluate("() => document.getElementById('aoa-run-status').textContent")
            assert '已标定为 longClick' in status, status
        finally:
            page.close()

    def test_scaled_video_center_stays_center_with_known_physical_display(self):
        page, inputs = self.new_page(), []
        try:
            self.prepare(page, inputs)
            page.evaluate("""() => {
                aoaPanelState.calibration = {context: {display: [1080, 1920]}};
                const canvas = aoaEl('aoa-screen-canvas');
                canvas.width = 576; canvas.height = 1024;
                aoaCommandCalibrateStart(0);
            }""")
            box = page.locator('#aoa-screen-canvas').bounding_box()
            page.mouse.click(box['x'] + box['width'] / 2, box['y'] + box['height'] / 2)
            mark = page.evaluate("aoaScreenGridState().calibrate")
            assert (mark['x'], mark['y']) == (181, 321), mark
            page.evaluate("aoaCommandCalibrateFinish(true)")
            assert page.evaluate("aoaPanelState.commands[0]") == 'click 181 321'
            assert inputs == []
        finally:
            page.close()

    def test_locate_cancel_restores_command_without_change(self):
        page = self.new_page()
        try:
            self.prepare(page)
            rows = page.locator('#aoa-command-list > li')
            rows.nth(0).get_by_role('button', name='定位第 1 条命令坐标').click()
            box = page.locator('#aoa-screen-canvas').bounding_box()
            page.mouse.click(box['x'] + box['width'] * 0.2,
                             box['y'] + box['height'] * 0.2)
            rows.nth(0).get_by_role('button', name='取消第 1 条命令标定').click()
            assert page.evaluate("aoaPanelState.commands[0]") == 'click 180 320'
            assert page.evaluate("aoaScreenGridState().calibrate") is None
        finally:
            page.close()

    def test_locate_rejects_non_click_command(self):
        page = self.new_page()
        try:
            self.prepare(page)
            rows = page.locator('#aoa-command-list > li')
            rows.nth(1).get_by_role('button', name='定位第 2 条命令坐标').click()
            assert page.evaluate("aoaPanelState.commandCalibrate") is None
            status = page.evaluate("() => document.getElementById('aoa-run-status').textContent")
            assert '无法标定' in status, status
        finally:
            page.close()

    def test_editing_command_while_calibrating_closes_the_mark(self):
        page = self.new_page()
        try:
            self.prepare(page)
            rows = page.locator('#aoa-command-list > li')
            rows.nth(0).get_by_role('button', name='定位第 1 条命令坐标').click()
            assert page.evaluate("aoaPanelState.commandCalibrate") is not None
            # 进编辑模式：准星自动撤销（互斥），命令不被改写。
            rows.nth(0).get_by_role('button', name='编辑第 1 条命令').click()
            assert page.evaluate("aoaPanelState.commandCalibrate") is None
            assert page.evaluate("aoaPanelState.commands[0]") == 'click 180 320'
            assert page.evaluate("aoaPanelState.commandEdit?.index") == 0
        finally:
            page.close()

    def test_deleting_or_reloading_commands_closes_stale_mark(self):
        page = self.new_page()
        try:
            self.prepare(page)
            rows = page.locator('#aoa-command-list > li')
            rows.nth(0).get_by_role('button', name='定位第 1 条命令坐标').click()
            # 删除命令：失效标记自动清准星。
            rows.nth(0).get_by_role('button', name='删除').click()
            assert page.evaluate("aoaPanelState.commandCalibrate") is None
            # 重新注入点击命令并再次标定，随后整体清空（确认框 stub 掉）。
            page.evaluate("""() => {
                window.showConfirmDialog = async () => true;
                aoaPanelState.commands = ['click 180 320', 'sleep 1000'];
                aoaRenderCommands();
                aoaCommandCalibrateStart(0);
            }""")
            assert page.evaluate("aoaPanelState.commandCalibrate") is not None
            page.evaluate("aoaCommandsClear()")
            assert page.evaluate("aoaPanelState.commandCalibrate") is None
        finally:
            page.close()
