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


    def test_screenshot_polling_pauses_on_no_adb_and_resumes(self):
        """usb_only 设备截图 409（无 ADB）：首次失败后轮询暂停不再打请求；
        设备清单刷新（ADB 就绪）后自动恢复截图并清除提示。"""
        page = self.new_page()
        state = {"shots": 0, "no_adb": True}
        try:
            def route_handler(route):
                url = route.request.url
                if url.endswith('/api/cluster/devices/actions'):
                    if (route.request.post_data_json or {}).get('action') == 'screenshot':
                        state["shots"] += 1
                        if state["no_adb"]:
                            route.fulfill(status=409, content_type='application/json', body=json.dumps(
                                {"detail": "device has no ADB connection; only AOA recovery is available: REC"}))
                        else:
                            route.fulfill(content_type='application/json', body=json.dumps({
                                "accepted": True, "command_id": "cmd-1"}))
                        return
                    route.fulfill(content_type='application/json', body=json.dumps({
                        "accepted": True, "command_id": "cmd-other"}))
                    return
                if '/api/cluster/commands/cmd-1' in url:
                    route.fulfill(content_type='application/json', body=json.dumps({
                        "command": {"status": "completed", "result": {"image": "data:image/png;base64,xx"}}}))
                    return
                if '/api/cluster/commands/' in url:
                    route.fulfill(content_type='application/json', body=json.dumps({
                        "command": {"status": "completed", "result": {}}}))
                    return
                route.fulfill(content_type='application/json', body=json.dumps({
                    'success': True, 'devices': []}))
            page.route('**/api/cluster/**', route_handler)
            page.route('**/api/devices/aoa/**', route_handler)
            page.route('**/api/devices/management*', route_handler)
            self.goto_shell(page)
            page.evaluate("""async () => {
                switchPage('devices'); await aoaViewToggle(true);
                // isActive=false：本测试走截图路径（实时流未激活）。
                window.aoaLiveScreen = {start: async () => {}, stop: () => {}, isActive: () => false};
                aoaSyncDeviceSources([{serial_no:'REC',worker_id:'worker-a',transport:'local_usb',state:'available'}]);
                window.loadDevicesManagement = async () => {};
                aoaPanelState.liveTargetKey = 'worker-a:REC';
                aoaEl('aoa-screen-image').hidden = true; aoaEl('aoa-screen-empty').hidden = true;
            }""")
            # 第一跳：409 → 标记无 ADB。
            page.evaluate("aoaRefreshScreen()")
            page.wait_for_timeout(200)
            assert state["shots"] == 1, state["shots"]
            empty_text = page.evaluate("() => document.getElementById('aoa-screen-empty').textContent")
            assert '无 ADB' in empty_text, empty_text

            # 再打 5 跳（模拟轮询）：不再发请求。
            for _ in range(5):
                page.evaluate("aoaRefreshScreen()")
            page.wait_for_timeout(200)
            assert state["shots"] == 1, state["shots"]
            waiting = page.evaluate("() => document.getElementById('aoa-screen-status').textContent")
            assert '等待设备就绪' in waiting, waiting

            # 设备就绪：清单同步复位标记 → 下一跳恢复截图成功。
            state["no_adb"] = False
            page.evaluate(
                "aoaSyncDeviceSources([{serial_no:'REC',worker_id:'worker-a',transport:'local_usb',state:'available'}])")
            page.evaluate("aoaRefreshScreen()")
            page.wait_for_timeout(600)
            assert state["shots"] == 2, state["shots"]
            status = page.evaluate("() => document.getElementById('aoa-screen-status').textContent")
            assert '已连接' in status, status
        finally:
            page.close()

    def test_live_stream_survives_static_screen_and_falls_back_on_pong_timeout(self):
        """静态画面（无新帧）不再被判流死亡；pong 心跳超时才回退截图模式。"""
        page = self.new_page()
        try:
            def route_handler(route):
                url = route.request.url
                if url.endswith('/stream/REC/start'):
                    route.fulfill(content_type='application/json', body=json.dumps({
                        "success": True, "ws_url": "ws://fake/stream"}))
                    return
                if url.endswith('/api/cluster/devices/actions'):
                    route.fulfill(content_type='application/json', body=json.dumps({
                        "accepted": True, "command_id": "cmd-1"}))
                    return
                if '/api/cluster/commands/cmd-1' in url:
                    # 回退后的截图轮询要能成功出图，否则错误状态会覆盖
                    # 心跳超时提示。
                    route.fulfill(content_type='application/json', body=json.dumps({
                        "command": {"status": "completed",
                                    "result": {"image": "data:image/png;base64,xx"}}}))
                    return
                route.fulfill(content_type='application/json', body=json.dumps({"success": True}))
            page.route('**/api/devices/aoa/**', route_handler)
            page.route('**/api/cluster/**', route_handler)
            self.goto_shell(page)
            page.evaluate("""async () => {
                switchPage('devices'); await aoaViewToggle(true);
                window.__fakeSockets = [];
                window.VideoDecoder = class {};
                class FakeSocket {
                    constructor(url) {
                        this.url = url; this.readyState = 1; this.sent = [];
                        window.__fakeSockets.push(this);
                        // 模拟浏览器异步建连成功：onopen 在 start() 赋值
                        // 处理器之后触发。
                        setTimeout(() => { if (this.onopen) this.onopen(); }, 30);
                    }
                    send(data) {
                        this.sent.push(data);
                        if (data === 'ping') {
                            this.onmessage({data: JSON.stringify({type: 'pong'})});
                        }
                    }
                    close() { this.readyState = 3; }
                }
                FakeSocket.OPEN = 1;
                window.WebSocket = FakeSocket;
            }""")
            page.evaluate("aoaLiveScreen.start('REC', 'worker-a')")
            page.wait_for_timeout(400)
            assert page.evaluate("aoaLiveScreen.isActive()") is True

            # 模拟已出画但画面静止：设置解码帧数后长时间不再出新帧。
            page.evaluate("aoaLiveScreen.__state.framesDecoded = 5")
            page.wait_for_timeout(16000)  # ≥4 个看门狗 tick
            assert page.evaluate("aoaLiveScreen.isActive()") is True, \
                page.evaluate("() => document.getElementById('aoa-screen-status').textContent")
            assert page.evaluate("aoaLiveScreen.__state.lastPongAt") > 0

            # 模拟半开连接：连续无 pong → 心跳超时 → 先自动重连一次。
            page.evaluate("aoaLiveScreen.__state.lastPongAt = Date.now() - 20000")
            page.wait_for_timeout(5000)
            assert page.evaluate("aoaLiveScreen.isActive()") is True
            page.wait_for_timeout(3400)  # 重连延迟 + 新建连
            assert len(page.evaluate("window.__fakeSockets")) == 2

            # 重连后的连接再次心跳超时 → 重试预算用完 → 回退截图模式。
            page.evaluate("window.__fakeSockets[1].onopen && setTimeout(() => window.__fakeSockets[1].onopen(), 0)")
            page.wait_for_timeout(200)
            page.evaluate("aoaLiveScreen.__state.lastPongAt = Date.now() - 20000")
            page.wait_for_timeout(5000)
            page.wait_for_timeout(3400)
            assert page.evaluate("aoaLiveScreen.isActive()") is False
            # 心跳超时状态文本会被回退后的截图轮询立即覆盖，改用稳定的
            # 回退信号断言：按钮复位为"▶ 实时投屏"且截图轮询接管。
            button = page.evaluate(
                "() => ({live: document.getElementById('aoa-toggle-live').getAttribute('data-live'),"
                " text: document.getElementById('aoa-toggle-live').textContent})")
            assert button['live'] == 'off' and '▶ 实时投屏' in button['text'], button
        finally:
            page.close()

    def test_live_stream_survives_screen_off_without_frames(self):
        """设备熄屏（始终无帧）但心跳正常：流保持存活等待亮屏，
        不再被首帧 8 秒超时误杀。"""
        page = self.new_page()
        try:
            def route_handler(route):
                if route.request.url.endswith('/stream/REC/start'):
                    route.fulfill(content_type='application/json', body=json.dumps({
                        "success": True, "ws_url": "ws://fake/stream"}))
                    return
                route.fulfill(content_type='application/json', body=json.dumps({"success": True}))
            page.route('**/api/devices/aoa/**', route_handler)
            page.route('**/api/cluster/**', route_handler)
            self.goto_shell(page)
            page.evaluate("""async () => {
                switchPage('devices'); await aoaViewToggle(true);
                window.VideoDecoder = class {};
                class FakeSocket {
                    constructor(url) {
                        this.url = url; this.readyState = 1; this.sent = [];
                        setTimeout(() => { if (this.onopen) this.onopen(); }, 30);
                    }
                    send(data) {
                        if (data === 'ping') {
                            this.onmessage({data: JSON.stringify({type: 'pong'})});
                        }
                    }
                    close() { this.readyState = 3; }
                }
                FakeSocket.OPEN = 1;
                window.WebSocket = FakeSocket;
            }""")
            page.evaluate("aoaLiveScreen.start('REC', 'worker-a')")
            page.wait_for_timeout(400)
            assert page.evaluate("aoaLiveScreen.isActive()") is True
            # 模拟熄屏：连续 12 秒无任何帧（超过旧首帧 8 秒阈值），心跳正常。
            page.wait_for_timeout(12000)
            assert page.evaluate("aoaLiveScreen.isActive()") is True
            assert page.evaluate("aoaLiveScreen.__state.framesDecoded") == 0
            status = page.evaluate("() => document.getElementById('aoa-screen-status').textContent")
            assert '等待设备画面' in status, status
        finally:
            page.close()

    def test_live_stream_auto_reconnects_once_then_falls_back(self):
        """流断开（如 USB 重枚举）：3 秒后自动重连一次；重连后再失败才
        回退截图模式，且手动开启会复位重试预算。"""
        page = self.new_page()
        try:
            def route_handler(route):
                url = route.request.url
                if url.endswith('/stream/REC/start'):
                    route.fulfill(content_type='application/json', body=json.dumps({
                        "success": True, "ws_url": "ws://fake/stream"}))
                    return
                if url.endswith('/api/cluster/devices/actions'):
                    route.fulfill(content_type='application/json', body=json.dumps({
                        "accepted": True, "command_id": "cmd-1"}))
                    return
                if '/api/cluster/commands/cmd-1' in url:
                    route.fulfill(content_type='application/json', body=json.dumps({
                        "command": {"status": "completed",
                                    "result": {"image": "data:image/png;base64,xx"}}}))
                    return
                route.fulfill(content_type='application/json', body=json.dumps({"success": True}))
            page.route('**/api/devices/aoa/**', route_handler)
            page.route('**/api/cluster/**', route_handler)
            self.goto_shell(page)
            page.evaluate("""async () => {
                switchPage('devices'); await aoaViewToggle(true);
                window.VideoDecoder = class {};
                window.__fakeSockets = [];
                class FakeSocket {
                    constructor(url) {
                        this.url = url; this.readyState = 1; this.sent = [];
                        window.__fakeSockets.push(this);
                        setTimeout(() => { if (this.onopen) this.onopen(); }, 20);
                    }
                    send(data) {
                        if (data === 'ping') {
                            this.onmessage({data: JSON.stringify({type: 'pong'})});
                        }
                    }
                    close() { this.readyState = 3; }
                }
                FakeSocket.OPEN = 1;
                window.WebSocket = FakeSocket;
            }""")
            page.evaluate("aoaLiveScreen.start('REC', 'worker-a')")
            page.wait_for_timeout(200)
            assert page.evaluate("aoaLiveScreen.isActive()") is True

            # 第一次断开（模拟 USB 重枚举杀会话）→ 应自动重连。
            page.evaluate("window.__fakeSockets[0].onclose()")
            page.wait_for_timeout(100)
            status = page.evaluate("() => document.getElementById('aoa-screen-status').textContent")
            assert '自动重连' in status, status
            page.wait_for_timeout(3400)   # 3 秒重连延迟 + 建连
            assert page.evaluate("aoaLiveScreen.isActive()") is True
            assert len(page.evaluate("window.__fakeSockets")) == 2  # 第二条连接

            # 重连后再次断开 → 重试预算已用完 → 回退截图模式。
            page.evaluate("window.__fakeSockets[1].onclose()")
            page.wait_for_timeout(3400)
            assert page.evaluate("aoaLiveScreen.isActive()") is False
            button = page.evaluate(
                "() => ({live: document.getElementById('aoa-toggle-live').getAttribute('data-live'),"
                " text: document.getElementById('aoa-toggle-live').textContent})")
            assert button['live'] == 'off' and '▶ 实时投屏' in button['text'], button
        finally:
            page.close()

    def test_device_change_during_inflight_load_refreshes_again(self):
        """设备变化恰逢在途加载：去重命中后自动补刷一次，新设备立即可见，
        而不是等 10 秒周期。"""
        import time as _time
        page = self.new_page()
        state = {"devices": [], "loads": 0}
        try:
            def route_handler(route):
                if '/api/devices/management' in route.request.url:
                    state["loads"] += 1
                    _time.sleep(0.4)  # 制造在途窗口
                    route.fulfill(content_type='application/json', body=json.dumps({
                        'success': True, 'devices': state["devices"]}))
                    return
                route.fulfill(content_type='application/json', body=json.dumps({
                    'success': True, 'devices': []}))
            page.route('**/api/devices/management*', route_handler)
            page.route('**/api/cluster/devices*', route_handler)
            page.route('**/api/devices/aoa/**', route_handler)
            self.goto_shell(page)
            page.evaluate("switchPage('devices')")
            page.wait_for_timeout(800)
            baseline = state["loads"]

            # 启动一次加载（在途）；随后设备变化 + 事件刷新（去重命中）。
            page.evaluate("loadDevicesManagement()")
            page.wait_for_timeout(100)
            state["devices"] = [{'serial_no': 'DEV-B', 'worker_id': 'worker-a',
                                 'transport': 'local_usb', 'state': 'available',
                                 'status': 'online'}]
            page.evaluate("scheduleDevicesManagementRefresh()")
            page.wait_for_timeout(1500)

            options = page.evaluate(
                "() => [...document.getElementById('aoa-record-device').options].map(o => o.value)")
            assert 'DEV-B' in options, (options, state["loads"], baseline)
            # 至少发生了一次自动补刷（在途加载 + 变化后补刷）。
            assert state["loads"] >= baseline + 2, state["loads"]
        finally:
            page.close()

    def test_visibility_return_triggers_catchup_refresh(self):
        """后台标签页丢掉设备事件后，回到前台应自动补一次刷新。"""
        page = self.new_page()
        state = {"devices": [], "loads": 0}
        try:
            def route_handler(route):
                if '/api/devices/management' in route.request.url:
                    state["loads"] += 1
                    route.fulfill(content_type='application/json', body=json.dumps({
                        'success': True, 'devices': state["devices"]}))
                    return
                route.fulfill(content_type='application/json', body=json.dumps({
                    'success': True, 'devices': []}))
            page.route('**/api/devices/management*', route_handler)
            page.route('**/api/cluster/devices*', route_handler)
            page.route('**/api/devices/aoa/**', route_handler)
            self.goto_shell(page)
            page.evaluate("switchPage('devices')")
            page.wait_for_timeout(800)
            baseline = state["loads"]

            # 模拟后台：hidden=true 时事件刷新被丢弃。
            page.evaluate("""() => {
                Object.defineProperty(document, 'hidden', {get: () => true, configurable: true});
                Object.defineProperty(document, 'visibilityState',
                                      {get: () => 'hidden', configurable: true});
                scheduleDevicesManagementRefresh();
            }""")
            page.wait_for_timeout(600)
            dropped = state["loads"]

            # 模拟回到前台：visibilitychange 应触发补刷。
            state["devices"] = [{'serial_no': 'DEV-C', 'worker_id': 'worker-a',
                                 'transport': 'local_usb', 'state': 'available',
                                 'status': 'online'}]
            page.evaluate("""() => {
                Object.defineProperty(document, 'hidden', {get: () => false, configurable: true});
                Object.defineProperty(document, 'visibilityState',
                                      {get: () => 'visible', configurable: true});
                document.dispatchEvent(new Event('visibilitychange'));
            }""")
            page.wait_for_timeout(900)
            options = page.evaluate(
                "() => [...document.getElementById('aoa-record-device').options].map(o => o.value)")
            assert 'DEV-C' in options, (options, state["loads"], baseline, dropped)
            assert state["loads"] > dropped, (state["loads"], dropped)
        finally:
            page.close()

    def test_usb_replug_refreshes_recorder_and_replay_selects(self):
        """插拔 USB 线：WS devices_changed 触发管理清单刷新后，录制设备与
        目标设备下拉应自动跟随（拔线保留"暂未枚举"标注，插线后新设备出现）。
        独立 setup：不得 mock loadDevicesManagement，否则刷新链路被短路。"""
        page = self.new_page()
        state = {"devices": []}
        try:
            def route_handler(route):
                url = route.request.url
                if '/api/devices/management' in url:
                    route.fulfill(content_type='application/json', body=json.dumps({
                        'success': True, 'devices': state["devices"]}))
                    return
                route.fulfill(content_type='application/json', body=json.dumps({
                    'success': True, 'devices': []}))
            page.route('**/api/devices/management*', route_handler)
            page.route('**/api/cluster/devices*', route_handler)
            page.route('**/api/devices/aoa/**', route_handler)
            page.add_init_script(
                "try { localStorage.removeItem('aoa-screen-grid'); } catch (e) {}")
            self.goto_shell(page)
            page.evaluate("""async () => {
                switchPage('devices');
                await aoaViewToggle(true);
                window.aoaLiveScreen = {start: async () => {}, stop: () => {}, isActive: () => true};
            }""")

            def set_devices(devs):
                state["devices"] = devs
                page.evaluate("scheduleDevicesManagementRefresh()")
                page.wait_for_timeout(900)

            # 初始：设备 A 在线可用。
            set_devices([{'serial_no': 'DEV-A', 'worker_id': 'worker-a',
                          'transport': 'local_usb', 'state': 'available',
                          'status': 'online'}])
            options = page.evaluate(
                "() => [...document.getElementById('aoa-record-device').options].map(o => o.value)")
            assert 'DEV-A' in options, options
            replay = page.evaluate(
                "() => [...document.getElementById('aoa-replay-device').options].map(o => o.value)")
            assert 'DEV-A' in replay, replay

            # 拔线：设备从清单消失，keepMissing 保留"暂未枚举"标注。
            set_devices([])
            label = page.evaluate(
                "() => document.getElementById('aoa-record-device').selectedOptions[0]?.textContent")
            assert '暂未枚举' in (label or ''), label

            # 插线：设备重新枚举并 ADB 就绪，下拉自动恢复且无需手动刷新。
            set_devices([{'serial_no': 'DEV-A', 'worker_id': 'worker-a',
                          'transport': 'local_usb', 'state': 'available',
                          'status': 'online'}])
            label = page.evaluate(
                "() => document.getElementById('aoa-record-device').selectedOptions[0]?.textContent")
            assert 'DEV-A' in (label or '') and '暂未枚举' not in (label or ''), label
            replay = page.evaluate(
                "() => [...document.getElementById('aoa-replay-device').options].map(o => o.value)")
            assert 'DEV-A' in replay, replay

            # 新插一台设备 B（ADB 就绪）：两个下拉都应自动出现 B。
            set_devices([{'serial_no': 'DEV-A', 'worker_id': 'worker-a',
                          'transport': 'local_usb', 'state': 'available',
                          'status': 'online'},
                         {'serial_no': 'DEV-B', 'worker_id': 'worker-a',
                          'transport': 'local_usb', 'state': 'available',
                          'status': 'online'}])
            record = page.evaluate(
                "() => [...document.getElementById('aoa-record-device').options].map(o => o.value)")
            replay = page.evaluate(
                "() => [...document.getElementById('aoa-replay-device').options].map(o => o.value)")
            assert 'DEV-B' in record and 'DEV-B' in replay, (record, replay)
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
