"""The AOA command-space grid overlay tracks the screen content box."""
import json

from playwright.sync_api import expect

from tests.runtime_ui.harness import RuntimeUiHarness


class RuntimeAoaScreenGridTests(RuntimeUiHarness):
    def prepare(self, page, inputs=None):
        def base_reply(route):
            if inputs is not None and route.request.url.endswith('/record/input'):
                inputs.append(route.request.post_data_json)
            route.fulfill(content_type='application/json', body=json.dumps({
                'success': True, 'profiles': [], 'command': 'click 200 540'}))
        page.route('**/api/devices/aoa/**', base_reply)
        for path in ('**/api/devices/management*', '**/api/cluster/devices*'):
            page.route(path, lambda route: route.fulfill(
                content_type='application/json', body='{"success":true,"devices":[]}'))
        page.add_init_script("try { localStorage.removeItem('aoa-screen-grid'); } catch (e) {}")
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
        }""")

    def test_grid_toggle_draws_command_space_over_the_content_box(self):
        page = self.new_page()
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        try:
            self.prepare(page)
            # 默认开启（首次使用无本地偏好即显示网格）。
            expect(page.locator('#aoa-screen-grid')).to_be_visible()
            assert page.evaluate("aoaScreenGridState().enabled") is True
            page.locator('#aoa-grid-toggle').click()
            expect(page.locator('#aoa-screen-grid')).to_be_hidden()
            assert page.evaluate("aoaScreenGridState().enabled") is False
            assert page.evaluate("document.getElementById('aoa-grid-toggle').textContent") == '⊞ 网格关'
            page.locator('#aoa-grid-toggle').click()
            expect(page.locator('#aoa-screen-grid')).to_be_visible()
            assert page.evaluate("document.getElementById('aoa-grid-toggle').textContent") == '⊞ 网格开'
            info = page.evaluate("""() => {
                const overlay = document.getElementById('aoa-screen-grid');
                const stage = document.getElementById('aoa-screen-stage');
                const media = document.getElementById('aoa-screen-canvas');
                const scale = Math.min(media.getBoundingClientRect().width / 720,
                    media.getBoundingClientRect().height / 1280);
                return {overlayW: overlay.getBoundingClientRect().width,
                    stageW: stage.getBoundingClientRect().width,
                    cover: overlay.style.width === `${stage.clientWidth}px`,
                    left: parseFloat(overlay.style.left), top: parseFloat(overlay.style.top),
                    contentW: 720 * scale, contentH: 1280 * scale,
                    button: document.getElementById('aoa-grid-toggle').textContent};
            }""")
            # 叠加层覆盖整个 stage（含外框黑边，刻度画在外面）。
            assert abs(info["overlayW"] - info["stageW"]) <= 3, info
            assert info['cover'] and info['left'] == 0 and info['top'] == 0, info
            assert info['button'] == '⊞ 网格开'
            # 布局尺寸变化后几何自动跟随。
            page.set_viewport_size({'width': 1100, 'height': 700})
            page.wait_for_timeout(150)  # ResizeObserver 异步触发后再断言
            after = page.evaluate("""() => {
                const overlay = document.getElementById('aoa-screen-grid');
                const stage = document.getElementById('aoa-screen-stage');
                return {overlayW: overlay.getBoundingClientRect().width,
                    stageW: stage.getBoundingClientRect().width};
            }""")
            assert abs(after["overlayW"] - after["stageW"]) <= 3, after
            # stage 上下各有 16px 内边距（CSS 为刻度留位）：X 刻度文字必须
            # 落在画面上方的外框里，画面内部 24px 深处不允许出现全不透明
            # 的标注像素（网格线是半透明的，alpha≤90，不算）。
            marks = page.evaluate("""() => {
                const overlay = document.getElementById('aoa-screen-grid');
                const media = document.getElementById('aoa-screen-canvas');
                const or = overlay.getBoundingClientRect();
                const mr = media.getBoundingClientRect();
                const scale = overlay.width / or.width;
                const ctx = overlay.getContext('2d');
                const probe = (x0, y0, x1, y1) => {
                    const data = ctx.getImageData(x0, y0, Math.max(1, x1 - x0), Math.max(1, y1 - y0)).data;
                    let opaque = 0;
                    for (let i = 3; i < data.length; i += 4) if (data[i] > 200) opaque += 1;
                    return opaque;
                };
                return {topBar: Math.max(1, Math.round((mr.top - or.top) * scale)),
                    outsideTop: probe(Math.round(60 * scale), 0, Math.round(200 * scale),
                        Math.max(1, Math.round((mr.top - or.top) * scale))),
                    inside: probe(Math.round((mr.left - or.left) * scale) + 24,
                        Math.round((mr.top - or.top) * scale) + 24,
                        Math.round((mr.left - or.left) * scale) + 84,
                        Math.round((mr.top - or.top) * scale) + 84)};
            }""")
            assert marks['topBar'] >= 14, marks       # stage padding 生效
            assert marks['outsideTop'] > 0, marks     # 上方外框有 X 刻度文字
            assert marks['inside'] == 0, marks        # 画面内部无标注像素
            assert errors == []
        finally:
            page.close()

    def test_hover_readout_reports_aoa_command_coordinates(self):
        page = self.new_page()
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        try:
            self.prepare(page)
            # 实时投屏按钮的开关状态必须可见可辨（文字 + data-live 样式钩子）。
            expect(page.locator('#aoa-toggle-live')).to_have_text('▶ 实时投屏')
            assert page.locator('#aoa-toggle-live').get_attribute('data-live') in (None, 'off')
            page.evaluate("""() => {
                let live = false;
                window.aoaLiveScreen = {
                    start: async () => { live = true; },
                    stop: () => { live = false; },
                    isActive: () => live};
            }""")
            page.locator('#aoa-toggle-live').click()
            expect(page.locator('#aoa-toggle-live')).to_have_text('⏹ 投屏中')
            assert page.locator('#aoa-toggle-live').get_attribute('data-live') == 'on'
            page.locator('#aoa-toggle-live').click()
            expect(page.locator('#aoa-toggle-live')).to_have_text('▶ 实时投屏')
            # 默认已开启（prepare 清空本地偏好）；确保状态为开。
            if page.evaluate("aoaScreenGridState().enabled") is not True:
                page.locator('#aoa-grid-toggle').click()
            expect(page.locator('#aoa-screen-grid')).to_be_visible()
            box = page.locator('#aoa-screen-canvas').bounding_box()
            # 内容框中心 = AOA (180, 320)。悬停 25% 处应读出 (90, 160)。
            page.mouse.move(box['x'] + box['width'] * 0.25,
                            box['y'] + box['height'] * 0.25)
            readout = page.evaluate("aoaScreenGridState().hover")
            assert readout is not None
            assert abs(readout['x'] - 90) <= 3
            assert abs(readout['y'] - 160) <= 3
            # 离开投屏后读数清空。
            page.mouse.move(box['x'] + box['width'] / 2, box['y'] - 40)
            expect(page.locator('#aoa-screen-grid')).to_be_visible()
            assert page.evaluate("aoaScreenGridState().hover") is None
            # 窄视口制造左右黑边：Y 刻度文字必须落在画面外的左侧黑边。
            page.set_viewport_size({'width': 900, 'height': 900})
            page.wait_for_timeout(150)  # ResizeObserver 异步触发后再断言
            marks = page.evaluate("""() => {
                const overlay = document.getElementById('aoa-screen-grid');
                const media = document.getElementById('aoa-screen-canvas');
                const or = overlay.getBoundingClientRect();
                const mr = media.getBoundingClientRect();
                const scale = overlay.width / or.width;
                const ctx = overlay.getContext('2d');
                const probe = (x0, y0, x1, y1) => {
                    const data = ctx.getImageData(x0, y0, Math.max(1, x1 - x0), Math.max(1, y1 - y0)).data;
                    let opaque = 0;
                    for (let i = 3; i < data.length; i += 4) if (data[i] > 200) opaque += 1;
                    return opaque;
                };
                const leftBar = Math.max(1, Math.round((mr.left - or.left) * scale));
                return {leftBar, outsideLeft: probe(0, Math.round(40 * scale), leftBar, overlay.height),
                    insideLeft: probe(leftBar + 10, Math.round(200 * scale), leftBar + 40, Math.round(320 * scale))};
            }""")
            assert marks['leftBar'] >= 20, marks        # 确实出现了黑边
            assert marks['outsideLeft'] > 0, marks      # 左侧黑边有 Y 刻度
            assert marks['insideLeft'] == 0, marks      # 画面内部无标注
            assert errors == []
        finally:
            page.close()

    def test_grid_input_never_reaches_the_recorder_path(self):
        page, inputs = self.new_page(), []
        try:
            self.prepare(page, inputs)
            # 默认已开启；网格是纯展示层，点击应直达录制路径。
            assert page.evaluate("aoaScreenGridState().enabled") is True
            expect(page.locator('#aoa-screen-grid')).to_be_visible()
            box = page.locator('#aoa-screen-canvas').bounding_box()
            # 在网格叠加层上点击/拖动：overlay pointer-events:none，
            # 输入必须落到下层 media 的录制路径且坐标不受网格影响。
            page.mouse.click(box['x'] + box['width'] * 0.5, box['y'] + box['height'] * 0.5)
            expect(page.locator('#aoa-run-status')).to_have_text('操作完成')
            assert len(inputs) == 1
            assert inputs[0]['input']['frame_size'] == [720, 1280]
            assert abs(inputs[0]['input']['x'] - 360) <= 1
            assert abs(inputs[0]['input']['y'] - 640) <= 1
        finally:
            page.close()


    def test_calibration_app_missing_guides_deploy_and_retries(self):
        """标定失败回执带 calibration_app_missing：弹引导 → 发起部署 →
        轮询安装完成 → 自动重试标定（mock 全链，不弹真审批页）。"""
        page = self.new_page()
        state = {"confirm": 0, "deploys": 0, "calibrations": 0}
        try:
            self.prepare(page)
            def route_handler(route):
                url = route.request.url
                post = route.request.post_data_json or {}
                if url.endswith('/record/input') and (post.get('input') or {}).get('action') == 'calibrate':
                    state["calibrations"] += 1
                    if state["calibrations"] == 1:
                        # 首次：worker 回执带结构化缺失标记 → 502 detail 透传
                        route.fulfill(status=502, content_type='application/json',
                                      body=json.dumps({"detail": {
                                          "message": "AOA 标定失败：请检查安全标定应用、独立验证点及 USB 状态",
                                          "calibration_app_missing": True}}))
                    else:
                        route.fulfill(content_type='application/json', body=json.dumps({
                            "success": True, "serial": "REC", "action": "calibrate",
                            "touch_calibration": {"measured": True, "verified": True,
                                                  "mapping": {"mapping_policy": "pointer_calibrated", "verified": True},
                                                  "context": {}}}))
                    return
                if url.endswith('/calibration-app/deploy'):
                    state["deploys"] += 1
                    route.fulfill(content_type='application/json', body=json.dumps({
                        "success": True, "deployment": {"id": "dep-1",
                        "approval_url": "https://review.example/dep-1", "status": "pending"}}))
                    return
                if url.endswith('/deployments/dep-1'):
                    route.fulfill(content_type='application/json', body=json.dumps({
                        "success": True, "deployment": {"id": "dep-1", "status": "completed"}}))
                    return
                route.fulfill(content_type='application/json', body=json.dumps({
                    'success': True, 'profiles': [], 'command': 'click 200 540'}))
            # 后注册的 route 优先级更高：放在 prepare 之后才能覆盖其 fallback。
            page.route('**/api/devices/aoa/**', route_handler)
            page.route('**/api/devices/deployments/**', route_handler)
            page.evaluate("""() => {
                window.__confirms = [];
                window.showConfirmDialog = async (title, message) => {
                    window.__confirms.push({title, message});
                    return true;
                };
                window.open = (url) => { window.__reviewUrl = url; };
            }""")
            page.locator('#aoa-grid-toggle').click()  # 预热 act-bridge 绑定页面
            page.evaluate("aoaCalibrateTouch()")
            page.wait_for_timeout(4500)
            summary = page.evaluate("""() => ({confirms: window.__confirms,
                review: window.__reviewUrl, status: document.getElementById('aoa-run-status').textContent})""")
            assert len(summary['confirms']) == 2, summary
            assert summary['confirms'][1]['title'] == '缺少 AOA 标定应用', summary
            assert summary['review'] == 'https://review.example/dep-1', summary
            expect(page.locator('#aoa-run-status')).to_have_text('操作完成')
            assert page.evaluate("aoaPanelState.calibration?.measured") is True, summary
            assert state["calibrations"] == 2 and state["deploys"] == 1, state
        finally:
            page.close()
