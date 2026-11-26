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
        self.assertTrue(report['cutSurface'], report)
        self.assertTrue(report['headerSingleOwner'], report)
        expect(panel).to_have_css('background-color', 'rgb(28, 25, 41)')

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
                    expect(page.locator('#auth-username-help')).to_be_visible()
                    expect(page.locator('#auth-password-help')).to_be_visible()
                    expect(page.locator('#auth-bootstrap-token-help')).to_be_hidden()
                    capture('login', 'form.auth-panel')
                    if viewport['width'] >= 768 and viewport['height'] >= 720:
                        login_box = page.locator('form.auth-panel').bounding_box()
                        self.assertAlmostEqual(login_box['width'], 540, delta=1)
                        self.assertLess(login_box['height'], 420)
                with self.subTest(mode='setup', viewport=viewport):
                    page.evaluate('state.authBootstrapTokenRequired = true; showAuthGate(true)')
                    expect(page.locator('#auth-display-name-row')).to_have_count(0)
                    expect(page.locator('#auth-bootstrap-token')).to_have_attribute('required', '')
                    expect(page.locator('#auth-username-help')).to_be_hidden()
                    expect(page.locator('#auth-password-help')).to_be_hidden()
                    expect(page.locator('#auth-message')).to_be_hidden()
                    expect(page.locator('#auth-bootstrap-token-help details summary')).to_be_visible()
                    expect(page.locator('#auth-bootstrap-token-help details code').first).to_be_hidden()
                    capture('setup', 'form.auth-panel')
                    if viewport['width'] >= 768 and viewport['height'] >= 720:
                        setup_box = page.locator('form.auth-panel').bounding_box()
                        self.assertAlmostEqual(setup_box['width'], 440, delta=1)
                        self.assertLess(setup_box['height'], 460)
                    fields = [
                        page.locator(selector).bounding_box()
                        for selector in ('#auth-username', '#auth-password', '#auth-bootstrap-token')
                    ]
                    for field in fields:
                        self.assertAlmostEqual(field['x'], fields[0]['x'], delta=1)
                        self.assertAlmostEqual(field['width'], fields[0]['width'], delta=1)
                        self.assertAlmostEqual(field['height'], fields[0]['height'], delta=1)
                    page.locator('#auth-bootstrap-token-help details').evaluate('el => el.open = true')
                    command_row = page.locator('.auth-bootstrap-command')
                    copy_button = command_row.get_by_role('button', name='复制获取初始化令牌命令')
                    expect(copy_button).to_be_visible()
                    command = command_row.locator('.auth-bootstrap-command-text')
                    expect(command).to_contain_text('python3 -c')
                    expect(command).to_have_css('white-space', 'nowrap')
                    expect(command).to_have_css('text-overflow', 'ellipsis')
                    self.assertTrue(command.evaluate('el => el.scrollWidth > el.clientWidth'))
                    row = command_row.bounding_box()
                    button = copy_button.bounding_box()
                    field = command_row.locator('.auth-bootstrap-command-field').bounding_box()
                    self.assertAlmostEqual(button['x'] + button['width'], row['x'] + row['width'], delta=1)
                    self.assertAlmostEqual(field['y'] + field['height'] / 2, button['y'] + button['height'] / 2, delta=1)
                    self.assertLessEqual(field['x'] + field['width'], button['x'])
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

    def test_bootstrap_command_copy_preserves_full_command_without_submitting(self):
        page = self.login_page()
        submitted = []
        page.route('**/api/auth/setup', lambda route: (
            submitted.append(route.request.post_data_json),
            route.fulfill(status=403, json={'success': False, 'error': 'unexpected submit'}),
        ))
        try:
            page.evaluate("""() => {
                state.authBootstrapTokenRequired = true;
                showAuthGate(true);
                window.copyTextToClipboard = async value => { window.__copiedBootstrapCommand = value; };
            }""")
            page.locator('#auth-bootstrap-token-help summary').click()
            button = page.get_by_role('button', name='复制获取初始化令牌命令')
            button.click()
            page.wait_for_function("typeof window.__copiedBootstrapCommand === 'string'")
            self.assertEqual(
                page.evaluate('window.__copiedBootstrapCommand'),
                """python3 -c 'import json; print(json.load(open("configs/secrets/environment.json"))["GMS_BOOTSTRAP_TOKEN"])'""",
            )
            gate = page.locator('#auth-gate')
            expect(gate).to_be_visible()
            toast = page.locator('#toast')
            expect(toast).to_be_visible()
            expect(toast).to_have_text('获取初始化令牌命令已复制')
            expect(toast).to_have_class('toast success show')
            self.assertGreater(
                int(toast.evaluate('el => getComputedStyle(el).zIndex')),
                int(gate.evaluate('el => getComputedStyle(el).zIndex')),
            )
            page.evaluate("() => { window.copyTextToClipboard = async () => { throw new Error('clipboard denied'); }; }")
            button.click()
            expect(toast).to_have_class('toast error show')
            expect(toast).to_have_text('复制失败，请检查浏览器剪贴板权限后重试')
            expect(toast).to_be_visible()
            self.assertEqual(submitted, [])
            page.evaluate('state.authBootstrapTokenRequired = false; showAuthGate(true)')
            expect(page.locator('#auth-bootstrap-token-row')).to_be_hidden()
            expect(page.locator('#auth-bootstrap-token-help')).to_be_hidden()
        finally:
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

    def test_password_visibility_is_contained_in_input_in_login_setup_and_elevation(self):
        page = self.login_page()
        try:
            for viewport in [
                {'width': 1440, 'height': 960}, {'width': 390, 'height': 844},
            ]:
                page.set_viewport_size(viewport)
                for mode in ('login', 'setup', 'elevation'):
                    with self.subTest(mode=mode, viewport=viewport):
                        page.evaluate("ModalManager.closeAll(); state.authGateDismissed = false")
                        if mode == 'elevation':
                            page.locator('#auth-close').click()
                            page.evaluate("ModalManager.open('elevate-modal')")
                            selector = '#elevate-password'
                        else:
                            page.evaluate("state.authBootstrapTokenRequired = true; showAuthGate(" + (
                                'true' if mode == 'setup' else 'false') + ")")
                            selector = '#auth-password'
                        field = page.locator(selector)
                        wrapper = field.locator('..')
                        toggle = wrapper.locator('button.password-visibility-toggle')
                        expect(toggle).to_have_count(1)
                        field.fill('isolated-render-fixture-long-password')
                        for kind in ('password', 'text'):
                            expect(field).to_have_attribute('type', kind)
                            geometry = wrapper.evaluate("""el => {
                                const input = el.querySelector('input');
                                const button = el.querySelector('.password-visibility-toggle');
                                const a = input.getBoundingClientRect();
                                const b = button.getBoundingClientRect();
                                const css = getComputedStyle(input);
                                return {
                                    inside: b.left >= a.left + 1 && b.right <= a.right - 1
                                        && b.top >= a.top + 1 && b.bottom <= a.bottom - 1,
                                    centered: Math.abs((a.top + a.bottom - b.top - b.bottom) / 2) <= 1,
                                    reserved: parseFloat(css.paddingRight) >= a.right - b.left + 2,
                                    background: getComputedStyle(button).backgroundColor,
                                    border: getComputedStyle(button).borderRightWidth,
                                };
                            }""")
                            self.assertTrue(geometry['inside'], geometry)
                            self.assertTrue(geometry['centered'], geometry)
                            self.assertTrue(geometry['reserved'], geometry)
                            self.assertEqual(geometry['background'], 'rgba(0, 0, 0, 0)', geometry)
                            self.assertEqual(geometry['border'], '0px', geometry)
                            toggle.click()
                        expect(field).to_have_attribute('type', 'password')
                        expect(field).to_have_value('isolated-render-fixture-long-password')
                        field.focus()
                        page.keyboard.press('Tab')
                        expect(toggle).to_be_focused()
                        expect(toggle).to_have_css('outline-offset', '-2px')
                        page.keyboard.press('Space')
                        expect(field).to_have_attribute('type', 'text')
                        page.keyboard.press('Space')
                        expect(field).to_have_attribute('type', 'password')
        finally:
            page.close()

    def assert_shared_password_control(self, page, selector):
        field = page.locator(selector)
        toggle = field.locator('..').locator('button.password-visibility-toggle')
        expect(field).to_be_visible()
        expect(field).to_have_attribute('type', 'password')
        expect(toggle).to_have_count(1)
        expect(toggle).to_have_attribute('aria-controls', selector[1:])
        field.fill('isolated-render-fixture-long-password')
        for kind in ('password', 'text'):
            expect(field).to_have_attribute('type', kind)
            geometry = field.locator('..').evaluate("""el => {
                const input = el.querySelector('input');
                const button = el.querySelector('button');
                const a = input.getBoundingClientRect();
                const b = button.getBoundingClientRect();
                const css = getComputedStyle(button);
                return {
                    inside: b.left >= a.left + 1 && b.right <= a.right - 1
                        && b.top >= a.top + 1 && b.bottom <= a.bottom - 1,
                    centered: Math.abs((a.top + a.bottom - b.top - b.bottom) / 2) <= 1,
                    reserved: parseFloat(getComputedStyle(input).paddingRight)
                        >= a.right - b.left + 2,
                    transparent: css.backgroundColor === 'rgba(0, 0, 0, 0)',
                    borderless: css.borderRightWidth === '0px',
                };
            }""")
            self.assertTrue(all(geometry.values()), geometry)
            toggle.click()
        expect(field).to_have_attribute('type', 'password')
        expect(field).to_have_value('isolated-render-fixture-long-password')
        field.focus()
        page.keyboard.press('Tab')
        expect(toggle).to_be_focused()
        page.keyboard.press('Space')
        expect(field).to_have_attribute('type', 'text')
        expect(toggle).to_have_attribute('aria-label', '隐藏密码')
        # Leave the revealed field active so the caller can verify dismissal.
        return field

    def test_shell_password_dialogs_share_geometry_and_conceal_on_close(self):
        page = self.new_page()
        try:
            self.goto_shell(page)
            for viewport in ({'width': 1440, 'height': 960}, {'width': 390, 'height': 844}):
                page.set_viewport_size(viewport)
                for modal, selector in (
                    ('device-password-modal', '#device-pswd'),
                    ('firmware-share-password-modal', '#firmware-share-password-input'),
                    ('username-detect-modal', '#username-detect-password'),
                    ('adb-proxy-modal', '#adb-proxy-ubuntu-password'),
                    ('agent-sdk-add-modal', '#agent-sdk-add-password'),
                    ('agent-sdk-edit-modal', '#agent-sdk-edit-password'),
                ):
                    with self.subTest(modal=modal, viewport=viewport):
                        page.evaluate("id => ModalManager.open(id)", modal)
                        if modal == 'adb-proxy-modal':
                            page.evaluate("document.getElementById('adb-proxy-ubuntu-source-panel').hidden = false")
                        elif modal == 'agent-sdk-add-modal':
                            page.evaluate("document.getElementById('agent-sdk-add-type').value = 'ssh_git'; sdkConfigOnTypeChange()")
                        elif modal == 'agent-sdk-edit-modal':
                            page.evaluate("document.querySelectorAll('.agent-sdk-edit-ssh-only').forEach(el => el.hidden = false)")
                        page.evaluate("new Promise(resolve => requestAnimationFrame(() => resolve()))")
                        field = self.assert_shared_password_control(page, selector)
                        page.evaluate("id => ModalManager.close(id)", modal)
                        expect(field).to_have_attribute('type', 'password')
        finally:
            page.close()

    def test_embedded_password_dialogs_share_geometry_and_conceal_on_close(self):
        page = self.new_page()
        try:
            for viewport in ({'width': 1440, 'height': 960}, {'width': 390, 'height': 844}):
                page.set_viewport_size(viewport)
                for path, selector in (
                    ('/gerrit-dashboard', '#settingRestPass'),
                    ('/redmine-agent?settings=1', '#settingRedminePass'),
                    ('/cluster', '#worker-password'),
                    ('/automation', '#build-password-input'),
                ):
                    with self.subTest(path=path, viewport=viewport):
                        page.goto(self.base_url + path, wait_until='domcontentloaded')
                        page.wait_for_function("typeof togglePasswordVisibility === 'function'")
                        if path == '/cluster':
                            page.wait_for_function("typeof syncClusterModalState === 'function'")
                            page.evaluate("document.getElementById('onboarding').hidden = false; syncClusterModalState()")
                        elif path == '/automation':
                            page.wait_for_function("typeof promptBuildPassword === 'function'")
                            page.evaluate("void promptBuildPassword()")
                        elif path != '/redmine-agent?settings=1':
                            page.wait_for_function("typeof showModal === 'function'")
                            page.evaluate("showModal('settingsModal')")
                        page.evaluate("new Promise(resolve => requestAnimationFrame(() => resolve()))")
                        field = self.assert_shared_password_control(page, selector)
                        if path == '/automation':
                            page.keyboard.press('Escape')
                            expect(page.locator('.password-backdrop')).to_have_count(0)
                            page.evaluate("void promptBuildPassword()")
                            expect(page.locator(selector)).to_have_attribute('type', 'password')
                            expect(page.locator(selector)).to_have_value('')
                            page.keyboard.press('Escape')
                        elif path == '/cluster':
                            page.keyboard.press('Escape')
                            expect(page.locator('#onboarding')).to_be_hidden()
                            expect(field).to_have_attribute('type', 'password')
                        elif path == '/redmine-agent?settings=1':
                            page.reload(wait_until='domcontentloaded')
                            expect(page.locator(selector)).to_have_attribute('type', 'password')
                        else:
                            page.evaluate("hideModal('settingsModal')")
                            expect(field).to_have_attribute('type', 'password')
        finally:
            page.close()


    def test_compact_setup_still_displays_authentication_errors(self):
        page = self.login_page()
        try:
            page.evaluate("state.authBootstrapTokenRequired = true; showAuthGate(true)")
            submitted = []

            def reject_setup(route):
                submitted.append(route.request.post_data_json)
                route.fulfill(status=403, json={'success': False, 'error': '初始化令牌无效'})

            page.route('**/api/auth/setup', reject_setup)
            page.locator('#auth-username').fill('custom-admin')
            page.locator('#auth-password').fill('isolated-render-fixture')
            page.locator('#auth-bootstrap-token').fill('invalid-fixture-token')
            page.locator('#auth-submit').click()
            expect(page.locator('#auth-message')).to_be_visible()
            expect(page.locator('#auth-message')).to_have_text('初始化令牌无效')
            expect(page.locator('#auth-submit')).to_be_enabled()
            self.assertEqual(len(submitted), 1)
            self.assertEqual(submitted[0]['username'], 'custom-admin')
            self.assertEqual(submitted[0]['display_name'], 'custom-admin')
        finally:
            page.close()

    def test_switch_target_help_survives_gate_refresh(self):
        page = self.login_page()
        try:
            for target, username_help, password_help in (
                ('admin', '此处不使用客户端 SSH 账号', '请输入管理员平台密码。'),
                ('client', '当前客户端账号固定为本机身份。', '请输入该客户端账号的 SSH 密码。'),
            ):
                page.evaluate("target => { document.getElementById('auth-username').dataset.accountSwitchTarget = target; showAuthGate(false); }", target)
                expect(page.locator('#auth-username-help')).to_contain_text(username_help)
                expect(page.locator('#auth-password-help')).to_have_text(password_help)
            page.evaluate("delete document.getElementById('auth-username').dataset.accountSwitchTarget; showAuthGate(false)")
            expect(page.locator('#auth-username-help')).to_have_text('管理员账号，或 用户名@客户端IP（SSH 账号）。')
            expect(page.locator('#auth-password-help')).to_have_text('管理员用平台密码；客户端用 SSH 密码。')
        finally:
            page.close()
