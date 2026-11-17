"""Authentication view geometry and existing form interactions."""
from __future__ import annotations

import os
from pathlib import Path

from tests.runtime_ui.harness import RuntimeUiHarness, expect
from tests.runtime_ui.test_modal_redesign import LAYOUT


class AuthDialogTests(RuntimeUiHarness):
    def login_page(self):
        page = self.new_page()
        page.route('**/api/auth/status', lambda route: route.fulfill(json={
            'auth_required': True, 'authenticated': False, 'setup_required': False,
        }))
        page.route('**/api/users/current', lambda route: route.fulfill(json={
            'ip': '192.0.2.66', 'username': 'render-user',
            'display_client_id': 'render-user@192.0.2.66',
        }))
        page.route('**/api/auth/client-ssh-status', lambda route: route.fulfill(json={
            'client_ip': '192.0.2.66', 'ssh_reachable': True,
        }))
        page.goto(self.base_url, wait_until='domcontentloaded')
        expect(page.locator('#auth-gate')).to_be_visible()
        expect(page.locator('#auth-username')).to_have_value('render-user@192.0.2.66')
        return page

    def check_panel(self, page, selector):
        page.wait_for_function("""selector => {
            const frame = document.querySelector(selector)?.querySelector('.tech-frame');
            const box = frame?.getBoundingClientRect();
            return frame?.querySelector('.tech-frame-accent')
                && Math.abs(frame.viewBox.baseVal.width - box.width) < 1
                && Math.abs(frame.viewBox.baseVal.height - box.height) < 1;
        }""", arg=selector)
        panel = page.locator(selector)
        report = panel.evaluate(LAYOUT)
        bounds = report['bounds']
        self.assertGreaterEqual(bounds['left'], 0, report)
        self.assertGreaterEqual(bounds['top'], 0, report)
        self.assertLessEqual(bounds['right'], page.viewport_size['width'], report)
        self.assertLessEqual(bounds['bottom'], page.viewport_size['height'], report)
        self.assertTrue(report['alignedCorners'], report)
        self.assertFalse(report['ticksOverlapClose'], report)
        self.assertEqual(report['leaks'], [], report)
        self.assertEqual(report['clipped'], [], report)
        for footer in report['footer']:
            self.assertLessEqual(footer['bottom'], bounds['bottom'], report)
        expect(panel).to_have_css('background-color', 'rgb(32, 32, 39)')

    def test_login_setup_and_ssh_guide_fit_their_content(self):
        page = self.login_page()
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        output = Path(os.environ['GMS_AUTH_GALLERY_OUT']) if os.getenv('GMS_AUTH_GALLERY_OUT') else None
        images = []
        if output:
            output.mkdir(parents=True, exist_ok=True)

        def capture(name, selector):
            self.check_panel(page, selector)
            if output:
                filename = f'{name}-{page.viewport_size["width"]}.png'
                page.screenshot(path=str(output / filename), animations='disabled')
                images.append(filename)

        try:
            for viewport in [
                {'width': 1440, 'height': 960}, {'width': 768, 'height': 720},
                {'width': 390, 'height': 844}, {'width': 844, 'height': 390},
            ]:
                page.set_viewport_size(viewport)
                with self.subTest(mode='login', viewport=viewport):
                    page.evaluate("state.authGateDismissed = false; closeAuthSshdGuide(); showAuthGate(false)")
                    capture('login', 'form.auth-panel')
                    if viewport['width'] >= 768 and viewport['height'] >= 720:
                        self.assertLess(page.locator('form.auth-panel').bounding_box()['height'], 420)
                with self.subTest(mode='setup', viewport=viewport):
                    page.evaluate('state.authBootstrapTokenRequired = true; showAuthGate(true)')
                    expect(page.locator('#auth-display-name-row')).to_be_visible()
                    expect(page.locator('#auth-bootstrap-token')).to_have_attribute('required', '')
                    capture('setup', 'form.auth-panel')
                    page.locator('#auth-bootstrap-token-help details').evaluate('el => el.open = true')
                    capture('setup-expanded', 'form.auth-panel')
                    page.locator('#auth-bootstrap-token-help details').evaluate('el => el.open = false')
                with self.subTest(mode='guide', viewport=viewport):
                    page.evaluate("showAuthGate(false); showAuthSshdGuide('渲染测试：客户端尚未开启 SSH 服务', '安装并启动 OpenSSH Server。\\n' + '完成后返回登录并重试。\\n'.repeat(30))")
                    expect(page.locator('form.auth-panel')).to_be_hidden()
                    capture('ssh-guide', '#auth-sshd-guide')
                    page.locator('#auth-sshd-guide .auth-actions button').click()
                    expect(page.locator('form.auth-panel')).to_be_visible()
                    expect(page.locator('#auth-password')).to_be_focused()
            self.assert_no_page_errors(errors)
        finally:
            if output:
                cards = ''.join(f'<figure><figcaption>{name}</figcaption><a href="{name}"><img loading="lazy" src="{name}"></a></figure>' for name in images)
                (output / 'index.html').write_text('<!doctype html><meta charset="utf-8"><title>登录窗口实际截图</title><style>body{background:#15151b;color:#ededf2;font:14px sans-serif;padding:20px}main{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:16px}figure{margin:0;padding:10px;border:1px solid #4a4a58}img{width:100%}</style><h1>登录窗口 · 隔离环境渲染测试数据</h1><main>' + cards + '</main>')
            page.close()

    def test_login_retains_prefill_password_toggle_enter_and_close(self):
        page = self.login_page()
        submitted = []

        def reject_login(route):
            submitted.append(route.request.post_data_json)
            route.fulfill(status=401, json={'success': False, 'error': '渲染测试：账号或密码错误'})

        page.route('**/api/auth/login', reject_login)
        try:
            page.locator('#auth-password').fill('isolated-render-fixture')
            toggle = page.locator('#auth-gate .password-visibility-toggle')
            toggle.click()
            expect(page.locator('#auth-password')).to_have_attribute('type', 'text')
            toggle.click()
            expect(page.locator('#auth-password')).to_have_attribute('type', 'password')
            page.locator('#auth-password').press('Enter')
            expect(page.locator('#auth-message')).to_contain_text('账号或密码错误')
            self.assertEqual(len(submitted), 1)
            self.assertEqual(submitted[0]['username'], 'render-user@192.0.2.66')
            expect(page.locator('#auth-submit')).to_be_enabled()
            page.locator('#auth-close').click()
            expect(page.locator('#auth-gate')).to_be_hidden()
            page.evaluate('state.authGateDismissed = false; showAuthGate(false)')
            expect(page.locator('#auth-gate')).to_be_visible()
            page.keyboard.press('Escape')
            expect(page.locator('#auth-gate')).to_be_hidden()
            self.assertEqual(len(submitted), 1)
        finally:
            page.close()
