"""Agent access, enrollment UI and platform quick actions."""

import json
import re

from tests.runtime_ui.harness import RuntimeUiHarness, expect


class RuntimeAgentTests(RuntimeUiHarness):
    def test_agent_access_panel_renders_real_scopes_after_delayed_auth(self):
        """真实浏览器验收。

        - 非管理员登录态经 gms:auth-ready 事件补发（慢登录/重登路径）：入口立即显示；
        - 非管理员点击入口时弹出管理员验证，验证成功后才进入管理面板；
        - 真实登录 + 真实 /api/auth/agent-scopes 契约（{scope: 描述} 对象）：
          权限复选框按对象键渲染并保留默认勾选集。
        """
        page = self.browser.new_page(
            viewport={"width": 1440, "height": 960},
            bypass_csp=True,
        )
        page.set_default_timeout(8000)
        page.set_default_navigation_timeout(15000)
        page_errors = []
        page.on("pageerror", lambda error: page_errors.append(error))
        try:
            # 非管理员登录态恢复时也保留入口；点击后必须先走管理员验证。
            page.goto(self.base_url, wait_until="domcontentloaded")
            expect(page.locator("#auth-gate")).to_be_visible()
            entry = page.locator("#agent-access-entry")
            page.evaluate(
                """
                () => {
                  state.currentUser = {role: 'user', is_admin: false};
                  state.authReady = true;
                  state.elevated = false;
                  window.dispatchEvent(new CustomEvent('gms:auth-ready'));
                  switchPage('users', null);
                }
                """
            )
            expect(entry).to_be_visible()
            # 这里模拟的是认证已恢复、但 auth-gate 的 DOM 尚待上一轮
            # 登录流程收起的时序；直接触发入口以断言其提权分支。
            page.click("#agent-access-entry button", force=True)
            expect(page.locator("#elevate-modal")).to_have_class(re.compile(r"show"))
            page.evaluate("() => cancelElevate()")

            # 真实登录（cookie 落到同一浏览器上下文）后重载，走真实 API。
            # 该页面跳过了 new_page() 的预登录，这里按同一约定先确保
            # 管理员账号存在（首次运行走 setup）。
            status = page.request.get(f"{self.base_url}/api/auth/status")
            self.assertTrue(status.ok, status.text())
            endpoint = (
                "setup" if status.json().get("setup_required") else "login"
            )
            authenticated = page.request.post(
                f"{self.base_url}/api/auth/{endpoint}",
                data={
                    "username": "ui-admin",
                    "password": "UiSmokeAdmin-2026!",
                    "display_name": "UI Smoke Admin",
                },
            )
            self.assertTrue(authenticated.ok, authenticated.text())
            page.reload(wait_until="domcontentloaded")
            page.wait_for_selector(".sidebar-item[data-page]")
            self.close_initial_modals(page)
            self.show_all_sidebar_pages(page)
            page.click(".sidebar-item[data-page='users']")
            # 用户管理页在未提权时会自动弹出 elevate-modal（loadUsersList
            # 的提权流程），先关掉再点 Agent 接入入口。
            self.close_initial_modals(page)
            page.click("#agent-access-entry button")
            page.wait_for_function(
                "document.querySelectorAll("
                "'#agent-access-scopes .agent-access-scope').length > 0"
            )
            self.assertEqual(
                page.locator("#agent-access-panel thead th").all_text_contents()[:2],
                ["Agent", "Token ID"],
            )
            scopes = page.evaluate(
                "() => Array.from(document.querySelectorAll("
                "'#agent-access-scopes .agent-access-scope'))"
                ".map((input) => input.value)"
            )
            # AGENT_SCOPES 服务端契约：对象键集合（修复前是 0 个复选框）。
            # 与 features/auth/constants.py AGENT_SCOPES 保持同步
            # （ADR 0014：knowledge.read 背景知识只读检索）。
            self.assertEqual(
                sorted(scopes),
                sorted([
                    "devices.read",
                    "devices.lease",
                    "devices.use_leased",
                    "devices.inventory",
                    "tests.execute",
                    "tests.cancel",
                    "jobs.read",
                    "reports.read",
                    "redmine.read",
                    "artifacts.read_own",
                    "apk.analyze_own",
                    "sdk.read",
                    "knowledge.read",
                    "build.read",
                    "build.execute",
                    "build.cancel",
                ]),
            )
            self.assertTrue(
                page.evaluate(
                    "document.querySelector("
                    "'#agent-access-scopes input[value=\"tests.execute\"]'"
                    ").checked"
                )
            )
            self.assert_no_page_errors(page_errors)
        finally:
            page.close()

    def test_admin_init_does_not_prefetch_users_list_or_prompt_elevation(self):
        # /api/users/list 需要管理员提权，且每个新标签页都会重置提权；
        # 启动预取只会在页面加载时触发 403 和提权弹框。用户列表应由
        # 用户管理页的 loadUsersList() 按需加载。
        page = self.new_page()
        users_requests = []
        page.on(
            "request",
            lambda request: users_requests.append(request.url)
            if "/api/users/list" in request.url
            else None,
        )
        try:
            page.goto(self.base_url, wait_until="domcontentloaded")
            page.wait_for_selector(".sidebar-item[data-page]")
            # 预取在页面加载后 100ms 内发生；多等一会确保断言覆盖到该窗口。
            page.wait_for_timeout(800)
            expect(page.locator("#elevate-modal")).not_to_have_class(
                re.compile(r"\bshow\b")
            )
            self.assertEqual(users_requests, [])
        finally:
            page.close()

    def test_security_audit_unelevated_state_offers_a_working_elevation_button(self):
        # 回归：安全审计空状态曾提示「点击右上角提权」，但 shell 顶部并无
        # 该按钮，未提权用户被卡死在提示行、没有任何可点击入口。现在空状态
        # 内嵌提权按钮，点击后走 requestElevatedAccess 并在成功后原地重载。
        page = self.new_page()
        page_errors = []
        page.on("pageerror", lambda exc: page_errors.append(str(exc)))
        try:
            self.goto_shell(page)
            page.wait_for_function("typeof window.loadSecurityAudit === 'function'")
            page.evaluate("window.switchPage('security-audit')")
            expect(page.locator("#page-security-audit")).to_have_class(
                re.compile(r"active")
            )
            page.evaluate(
                """() => {
                  state.authRequired = true;
                  state.elevated = false;
                  state.currentUser = {username: 'ui-smoke-admin'};
                  window.__elevateCalls = [];
                  window.__originalRequestElevatedAccess = window.requestElevatedAccess;
                  window.requestElevatedAccess = async (label) => {
                    window.__elevateCalls.push(label);
                    return true;
                  };
                  securityAuditState.loaded = false;
                  window.__auditReloads = 0;
                  window.__originalLoadSecurityAudit = window.loadSecurityAudit;
                  window.loadSecurityAudit = (reset) => {
                    window.__auditReloads += 1;
                    return window.__originalLoadSecurityAudit(reset);
                  };
                  return window.loadSecurityAudit(true);
                }"""
            )
            gate = page.locator(
                "#security-audit-table-body [data-click='elevateSecurityAuditView']"
            )
            expect(gate).to_be_visible()
            # 旧文案不得再出现（它指向不存在的「右上角」按钮）。
            expect(page.locator("#security-audit-table-body")).not_to_contain_text(
                "右上角"
            )
            gate.click()
            page.wait_for_function("window.__auditReloads >= 1")
            self.assertEqual(
                page.evaluate("window.__elevateCalls"),
                ["查看安全审计"],
            )
            page.evaluate(
                """() => {
                  window.loadSecurityAudit = window.__originalLoadSecurityAudit;
                  window.requestElevatedAccess = window.__originalRequestElevatedAccess;
                }"""
            )
            self.assert_no_page_errors(page_errors)
        finally:
            page.close()

    def test_agent_page_quick_action_opens_every_sidebar_page(self):
        page = self.new_page()
        page_errors = []
        page.on("pageerror", lambda exc: page_errors.append(str(exc)))
        try:
            self.goto_shell(page)
            page.wait_for_function("typeof openAgentPageAction === 'function'")
            test_page_frame = page.locator("#page-test").evaluate(
                """container => ({
                    borderTopWidth: getComputedStyle(container).borderTopWidth,
                    marginTop: getComputedStyle(container).marginTop,
                    paddingTop: getComputedStyle(container).paddingTop,
                })"""
            )
            self.assertEqual(
                test_page_frame,
                {"borderTopWidth": "0px", "marginTop": "0px", "paddingTop": "15px"},
            )

            expected_page_frame = None
            expected_analysis_upload_frame = None
            for page_name in self.visible_sidebar_pages(page):
                with self.subTest(page=page_name):
                    page.evaluate("target => openAgentPageAction(target, '{}')", page_name)
                    expect(page.locator(f"#page-{page_name}")).to_have_class(re.compile(r"active"))
                    expect(page.locator(f'.sidebar-item[data-page="{page_name}"]')).to_have_class(re.compile(r"active"))
                    if page_name != "test":
                        page_geometry = page.locator(f"#page-{page_name}").evaluate(
                            """container => {
                                const pageRect = container.getBoundingClientRect();
                                const title = container.querySelector('.section-title');
                                const titleRect = title ? title.getBoundingClientRect() : null;
                                const firstChild = container.firstElementChild;
                                const content = firstChild && firstChild.nextElementSibling;
                                const contentRect = content ? content.getBoundingClientRect() : null;
                                const containerStyle = getComputedStyle(container);
                                const frame = getComputedStyle(container, '::after');
                                return {
                                    pageLeft: pageRect.left,
                                    pageTop: pageRect.top,
                                    pageWidth: pageRect.width,
                                    pageHeight: pageRect.height,
                                    titleLeft: titleRect && titleRect.left,
                                    titleTop: titleRect && titleRect.top,
                                    contentLeft: contentRect && contentRect.left,
                                    contentTop: contentRect && contentRect.top,
                                    titleToContent: titleRect && contentRect && contentRect.top - titleRect.bottom,
                                    frameTop: parseFloat(frame.top),
                                    frameRight: parseFloat(frame.right),
                                    frameBottom: parseFloat(frame.bottom),
                                    frameLeft: parseFloat(frame.left),
                                    frameBorder: frame.borderTopWidth,
                                    frameDisplay: frame.display,
                                    pageBorder: containerStyle.borderTopWidth,
                                    pageMargin: containerStyle.marginTop,
                                };
                            }"""
                        )
                        page_frame = {
                            key: page_geometry[key]
                            for key in ["pageLeft", "pageTop", "pageWidth", "pageHeight"]
                        }
                        if expected_page_frame is None:
                            expected_page_frame = page_frame
                        else:
                            self.assertEqual(page_frame, expected_page_frame)
                        self.assertAlmostEqual(
                            page_geometry["titleLeft"] - page_geometry["pageLeft"],
                            15,
                            delta=0.5,
                        )
                        self.assertAlmostEqual(
                            page_geometry["titleTop"] - page_geometry["pageTop"],
                            15,
                            delta=0.5,
                        )
                        self.assertAlmostEqual(
                            page_geometry["contentLeft"],
                            page_geometry["titleLeft"],
                            delta=0.5,
                        )
                        self.assertAlmostEqual(page_geometry["titleToContent"], 6, delta=0.5)
                        self.assertAlmostEqual(page_geometry["frameLeft"], 15, delta=0.5)
                        self.assertAlmostEqual(page_geometry["frameRight"], 15, delta=0.5)
                        self.assertAlmostEqual(page_geometry["frameBottom"], 15, delta=0.5)
                        self.assertAlmostEqual(
                            page_geometry["pageTop"] + page_geometry["frameTop"],
                            page_geometry["contentTop"],
                            delta=0.5,
                        )
                        self.assertEqual(page_geometry["frameBorder"], "1px")
                        self.assertEqual(page_geometry["pageBorder"], "0px")
                        self.assertEqual(page_geometry["pageMargin"], "0px")
                        overlapping_frames = page.locator(f"#page-{page_name}").evaluate(
                            """container => {
                                const pageRect = container.getBoundingClientRect();
                                const frameStyle = getComputedStyle(container, '::after');
                                if (frameStyle.display === 'none') return [];
                                const target = {
                                    left: pageRect.left + parseFloat(frameStyle.left),
                                    top: pageRect.top + parseFloat(frameStyle.top),
                                    right: pageRect.right - parseFloat(frameStyle.right),
                                    bottom: pageRect.bottom - parseFloat(frameStyle.bottom),
                                };
                                return Array.from(container.querySelectorAll('*')).filter(node => {
                                    const style = getComputedStyle(node);
                                    if (style.display === 'none' || style.visibility === 'hidden') return false;
                                    if (parseFloat(style.borderTopWidth) <= 0) return false;
                                    const rect = node.getBoundingClientRect();
                                    return Math.abs(rect.left - target.left) <= 1
                                        && Math.abs(rect.top - target.top) <= 1
                                        && Math.abs(rect.right - target.right) <= 1
                                        && Math.abs(rect.bottom - target.bottom) <= 1;
                                }).map(node => node.id || node.className || node.tagName);
                            }"""
                        )
                        self.assertEqual(overlapping_frames, [])
                        if page_name in {"report-analysis", "apk-analysis"}:
                            upload_selector = (
                                "#report-upload-zone"
                                if page_name == "report-analysis"
                                else "#apk-upload-zone"
                            )
                            expect(page.locator(upload_selector)).to_have_class(
                                re.compile(r"\bupload-empty\b")
                            )
                            upload_geometry = page.locator(upload_selector).evaluate(
                                """zone => {
                                    const rect = zone.getBoundingClientRect();
                                    const pageRect = zone.closest('.page-content').getBoundingClientRect();
                                    const pageTitleStyle = getComputedStyle(
                                        zone.closest('.page-content').querySelector('.section-title')
                                    );
                                    const title = zone.querySelector('.analysis-upload-title');
                                    const instructions = zone.querySelector('.upload-instructions');
                                    const button = zone.querySelector('button');
                                    const titleRect = title.getBoundingClientRect();
                                    const buttonRect = button.getBoundingClientRect();
                                    const titleStyle = getComputedStyle(title);
                                    const instructionsStyle = getComputedStyle(instructions);
                                    const buttonStyle = getComputedStyle(button);
                                    return {
                                        left: rect.left - pageRect.left,
                                        top: rect.top - pageRect.top,
                                        bottom: pageRect.bottom - rect.bottom,
                                        width: rect.width,
                                        height: rect.height,
                                        titleLeft: titleRect.left - pageRect.left,
                                        titleTop: titleRect.top - pageRect.top,
                                        buttonLeft: buttonRect.left - pageRect.left,
                                        buttonTop: buttonRect.top - pageRect.top,
                                        buttonWidth: buttonRect.width,
                                        borderStyle: getComputedStyle(zone).borderTopStyle,
                                        borderWidth: getComputedStyle(zone).borderTopWidth,
                                        borderRadius: getComputedStyle(zone).borderTopLeftRadius,
                                        minHeight: getComputedStyle(zone).minHeight,
                                        pageTitleFont: [
                                            pageTitleStyle.fontFamily,
                                            pageTitleStyle.fontSize,
                                            pageTitleStyle.fontWeight,
                                            pageTitleStyle.lineHeight,
                                        ],
                                        titleFont: [
                                            titleStyle.fontFamily,
                                            titleStyle.fontSize,
                                            titleStyle.fontWeight,
                                            titleStyle.lineHeight,
                                        ],
                                        instructionsFont: [
                                            instructionsStyle.fontFamily,
                                            instructionsStyle.fontSize,
                                            instructionsStyle.fontWeight,
                                            instructionsStyle.lineHeight,
                                        ],
                                        buttonFont: [
                                            buttonStyle.fontFamily,
                                            buttonStyle.fontSize,
                                            buttonStyle.fontWeight,
                                            buttonStyle.lineHeight,
                                        ],
                                    };
                                }"""
                            )
                            if expected_analysis_upload_frame is None:
                                expected_analysis_upload_frame = upload_geometry
                            else:
                                self.assertEqual(
                                    upload_geometry,
                                    expected_analysis_upload_frame,
                                )
                            self.assertAlmostEqual(upload_geometry["left"], 15, delta=0.5)
                            self.assertAlmostEqual(upload_geometry["top"], 40, delta=0.5)
                            self.assertAlmostEqual(
                                upload_geometry["height"],
                                page.evaluate("window.innerHeight * 0.5"),
                                delta=0.5,
                            )
                            self.assertGreater(upload_geometry["bottom"], 15)
                            self.assertEqual(upload_geometry["borderStyle"], "solid")
                            self.assertEqual(upload_geometry["borderWidth"], "1px")
                            self.assertEqual(upload_geometry["borderRadius"], "8px")
                            self.assertEqual(
                                upload_geometry["minHeight"],
                                f"{page.evaluate('window.innerHeight * 0.5'):g}px",
                            )
                            self.assertAlmostEqual(upload_geometry["buttonWidth"], 160, delta=0.5)
                            self.assertEqual(page_geometry["frameDisplay"], "none")
                            if page_name == "report-analysis":
                                self.assertEqual(
                                    page.locator("#report-upload-progress").evaluate(
                                        "progress => getComputedStyle(progress).opacity"
                                    ),
                                    "0",
                                )
                                page.locator(upload_selector).evaluate(
                                    "zone => zone.classList.remove('upload-empty')"
                                )
                                result_state_frame = page.locator(f"#page-{page_name}").evaluate(
                                    """(container, selector) => {
                                        const zone = container.querySelector(selector);
                                        const frame = getComputedStyle(container, '::after');
                                        return {
                                            frameDisplay: frame.display,
                                            uploadBorder: getComputedStyle(zone).borderTopWidth,
                                            uploadHeight: zone.getBoundingClientRect().height,
                                        };
                                    }""",
                                    upload_selector,
                                )
                                self.assertEqual(result_state_frame["frameDisplay"], "block")
                                self.assertEqual(result_state_frame["uploadBorder"], "0px")
                                self.assertAlmostEqual(
                                    result_state_frame["uploadHeight"],
                                    80,
                                    delta=0.5,
                                )
                                page.locator(upload_selector).evaluate(
                                    "zone => zone.classList.add('upload-empty')"
                                )
                        if page_name == "architecture":
                            architecture_frame = page.locator(
                                "#page-architecture .architecture-container"
                            ).evaluate(
                                """container => {
                                    const style = getComputedStyle(container);
                                    const iframeStyle = getComputedStyle(container.querySelector('iframe'));
                                    return {
                                        background: style.backgroundColor,
                                        border: style.borderTopWidth,
                                        padding: style.paddingTop,
                                        radius: style.borderTopLeftRadius,
                                        iframeRadius: iframeStyle.borderTopLeftRadius,
                                    };
                                }"""
                            )
                            self.assertEqual(
                                architecture_frame,
                                {
                                    "background": "rgba(0, 0, 0, 0)",
                                    "border": "0px",
                                    "padding": "0px",
                                    "radius": "8px",
                                    "iframeRadius": "7px",
                                },
                            )

            page.evaluate("openAgentPageAction('redmine-agent', JSON.stringify({tab:'department', name:'黄超群'}))")
            redmine_src = page.locator("#redmine-agent-frame").get_attribute("src")
            self.assertIsNotNone(redmine_src)
            self.assertIn("tab=department", redmine_src)
            self.assertIn("name=", redmine_src)

            page.evaluate("openAgentPageAction('gerrit-dashboard', '{}')")
            gerrit_src = page.locator("#gerrit-dashboard-frame").get_attribute("src")
            self.assertEqual(gerrit_src, "/gerrit-dashboard")

            self.assert_no_page_errors(page_errors)
        finally:
            page.close()

    def test_agent_distinguishes_rule_router_from_analysis_model_availability(self):
        page = self.new_page()
        probe_requests = []

        def ai_status(route):
            is_probe = "probe=true" in route.request.url
            probe_requests.append(is_probe)
            provider = {
                "provider": "glm_local",
                "name": "Local GLM",
                "model": "glm-local",
                "enabled": True,
                "local": True,
                "configured": True,
                "credential_configured": is_probe,
                "state": "available" if is_probe else "credential_missing",
                "available": True if is_probe else None,
                "checked": is_probe,
            }
            if is_probe:
                provider["latency_ms"] = 42
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({
                    "success": True,
                    "data": {
                        "status": {
                            "local_provider": "glm_local",
                            "primary_provider": "remote",
                            "providers": [provider],
                        }
                    },
                }),
            )

        page.route("**/api/config/ai*", ai_status)
        try:
            self.goto_shell(page)
            page.evaluate("switchPage('agent', null)")
            status = page.locator("#agent-model-status")
            expect(status).to_have_text("本地模型：缺少密钥")
            self.assertIn("Agent 指令路由使用确定性规则", status.get_attribute("title"))

            page.locator("#agent-model-check").click()
            expect(status).to_have_text("本地模型：可用 · 42ms")
            expect(status).to_have_attribute("data-state", "available")
            self.assertEqual(probe_requests, [False, True])
        finally:
            page.close()

    def test_skill_install_action_uses_current_controller_and_keeps_zip_offline(self):
        page = self.new_page()
        try:
            self.goto_shell(page)
            result = page.evaluate(
                """() => {
                    const install = buildSkillInstallCommand();
                    const installApi = generateCurlCommand({
                        method: 'GET',
                        path: '/api/agent/install.sh',
                    }, {});
                    const archiveApi = generateCurlCommand({
                        method: 'GET',
                        path: '/api/system/skills',
                    }, {params: []});
                    return {
                        install,
                        installApi,
                        archiveApi,
                        primaryLabel: Array.from(document.querySelectorAll('button'))
                            .find(button => button.textContent.includes('安装/更新命令'))
                            ?.textContent.trim(),
                        offlineLabel: Array.from(document.querySelectorAll('button'))
                            .find(button => button.textContent.includes('离线包'))
                            ?.textContent.trim(),
                    };
                }"""
            )

            expected = (
                'export GMS_INSTALL_CA_CERT=/path/to/controller-ca.crt; '
                f'curl --cacert "$GMS_INSTALL_CA_CERT" -fsSL "{self.base_url}/api/agent/install.sh" | '
                "bash -s -- --paircode-prompt"
            )
            self.assertEqual(result["install"], expected)
            self.assertEqual(result["installApi"]["full"], expected)
            self.assertIn("-OJ", result["archiveApi"]["full"])
            self.assertNotIn("| bash", result["archiveApi"]["full"])
            self.assertEqual(result["primaryLabel"], "📋 安装/更新命令")
            self.assertEqual(result["offlineLabel"], "📦 离线包")
        finally:
            page.close()
