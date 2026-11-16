"""Exercise proxied noVNC keyboard/focus behavior without connecting to a host."""

import json
import unittest
from pathlib import Path
from urllib.parse import unquote, urlsplit

from features.system.desktop import novnc_asset_override
from tests.runtime_ui.harness import RuntimeUiHarness, expect


_DESKTOP_FIXTURE = """<!doctype html><html><body>
<canvas id="desktop" tabindex="0" width="640" height="320"></canvas>
<script type="module">
import RFB from './core/rfb.js';
import Keyboard from './core/input/keyboard.js';
const canvas = document.getElementById('desktop');
// 直接用已安装客户端的真实方法，不建立连接。
const rfb = Object.create(RFB.prototype);
rfb._canvas = canvas;
rfb.focusOnClick = true;
rfb._mouseButtonMask = 0;
rfb._mouseMoveTimer = null;
rfb._mouseLastMoveTime = 0;
const keyboard = new Keyboard(canvas);
window.keyEvents = [];
keyboard.onkeyevent = (keysym, code, down) => window.keyEvents.push({keysym, code, down});
keyboard.grab();
// 挂 noVNC 真实焦点/鼠标处理器：_handleMouse 会取消浏览器 mousedown
// 默认聚焦，焦点须由 RFB 显式获取。
canvas.addEventListener('mousedown', rfb._focusCanvas.bind(rfb));
canvas.addEventListener('touchstart', rfb._focusCanvas.bind(rfb));
window.mouseEvents = [];
if (rfb._handleMouse) {
    for (const type of ['mousedown', 'mouseup', 'mousemove', 'click']) {
        canvas.addEventListener(type, event => {
            rfb._handleMouse(event);
            window.mouseEvents.push({type, defaultPrevented: event.defaultPrevented});
        });
    }
} else {
    const {default: Mouse} = await import('./core/input/mouse.js');
    const mouse = new Mouse(canvas);
    mouse.grab();
    canvas.addEventListener('mousedown', event => {
        window.mouseEvents.push({type: event.type, defaultPrevented: event.defaultPrevented});
    });
}
window.rfb = rfb;
window.focusAttempts = 0;
window.delayedFocus = () => { rfb.focus(); window.focusAttempts++; };
// 连接完成与控制条自动隐藏回调都会调用 focus。
setTimeout(window.delayedFocus, 120);
setTimeout(window.delayedFocus, 220);
window.desktopReady = true;
</script></body></html>"""


class RuntimeDesktopKeyboardTests(RuntimeUiHarness):
    @classmethod
    def setUpClass(cls):
        cls.novnc_root = next((path for path in (Path('/opt/noVNC'), Path('/usr/share/novnc'))
                               if (path / 'core/rfb.js').is_file()), None)
        if cls.novnc_root is None:
            raise unittest.SkipTest('Install noVNC to exercise its real browser modules')
        super().setUpClass()

    def route_desktop_fixture(self, page):
        page.route('**/api/cluster/status', lambda route: route.fulfill(
            json={'success': True, 'enabled': True, 'local_worker_id': 'ats-worker-controller', 'workers': []},
        ))
        page.route('**/api/users/workspace-context', lambda route: route.fulfill(
            json={'success': True, 'data': {'context': {
                'scope_mode': 'cluster', 'worker_id': 'ats-worker-controller', 'device_ids': [],
            }}},
        ))
        page.route('**/api/desktop/vnc/status', lambda route: route.fulfill(
            json={'success': True, 'running': True},
        ))
        page.route('**/api/desktop/novnc/access', lambda route: route.fulfill(
            json={'success': True, 'url': '/novnc/keyboard-fixture.html'},
        ))

        def serve_asset(route):
            path = unquote(urlsplit(route.request.url).path).removeprefix('/novnc/')
            if path == 'keyboard-fixture.html':
                route.fulfill(content_type='text/html', body=_DESKTOP_FIXTURE)
                return
            asset = (self.novnc_root / path).resolve()
            if not asset.is_relative_to(self.novnc_root.resolve()) or not asset.is_file():
                route.fulfill(status=404, body='Fixture asset not found')
                return
            route.fulfill(content_type='application/javascript', body=novnc_asset_override(path, asset.read_bytes()))

        page.route(f'{self.base_url}/novnc/**', serve_asset)

    def enter_desktop(self, page):
        self.route_desktop_fixture(page)
        self.goto_shell(page)
        page.evaluate("""async () => {
            await GmsWorkspace.ready;
            await initializeClusterMode();
            state.elevated = true;
            state.elevatedUntil = Date.now() + 60000;
            applyClusterMode(true);
            const pages = Array.from(document.querySelectorAll('.sidebar-item'))
                .filter(item => item.style.display !== 'none').map(item => item.dataset.page);
            const previous = pages[(pages.indexOf('desktop') + pages.length - 1) % pages.length];
            switchPage(previous, null);
        }""")
        page.locator('body').focus()
        page.keyboard.press('ArrowDown')
        page.wait_for_function("currentPage === 'desktop'")
        return self.connected_frame(page)

    def connected_frame(self, page):
        expect(page.locator('#host-workspace-grid iframe')).to_have_count(1)
        frame = self.frame_for(page, '#host-workspace-grid iframe')
        frame.wait_for_function('window.desktopReady && window.focusAttempts === 2')
        return frame

    def test_delayed_vnc_focus_preserves_repeated_web_arrow_navigation(self):
        page = self.new_page()
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        try:
            self.enter_desktop(page)
            self.assertEqual(page.evaluate('document.activeElement.id'), 'page-desktop')
            for layout in ('horizontal', 'vertical', 'quad', 'single'):
                page.keyboard.press('ArrowRight')
                expect(page.locator(f'[data-workspace-layout="{layout}"]')).to_have_class('btn-xs active')
                self.connected_frame(page)
                self.assertFalse(page.evaluate('document.activeElement.tagName === "IFRAME"'))
            page.keyboard.press('ArrowLeft')
            expect(page.locator('[data-workspace-layout="quad"]')).to_have_class('btn-xs active')
            desktop_frame = self.connected_frame(page)
            next_page = page.evaluate("""() => {
                const pages = Array.from(document.querySelectorAll('.sidebar-item'))
                    .filter(item => item.style.display !== 'none').map(item => item.dataset.page);
                return pages[(pages.indexOf('desktop') + 1) % pages.length];
            }""")
            page.keyboard.press('ArrowDown')
            page.wait_for_function(f'currentPage === {json.dumps(next_page)}')
            desktop_frame.evaluate('window.delayedFocus()')
            self.assertFalse(page.evaluate('document.activeElement.matches("#host-workspace-grid iframe")'))
            self.assert_no_page_errors(errors)
        finally:
            page.close()

    def test_clicked_desktop_keeps_arrows_and_accepts_both_enter_keys(self):
        page = self.new_page()
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        try:
            frame = self.enter_desktop(page)
            frame.locator('#desktop').click()
            expect(frame.locator('#desktop')).to_be_focused()
            self.assertTrue(frame.evaluate("window.mouseEvents.some(event => event.type === 'mousedown' && event.defaultPrevented)"))
            for key in ('ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight', 'Enter', 'NumpadEnter'):
                page.keyboard.press(key)
            self.assertEqual(page.evaluate('currentPage'), 'desktop')
            events = frame.evaluate('window.keyEvents')
            arrow_events = [event for event in events if event['code'].startswith('Arrow')]
            self.assertEqual(len(arrow_events), 8)
            enters = [event for event in events if event['code'] in ('Enter', 'NumpadEnter')]
            self.assertEqual([event['keysym'] for event in enters], [0xff0d] * 4)
            self.assertEqual([event['down'] for event in enters], [True, False, True, False])
            # 旧桌面延迟焦点回调再次触发时，焦点仍须留在 Web 控件上。
            shell_control = page.locator('[data-workspace-layout="single"]')
            for _ in range(3):
                shell_control.focus()
                frame.evaluate('window.delayedFocus()')
                expect(shell_control).to_be_focused()
                frame.locator('#desktop').click()
                expect(frame.locator('#desktop')).to_be_focused()
                page.keyboard.type('echo')
            typed = [event['code'] for event in frame.evaluate('window.keyEvents')
                     if event['down'] and event['code'].startswith('Key')]
            self.assertEqual(typed, ['KeyE', 'KeyC', 'KeyH', 'KeyO'] * 3)
            shell_control.focus()
            frame.evaluate('window.delayedFocus()')
            expect(shell_control).to_be_focused()
            page.keyboard.press('ArrowRight')
            expect(page.locator('[data-workspace-layout="horizontal"]')).to_have_class('btn-xs active')
            self.assert_no_page_errors(errors)
        finally:
            page.close()

    def test_focus_on_click_can_still_be_disabled(self):
        page = self.new_page()
        try:
            frame = self.enter_desktop(page)
            frame.evaluate('window.rfb.focusOnClick = false')
            frame.locator('#desktop').click()
            expect(frame.locator('#desktop')).not_to_be_focused()
            frame.evaluate('window.rfb.focusOnClick = true')
            frame.locator('#desktop').click()
            expect(frame.locator('#desktop')).to_be_focused()
        finally:
            page.close()

    def test_standalone_novnc_keeps_automatic_focus(self):
        page = self.new_page()
        try:
            self.route_desktop_fixture(page)
            page.goto(f'{self.base_url}/novnc/keyboard-fixture.html')
            page.wait_for_function('window.desktopReady && window.focusAttempts === 2')
            expect(page.locator('#desktop')).to_be_focused()
        finally:
            page.close()
