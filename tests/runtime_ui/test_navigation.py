"""Shell startup, page navigation and refresh lifecycle."""

import json
import re

from tests.runtime_ui.harness import REPO_ROOT, RuntimeUiHarness, expect


class RuntimeNavigationTests(RuntimeUiHarness):
    def test_vpn_proxy_builtin_card_opens_management_panel_once(self):
        page = self.new_page()
        try:
            # Existing localStorage data must not prevent newly added built-ins
            # from appearing, nor should refreshing duplicate them.
            page.add_init_script("""
                localStorage.setItem('gms_utility_tools_categories', JSON.stringify({
                    '常用工具': [{icon: '📦', title: '已有工具', tool_id: 'existing-tool'}],
                    '测试工具': []
                }));
                localStorage.removeItem('gms_utility_tools_hidden_builtins');
            """)
            self.goto_shell(page)
            page.evaluate("switchPage('tools', null)")
            card = page.locator(
                '#ut-grid .tool-card[data-tools-action="builtin-open"]'
                '[data-builtin-action="openVpnProxyPanel"]'
            )
            expect(card).to_have_count(1)
            expect(card.locator('.tool-title')).to_have_text('vpn-proxy')
            self.assertIn('VPN HTTP/HTTPS', card.get_attribute('title') or '')
            self.assertIn('已有工具', page.locator('#ut-grid').inner_text())

            page.route('**/api/tools/vpn-proxy/status', lambda route: route.fulfill(
                status=200, content_type='application/json',
                body=json.dumps({'success': True, 'data': {
                    'installed': True,
                    'services': {'proxy': 'active', 'firewall': 'active',
                                 'stats': 'active', 'installer': 'active'},
                    'config': {'proxy_url': 'http://proxy.test:7897',
                               'installer_enabled': True, 'allowed_clients': []},
                    'clients': {'connected': [], 'recent': [],
                                'sources': {'connections': True, 'access_log': True}},
                    'install': {'linux': 'echo linux', 'windows': 'echo windows'},
                    'stats_error': None,
                }})
            ))
            page.evaluate("() => { window.open = () => { throw Error('unexpected external tab'); }; }")
            card.click()
            expect(page.locator('#vpn-proxy-modal')).to_have_class(re.compile('show'))
            expect(page.locator('#vpn-proxy-status')).to_contain_text('运行中')
            expect(page.locator('#vpn-proxy-linux-command')).to_have_value('echo linux')
            page.locator('#vpn-proxy-close').click()
            expect(page.locator('#vpn-proxy-modal')).not_to_have_class(re.compile('show'))

            page.evaluate("ut_loadToolsList(); ut_loadToolsList();")
            expect(card).to_have_count(1)
            expect(card.locator('button[data-tools-action="hide-builtin"]')).to_have_count(1)
            # Hide persists; built-in reconciliation must respect user's choice.
            card.locator('button[data-tools-action="hide-builtin"]').click()
            page.evaluate("ut_loadToolsList()")
            expect(card).to_have_count(0)
        finally:
            page.close()

    def test_deleted_report_category_survives_refresh_and_manual_readd(self):
        page = self.new_page()
        try:
            self.goto_shell(page)
            page.evaluate("""() => {
                localStorage.setItem('gms_utility_tools_categories', JSON.stringify({
                    '报告工具': [{icon: '📅', title: '周报总结',
                                  builtin_id: 'builtin-weekly-report'}],
                    '测试工具': []
                }));
                localStorage.removeItem('gms_utility_tools_hidden_categories');
                localStorage.removeItem('gms_utility_tools_hidden_builtins');
                ut_loadToolsList();
                switchPage('tools', null);
            }""")
            report = page.locator('#ut-grid .category-section[data-category="报告工具"]')
            expect(report).to_have_count(1)

            page.evaluate("() => { window.showConfirmDialog = async () => true; }")
            page.evaluate("ut_deleteCategory('报告工具')")
            expect(report).to_have_count(0)
            hidden = page.evaluate(
                "() => JSON.parse(localStorage.getItem('gms_utility_tools_hidden_categories'))"
            )
            self.assertIn('报告工具', hidden)

            page.evaluate("ut_loadToolsList(); ut_loadToolsList();")
            expect(report).to_have_count(0)
            page.reload()
            page.evaluate("switchPage('tools', null)")
            expect(report).to_have_count(0)
            self.assertNotIn('报告工具', page.evaluate(
                "() => Object.keys(JSON.parse(localStorage.getItem('gms_utility_tools_categories')))"
            ))
            self.assertGreaterEqual(page.locator('#ut-grid .category-section').count(), 1)

            # Recreating the name explicitly restores the built-in card.
            page.evaluate("""() => {
                ut_editingCategory = null;
                document.getElementById('ut-category-name').value = '报告工具';
                document.getElementById('ut-category-icon').value = '📅';
                ut_saveCategory();
            }""")
            expect(report).to_have_count(1)
            expect(report.locator('.tool-title')).to_contain_text('周报总结')
            page.reload()
            page.evaluate("switchPage('tools', null)")
            expect(report).to_have_count(1)
        finally:
            page.close()

    def test_vpn_proxy_client_access_controls_allow_reenable_after_disconnect(self):
        page = self.new_page()
        disabled = set()
        requests = []

        def status(route):
            connected = [] if disabled else [
                {'client': '172.16.14.66', 'connections': 2,
                 'network': '172.16.14.0/24'}
            ]
            route.fulfill(status=200, content_type='application/json',
                          body=json.dumps({'success': True, 'data': {
                              'installed': True,
                              'services': {'proxy': 'active', 'firewall': 'active',
                                           'stats': 'active', 'installer': 'active'},
                              'config': {'proxy_url': 'http://proxy.test:7897',
                                         'allowed_clients': ['172.16.14.0/24']},
                              'client_access': {'available': True,
                                                'disabled': sorted(disabled),
                                                'error': None},
                              'clients': {'connected': connected, 'recent': [],
                                          'sources': {'connections': True,
                                                      'access_log': True}},
                              'install': {'linux': '', 'windows': ''},
                              'stats_error': None,
                          }}))

        def control(route):
            request = json.loads(route.request.post_data)
            requests.append(request)
            if request['action'] == 'disable':
                disabled.add(request['client'])
            elif request['action'] == 'enable':
                disabled.discard(request['client'])
            route.fulfill(status=200, content_type='application/json',
                          body=json.dumps({'success': True, 'data': request}))

        try:
            self.goto_shell(page)
            page.route('**/api/tools/vpn-proxy/status', status)
            page.route('**/api/tools/vpn-proxy/client-control', control)
            page.evaluate('() => { window.showConfirmDialog = async () => true; }')
            page.evaluate('openVpnProxyPanel()')
            row = page.locator('#vpn-proxy-connected tr').filter(
                has_text='172.16.14.66'
            )
            expect(row).to_have_count(1)
            expect(row.locator('.vpn-proxy-client-toggle')).to_have_text('禁用')
            row.locator('.vpn-proxy-client-toggle').click()
            expect(row.locator('.vpn-proxy-client-state')).to_have_text('已禁用')
            expect(row.locator('.vpn-proxy-client-toggle')).to_have_text('启用')
            self.assertEqual(requests[-1], {'client': '172.16.14.66',
                                            'action': 'disable'})
            # The disabled client has no TCP connections, yet remains listed.
            self.assertEqual(row.locator('td').nth(1).inner_text(), '—')
            row.locator('.vpn-proxy-client-toggle').click()
            expect(row.locator('.vpn-proxy-client-state')).to_have_text('允许访问')
            self.assertEqual(requests[-1]['action'], 'enable')
        finally:
            page.close()

    def test_tools_pages_keep_long_lists_scrollable_after_repeated_navigation(self):
        page = self.new_page()
        try:
            page.add_init_script("""
                const tools = Array.from({length: 80}, (_, index) => ({
                    icon: '🔧', title: `Layout ${index}`, url: 'https://example.test/'
                }));
                for (const key of ['gms_tools_categories', 'gms_utility_tools_categories']) {
                    localStorage.setItem(key, JSON.stringify({'Layout test': tools}));
                }
            """)
            self.goto_shell(page)
            for kind in ("websites", "tools", "websites", "tools"):
                with self.subTest(page=kind):
                    page.evaluate("kind => switchPage(kind, null)", kind)
                    host = page.locator(f'[data-tools-page="{kind}"]')
                    expect(host.locator('.tool-card').nth(79)).to_be_attached()
                    geometry = host.evaluate("""host => {
                        const scroll = host.querySelector('.tools-scroll');
                        const footer = host.querySelector('.tools-footer');
                        const page = host.closest('.page-content');
                        scroll.scrollTop = scroll.scrollHeight;
                        return {
                            clientHeight: scroll.clientHeight,
                            scrollHeight: scroll.scrollHeight,
                            scrollTop: scroll.scrollTop,
                            footerBottom: footer.getBoundingClientRect().bottom,
                            pageBottom: page.getBoundingClientRect().bottom,
                        };
                    }""")
                    self.assertGreater(geometry['clientHeight'], 0)
                    self.assertGreater(geometry['scrollHeight'], geometry['clientHeight'])
                    self.assertGreater(geometry['scrollTop'], 0)
                    self.assertLessEqual(geometry['footerBottom'], geometry['pageBottom'])
                    self.assertEqual(host.locator('.tools-footer').count(), 1)
                    self.assertEqual(page.locator(f'#page-{kind} .tools-header-add').count(), 0)
                    host.locator('.tools-footer .tools-add-category').click()
                    modal = page.locator('#ut-category-modal' if kind == 'tools' else '#category-modal')
                    expect(modal).to_have_class(re.compile(r"show"))
                    page.keyboard.press('Escape')
                    expect(modal).not_to_have_class(re.compile(r"show"))
            self.assertEqual(page.locator('#tools-grid').count(), 1)
            self.assertEqual(page.locator('#ut-grid').count(), 1)
        finally:
            page.close()

    def test_tools_and_websites_allow_scrolling_to_fourth_group_with_bottom_action(self):
        """Every category stays reachable while the bottom add action remains visible."""
        page = self.new_page()
        try:
            page.set_viewport_size({"width": 1792, "height": 856})
            page.add_init_script("""
                const data = {
                    '测试工具': Array.from({length: 7}, (_, i) => ({
                        icon: '✅', title: `Test ${i}`, url: 'https://example.test/'
                    })),
                    'Gerrit工具': [{icon:'📦',title:'Gerrit',url:'https://example.test/'}],
                    '烧写工具': [{icon:'🔥',title:'Flash',url:'https://example.test/'}],
                    '报告工具': [{icon:'📊',title:'Report',url:'https://example.test/'}]
                };
                for (const key of ['gms_tools_categories', 'gms_utility_tools_categories'])
                    localStorage.setItem(key, JSON.stringify(data));
            """)
            self.goto_shell(page)
            for kind in ('tools', 'websites'):
                page.evaluate("kind => switchPage(kind, null)", kind)
                host = page.locator(f'[data-tools-page="{kind}"]')
                sections = host.locator('.category-section')
                # Both pages initialize lazily; wait for the category grid
                # to be populated instead of asserting during navigation.
                page.wait_for_function(
                    "kind => document.querySelector(`[data-tools-page=\"${kind}\"]`)"
                    "?.querySelectorAll('.category-section').length >= 4",
                    arg=kind,
                )
                self.assertGreaterEqual(sections.count(), 4)
                geometry = host.evaluate("""host => {
                    const element = host.querySelectorAll('.category-section')[3];
                    element.scrollIntoView({block: 'end'});
                    const scroll = host.querySelector('.tools-scroll').getBoundingClientRect();
                    const last = element.getBoundingClientRect();
                    const footer = host.querySelector('.tools-footer').getBoundingClientRect();
                    return {
                        visibleBottom: scroll.bottom,
                        fourthBottom: last.bottom,
                        visibleTop: scroll.top,
                        fourthTop: last.top,
                        footerTop: footer.top,
                        footerBottom: footer.bottom,
                        hostBottom: host.getBoundingClientRect().bottom
                    };
                }""")
                self.assertGreaterEqual(geometry['fourthTop'], geometry['visibleTop'] - 2)
                self.assertLessEqual(geometry['fourthBottom'], geometry['visibleBottom'] + 2)
                self.assertGreaterEqual(geometry['footerTop'], geometry['visibleBottom'])
                self.assertLessEqual(geometry['footerBottom'], geometry['hostBottom'] + 2)
        finally:
            page.close()

    def test_repeated_category_initialization_binds_each_node_once(self):
        page = self.new_page()
        try:
            self.goto_shell(page)
            result = page.evaluate("""() => {
                const section = document.createElement('section');
                section.className = 'category-section';
                const calls = [];
                const original = section.addEventListener.bind(section);
                section.addEventListener = (type, listener, options) => {
                    calls.push(type);
                    original(type, listener, options);
                };
                document.body.append(section);
                initDragAndDrop();
                initDragAndDrop();
                switchPage('websites', null);
                switchPage('test', null);
                switchPage('websites', null);
                initDragAndDrop();
                section.remove();
                return calls;
            }""")
            self.assertEqual(sorted(result), ['dragend', 'dragover', 'dragstart', 'drop'])
        finally:
            page.close()

    def test_sidebar_pages_switch_without_runtime_errors(self):
        page = self.new_page()
        page_errors = []
        page.on("pageerror", lambda exc: page_errors.append(str(exc)))
        try:
            self.goto_shell(page)
            pages = page.locator(".sidebar-item[data-page]").evaluate_all(
                "(items) => items.map(item => item.dataset.page)"
            )
            for page_name in pages:
                self.show_all_sidebar_pages(page)
                page.evaluate('(name) => window.switchPage(name, null)', page_name)
                expect(page.locator(f"#page-{page_name}")).to_have_class(re.compile(r"active"))
            self.assertEqual(page_errors, [])
        finally:
            page.close()

    def test_shell_boot_does_not_touch_inactive_page_endpoints(self):
        """Lazy activation: 未激活页面的端点在启动期必须零请求。

        '文件拆开了'不等于'运行生命周期
        拆开了'。shell 启动落在非 websites 页面时，websites 的 load/save
        端点都不应被触碰；set-username 在已有本地缓存时也不再重复登记。
        """
        page = self.new_page()
        blocked_paths = []
        page.on(
            "request",
            lambda request: blocked_paths.append(request.url)
            if any(
                marker in request.url
                for marker in (
                    "/api/websites/",
                    "/api/reports/weekly-report/",
                    "/api/opengrok/",
                )
            )
            else None,
        )
        try:
            page.add_init_script(
                """
                localStorage.setItem('gms_username_127.0.0.1', 'smoke-user');
                localStorage.setItem('gms_current_page', 'test');
                """
            )
            self.goto_shell(page)
            page.wait_for_timeout(800)
            self.assertEqual(
                [p for p in blocked_paths if "/api/websites/" in p],
                [],
                "inactive websites page was contacted during shell boot",
            )
        finally:
            page.close()

    def test_websites_init_and_migration_never_post_to_server(self):
        """Page init must not perform mutating network requests.

        Defaults/migration used to call saveCategories() unguarded, which
        POSTed /api/websites/save on every fresh page load and produced
        'Failed to fetch' console errors across ten unrelated E2E pages
        (review finding: Load / Migrate / Persist are local phases; only
        a real user mutation may Sync).
        """
        page = self.new_page()
        save_requests = []
        page.on(
            "request",
            lambda request: save_requests.append(request.url)
            if request.method == "POST" and request.url.endswith("/api/websites/save")
            else None,
        )
        try:
            # Seed the legacy flat storage so the init path exercises the
            # migrateToCategories + saveCategories branch, then load the
            # shell with an empty categorized store.
            page.add_init_script(
                """
                localStorage.setItem('gms_tools_categories', JSON.stringify({
                    '其他': [{ icon: '🧪', title: 'Legacy Tool', url: 'https://legacy.example' }]
                }));
                """
            )
            self.goto_shell(page)
            page.evaluate("(name) => window.switchPage(name, null)", "websites")
            expect(page.locator("#page-websites")).to_have_class(re.compile(r"active"))
            page.wait_for_timeout(500)
            self.assertEqual(
                save_requests,
                [],
                "websites page init performed a server write request",
            )
        finally:
            page.close()

    def test_sidebar_page_transitions_do_not_create_large_layout_shifts(self):
        page = self.new_page()
        try:
            self.goto_shell(page)
            page.evaluate(
                """() => {
                  window.__navigationLayoutShifts = [];
                  window.__navigationLayoutShiftObserver = new PerformanceObserver(list => {
                    list.getEntries().forEach(entry => {
                      if (!entry.hadRecentInput) {
                        window.__navigationLayoutShifts.push({
                          value: entry.value,
                          sources: entry.sources.map(source =>
                            source.node && (source.node.id || source.node.className || source.node.tagName)
                          ),
                        });
                      }
                    });
                  });
                  window.__navigationLayoutShiftObserver.observe({type: 'layout-shift'});
                }"""
            )
            for page_name in self.visible_sidebar_pages(page):
                with self.subTest(page=page_name):
                    self.show_all_sidebar_pages(page)
                    page.evaluate(
                        """name => {
                          window.__navigationLayoutShifts.length = 0;
                          switchPage(name, null);
                        }""",
                        page_name,
                    )
                    page.wait_for_timeout(450)
                    shifts = page.evaluate(
                        """() => ({
                          score: window.__navigationLayoutShifts.reduce(
                            (sum, entry) => sum + entry.value, 0
                          ),
                          entries: window.__navigationLayoutShifts,
                        })"""
                    )
                    self.assertLess(shifts["score"], 0.05, shifts)
        finally:
            page.close()

    def test_lazy_frames_use_stable_loading_surface_and_do_not_reload_on_revisit(self):
        page = self.new_page()
        try:
            self.goto_shell(page)
            frames = [
                ("gms-assistant", "#gms-assistant-frame"),
                ("automation", "#automation-frame"),
                ("cluster", "#cluster-frame"),
                ("redmine-agent", "#redmine-agent-frame"),
                ("gerrit-dashboard", "#gerrit-dashboard-frame"),
                ("architecture", "#architecture-iframe"),
            ]
            for page_name, selector in frames:
                with self.subTest(page=page_name):
                    loading = page.evaluate(
                        """([pageName, selector]) => {
                          switchPage(pageName, null);
                          const frame = document.querySelector(selector);
                          const shell = frame.closest('.embedded-frame-shell');
                          const rect = shell.getBoundingClientRect();
                          return {
                            state: shell.dataset.frameState,
                            progressState: shell.dataset.frameProgress,
                            frameVisibility: getComputedStyle(frame).visibility,
                            loaderDisplay: getComputedStyle(
                              shell.querySelector('.embedded-frame-loading')
                            ).display,
                            skeletonDisplay: getComputedStyle(
                              shell.querySelector('.embedded-frame-skeleton')
                            ).display,
                            skeletonCards: shell.querySelectorAll(
                              '.embedded-frame-skeleton-card'
                            ).length,
                            skeletonPanels: shell.querySelectorAll(
                              '.embedded-frame-skeleton-panel'
                            ).length,
                            progressVisibility: getComputedStyle(
                              shell.querySelector('.embedded-frame-loading-chip')
                            ).visibility,
                            rect: {width: rect.width, height: rect.height},
                          };
                        }""",
                        [page_name, selector],
                    )
                    self.assertEqual(loading["state"], "loading", loading)
                    self.assertEqual(loading["progressState"], "hidden", loading)
                    self.assertEqual(loading["frameVisibility"], "hidden", loading)
                    self.assertEqual(loading["loaderDisplay"], "block", loading)
                    self.assertEqual(loading["skeletonDisplay"], "grid", loading)
                    self.assertEqual(loading["skeletonCards"], 4, loading)
                    self.assertEqual(loading["skeletonPanels"], 2, loading)
                    self.assertEqual(loading["progressVisibility"], "hidden", loading)
                    self.assertGreater(loading["rect"]["height"], 100, loading)

                    page.wait_for_function(
                        "selector => document.querySelector(selector).closest("
                        "'.embedded-frame-shell').dataset.frameState === 'ready'",
                        arg=selector,
                    )
                    # Surface-ready may precede the document's initial load
                    # (for example while an async chart script is pending).
                    page.wait_for_function(
                        "selector => document.querySelector(selector).dataset.documentLoaded === 'true'",
                        arg=selector,
                    )
                    ready = page.evaluate(
                        """selector => {
                          const frame = document.querySelector(selector);
                          const shell = frame.closest('.embedded-frame-shell');
                          const rect = shell.getBoundingClientRect();
                          window.__lazyFrameReloads = 0;
                          frame.addEventListener('load', () => {
                            window.__lazyFrameReloads += 1;
                          });
                          return {
                            frameVisibility: getComputedStyle(frame).visibility,
                            loaderDisplay: getComputedStyle(
                              shell.querySelector('.embedded-frame-loading')
                            ).display,
                            rect: {width: rect.width, height: rect.height},
                          };
                        }""",
                        selector,
                    )
                    self.assertEqual(ready["frameVisibility"], "visible", ready)
                    self.assertEqual(ready["loaderDisplay"], "none", ready)
                    self.assertAlmostEqual(
                        loading["rect"]["width"], ready["rect"]["width"], delta=1
                    )
                    self.assertAlmostEqual(
                        loading["rect"]["height"], ready["rect"]["height"], delta=1
                    )

                    page.evaluate("switchPage('test', null)")
                    page.evaluate("name => switchPage(name, null)", page_name)
                    page.wait_for_timeout(150)
                    self.assertEqual(page.evaluate("window.__lazyFrameReloads"), 0)
                    self.assertEqual(
                        page.locator(selector).evaluate(
                            "frame => frame.closest('.embedded-frame-shell').dataset.frameState"
                        ),
                        "ready",
                    )
        finally:
            page.close()

    def test_lazy_frame_reload_keeps_previous_surface_visible(self):
        page = self.new_page()
        try:
            self.goto_shell(page)
            page.evaluate("switchPage('cluster', null)")
            page.wait_for_function(
                "document.querySelector('#cluster-frame').closest("
                "'.embedded-frame-shell').dataset.frameState === 'ready'"
            )

            reloading = page.evaluate(
                """() => {
                  const frame = document.querySelector('#cluster-frame');
                  const shell = frame.closest('.embedded-frame-shell');
                  const previousText = frame.contentDocument.body.innerText;
                  setLazyFrameSource(frame, frame.getAttribute('src'));
                  const loader = shell.querySelector('.embedded-frame-loading');
                  return {
                    state: shell.dataset.frameState,
                    hasContent: shell.dataset.frameHasContent,
                    frameVisibility: getComputedStyle(frame).visibility,
                    loaderDisplay: getComputedStyle(loader).display,
                    loaderBackground: getComputedStyle(loader).backgroundColor,
                    skeletonDisplay: getComputedStyle(
                      loader.querySelector('.embedded-frame-skeleton')
                    ).display,
                    previousContentRetained:
                      previousText.length > 0
                      && frame.contentDocument.body.innerText === previousText,
                  };
                }"""
            )
            self.assertEqual(reloading["state"], "loading", reloading)
            self.assertEqual(reloading["hasContent"], "true", reloading)
            self.assertEqual(reloading["frameVisibility"], "visible", reloading)
            self.assertEqual(reloading["loaderDisplay"], "block", reloading)
            self.assertEqual(reloading["loaderBackground"], "rgba(0, 0, 0, 0)", reloading)
            self.assertEqual(reloading["skeletonDisplay"], "none", reloading)
            self.assertTrue(reloading["previousContentRetained"], reloading)
            page.wait_for_function(
                "document.querySelector('#cluster-frame').closest("
                "'.embedded-frame-shell').dataset.frameState === 'ready'"
            )
        finally:
            page.close()

    def test_lazy_frame_progress_text_is_deferred_until_load_is_slow(self):
        page = self.new_page()
        try:
            self.goto_shell(page)
            initial = page.evaluate(
                """() => {
                  const shell = document.createElement('div');
                  shell.className = 'embedded-frame-shell';
                  shell.dataset.frameState = 'loading';
                  const status = document.createElement('div');
                  status.className = 'embedded-frame-loading';
                  status.dataset.loadingText = '页面仍在准备中…';
                  const frame = document.createElement('iframe');
                  // Keep the synthetic frame pending so the delay can be
                  // observed without depending on network timing.
                  frame.setAttribute = () => {};
                  shell.append(status, frame);
                  document.body.appendChild(shell);
                  setLazyFrameSource(frame, '/held-frame');
                  window.__deferredFrameFixture = {shell, frame};
                  return {
                    state: shell.dataset.frameState,
                    progress: shell.dataset.frameProgress,
                    chipVisibility: getComputedStyle(
                      status.querySelector('.embedded-frame-loading-chip')
                    ).visibility,
                  };
                }"""
            )
            self.assertEqual(initial, {
                "state": "loading",
                "progress": "hidden",
                "chipVisibility": "hidden",
            })

            page.wait_for_function(
                "window.__deferredFrameFixture.shell.dataset.frameProgress === 'visible'"
            )
            visible = page.evaluate(
                """() => {
                  const {shell, frame} = window.__deferredFrameFixture;
                  const chip = shell.querySelector('.embedded-frame-loading-chip');
                  const result = {
                    progress: shell.dataset.frameProgress,
                    chipVisibility: getComputedStyle(chip).visibility,
                    text: chip.textContent,
                  };
                  if (frame.__surfaceProgressTimer) clearTimeout(frame.__surfaceProgressTimer);
                  shell.remove();
                  delete window.__deferredFrameFixture;
                  return result;
                }"""
            )
            self.assertEqual(visible, {
                "progress": "visible",
                "chipVisibility": "visible",
                "text": "页面仍在准备中…",
            })
        finally:
            page.close()

    def test_update_monitor_tab_survives_refresh(self):
        page = self.new_page()
        try:
            page.goto(
                f"{self.base_url}/gms-update-monitor",
                wait_until="domcontentloaded",
            )
            page.wait_for_function("typeof setTab === 'function'")
            page.evaluate("setTab('artifacts')")
            expect(page.locator('[data-tab="artifacts"]')).to_have_class(
                re.compile(r"\bactive\b")
            )

            page.reload(wait_until="domcontentloaded")
            page.wait_for_function("typeof setTab === 'function'")

            expect(page.locator('[data-tab="artifacts"]')).to_have_class(
                re.compile(r"\bactive\b")
            )
            self.assertEqual(page.evaluate("tab"), "artifacts")
            self.assertEqual(
                page.evaluate("sessionStorage.getItem('gms_update_monitor_tab')"),
                "artifacts",
            )
            self.assertIn("tab=artifacts", page.url)
        finally:
            page.close()

    def test_async_refreshes_keep_rendered_content_until_atomic_replacement(self):
        page = self.new_page()
        page_errors = []
        page.on("pageerror", lambda exc: page_errors.append(str(exc)))
        try:
            self.goto_shell(page)
            page.wait_for_function(
                "typeof loadTestReports === 'function' "
                "&& typeof loadSecurityAudit === 'function' "
                "&& typeof loadSuiteBrowserDirectory === 'function'"
            )

            page.evaluate(
                """() => {
                  const tbody = document.querySelector('#reports-table-body');
                  tbody.innerHTML = '<tr><td colspan="10">old-report-surface</td></tr>';
                  reportsWorkersLoaded = true;
                  reportsHasLoaded = true;
                  reportsLoadedItems = [{timestamp: 'old-report-surface'}];
                  reportsLastQueryKey = reportsListUrl(false);
                  window.__originalRequestReportsData = requestReportsData;
                  requestReportsData = () => new Promise(resolve => {
                    window.__resolveStableReports = resolve;
                  });
                  window.__stableReportsPending = loadTestReports(false, false, true);
                }"""
            )
            expect(page.locator("#reports-table-body")).to_contain_text(
                "old-report-surface"
            )
            expect(page.locator("#reports-table-body")).to_have_attribute(
                "aria-busy", "true"
            )
            expect(page.locator("#reports-table-body")).not_to_contain_text(
                "正在加载报告"
            )
            # mock 的 resolve 句柄在 loadTestReports 真正发出请求时才赋值
            # （内部先 await GmsWorkspace.ready）；先等 mock 被调用，
            # 否则与页面异步初始化竞争。
            page.wait_for_function("typeof window.__resolveStableReports === 'function'")
            page.evaluate(
                """() => window.__resolveStableReports({
                  reports: [{
                    timestamp: 'new-report-surface', report_name: 'new-report-surface',
                    test_type: 'CTS', suite_path: '/tmp/android-cts-16_r1/tools',
                    pass: 1, fail: 0, total: 1, worker_id: 'ats-worker-controller'
                  }],
                  next_cursor: ''
                })"""
            )
            page.wait_for_function(
                "window.__stableReportsPending",
            )
            page.evaluate(
                """async () => {
                  await window.__stableReportsPending;
                  requestReportsData = window.__originalRequestReportsData;
                }"""
            )
            expect(page.locator("#reports-table-body")).to_contain_text(
                "new-report-surface"
            )
            expect(page.locator("#reports-table-body")).not_to_contain_text(
                "old-report-surface"
            )
            expect(page.locator("#reports-table-body")).to_have_attribute(
                "aria-busy", "false"
            )

            page.evaluate(
                """() => {
                  // loadSecurityAudit 对未提权会话短路为权限提示行；本段
                  // 验证刷新原子性，先补提权态（同 stale pagination 用例）。
                  state.elevated = true;
                  state.elevatedUntil = Date.now() + 60000;
                  const tbody = document.querySelector('#security-audit-table-body');
                  tbody.innerHTML = '<tr><td colspan="6">old-audit-surface</td></tr>';
                  securityAuditState.loaded = true;
                  securityAuditState.loading = false;
                  securityAuditState.recordsCache = [{id: 'old-audit-surface'}];
                  window.__originalApiCallForStableRefresh = apiCall;
                  apiCall = (...args) => String(args[0]).startsWith('/api/security-audit/logs')
                    ? new Promise(resolve => { window.__resolveStableAudit = resolve; })
                    : window.__originalApiCallForStableRefresh(...args);
                  window.__stableAuditPending = loadSecurityAudit(true);
                }"""
            )
            expect(page.locator("#security-audit-table-body")).to_contain_text(
                "old-audit-surface"
            )
            expect(page.locator("#security-audit-table-body")).to_have_attribute(
                "aria-busy", "true"
            )
            page.wait_for_function("typeof window.__resolveStableAudit === 'function'")
            page.evaluate(
                """() => window.__resolveStableAudit({data: {
                  records: [{
                    id: 'new-audit-surface', timestamp: '2026-08-12T12:00:00Z',
                    source: 'web', status_code: 200, username: 'new-audit-surface',
                    client_ip: '127.0.0.1', operation: 'page_view', duration_ms: 1
                  }],
                  has_more: false,
                  stats: {total: 1, web: 1, cli: 0, errors: 0}
                }})"""
            )
            page.evaluate(
                """async () => {
                  await window.__stableAuditPending;
                  apiCall = window.__originalApiCallForStableRefresh;
                }"""
            )
            expect(page.locator("#security-audit-table-body")).to_contain_text(
                "new-audit-surface"
            )
            expect(page.locator("#security-audit-table-body")).not_to_contain_text(
                "old-audit-surface"
            )

            page.evaluate(
                """() => {
                  const list = document.querySelector('#suite-file-list');
                  list.innerHTML = '<div class="suite-file-row">old-suite-surface</div>';
                  state.suiteBrowser.selectedSuitePath = '/tmp/android-cts-16_r1/tools';
                  state.suiteBrowser.currentPath = 'old';
                  state.suiteBrowser.suiteRoot = '/tmp/android-cts-16_r1';
                  testSuitesCache = [{
                    tools_path: '/tmp/android-cts-16_r1/tools',
                    test_type: 'cts', version: '16_r1'
                  }];
                  window.__originalApiCallForSuiteRefresh = apiCall;
                  apiCall = (...args) => String(args[0]).includes('/api/test/suites/files')
                    ? new Promise(resolve => { window.__resolveStableSuite = resolve; })
                    : window.__originalApiCallForSuiteRefresh(...args);
                  window.__stableSuitePending = loadSuiteBrowserDirectory('next');
                }"""
            )
            expect(page.locator("#suite-file-list")).to_contain_text(
                "old-suite-surface"
            )
            expect(page.locator("#suite-file-list")).to_have_attribute(
                "aria-busy", "true"
            )
            page.wait_for_function("typeof window.__resolveStableSuite === 'function'")
            page.evaluate(
                """() => window.__resolveStableSuite({data: {
                  path: 'next', suite_root: '/tmp/android-cts-16_r1',
                  items: [{name: 'new-suite-surface.txt', path: 'next/new-suite-surface.txt',
                    type: 'file', size: 12}]
                }})"""
            )
            page.evaluate(
                """async () => {
                  await window.__stableSuitePending;
                  apiCall = window.__originalApiCallForSuiteRefresh;
                }"""
            )
            expect(page.locator("#suite-file-list")).to_contain_text(
                "new-suite-surface.txt"
            )
            expect(page.locator("#suite-file-list")).not_to_contain_text(
                "old-suite-surface"
            )

            atomic_table = page.evaluate(
                """async () => {
                  const box = document.createElement('div');
                  box.dataset.loaded = 'true';
                  box.textContent = 'old-device-table-surface';
                  document.body.appendChild(box);
                  let blankSeen = false;
                  let mutations = 0;
                  const observer = new MutationObserver(() => {
                    mutations += 1;
                    if (!box.textContent.trim()) blankSeen = true;
                  });
                  observer.observe(box, {childList: true, subtree: false});
                  const rows = Array.from({length: 600}, (_, index) => index);
                  dcfgSetTable(
                    box,
                    '<colgroup><col></colgroup>',
                    '<th>value</th>',
                    rows,
                    value => `<tr><td>device-row-${value}</td></tr>`,
                    100
                  );
                  const immediate = box.textContent;
                  for (let index = 0; index < 8; index += 1) {
                    await new Promise(resolve => requestAnimationFrame(resolve));
                  }
                  await Promise.resolve();
                  observer.disconnect();
                  const result = {
                    immediate,
                    finalText: box.textContent,
                    blankSeen,
                    mutations,
                  };
                  box.remove();
                  return result;
                }"""
            )
            self.assertEqual(atomic_table["immediate"], "old-device-table-surface")
            self.assertIn("device-row-599", atomic_table["finalText"])
            self.assertFalse(atomic_table["blankSeen"], atomic_table)
            self.assertEqual(atomic_table["mutations"], 1, atomic_table)
            self.assert_no_page_errors(page_errors)
        finally:
            page.close()

    def test_stable_refreshes_do_not_reuse_stale_pagination_state(self):
        page = self.new_page()
        page_errors = []
        page.on("pageerror", lambda exc: page_errors.append(str(exc)))
        try:
            self.goto_shell(page)
            page.wait_for_function(
                "typeof loadTestReports === 'function' "
                "&& typeof loadSecurityAudit === 'function'"
            )

            reports_url = page.evaluate(
                """async () => {
                  const worker = document.querySelector('#reports-worker-filter');
                  reportsWorkersLoaded = true;
                  if (worker) worker.dataset.workersLoaded = 'true';
                  reportsHasLoaded = true;
                  reportsLoadedItems = [{timestamp: 'old-report'}];
                  reportsNextCursor = 'stale-cursor';
                  reportsLastQueryKey = reportsListUrl(false);
                  document.querySelector('#reports-table-body').innerHTML =
                    '<tr><td colspan="10">old-report</td></tr>';
                  const original = requestReportsData;
                  requestReportsData = url => {
                    window.__stablePaginationReportsUrl = url;
                    return Promise.resolve({reports: [], next_cursor: ''});
                  };
                  try {
                    await loadTestReports(true, true, true);
                    return window.__stablePaginationReportsUrl;
                  } finally {
                    requestReportsData = original;
                  }
                }"""
            )
            self.assertIn("user_only=true", reports_url)
            self.assertNotIn("cursor=", reports_url)

            audit_state = page.evaluate(
                """async () => {
                  // loadSecurityAudit 在未提权会话下短路为权限提示行；
                  // 本用例验证的是分页状态回滚语义，先补提权态。
                  state.elevated = true;
                  state.elevatedUntil = Date.now() + 60000;
                  const tbody = document.querySelector('#security-audit-table-body');
                  tbody.innerHTML = '<tr><td colspan="6">old-audit</td></tr>';
                  securityAuditState.loaded = true;
                  securityAuditState.loading = false;
                  securityAuditState.offset = 100;
                  securityAuditState.hasMore = true;
                  securityAuditState.currentFilterParams = getSecurityAuditFilterKey();
                  const original = apiCall;
                  apiCall = (...args) => String(args[0]).startsWith('/api/security-audit/logs')
                    ? Promise.reject(new Error('simulated audit failure'))
                    : original(...args);
                  const failed = await loadSecurityAudit(true);
                  const afterFailure = {
                    loaded: failed,
                    offset: securityAuditState.offset,
                    text: tbody.textContent,
                  };

                  document.querySelector('#audit-search-input').value = 'new-filter';
                  apiCall = (...args) => {
                    if (!String(args[0]).startsWith('/api/security-audit/logs')) {
                      return original(...args);
                    }
                    window.__stablePaginationAuditUrl = String(args[0]);
                    return Promise.resolve({data: {
                      records: [], has_more: false,
                      stats: {total: 0, web: 0, cli: 0, errors: 0},
                    }});
                  };
                  try {
                    await loadMoreSecurityAudit();
                    return {
                      afterFailure,
                      resetUrl: window.__stablePaginationAuditUrl,
                      finalOffset: securityAuditState.offset,
                    };
                  } finally {
                    apiCall = original;
                  }
                }"""
            )
            self.assertFalse(audit_state["afterFailure"]["loaded"])
            self.assertEqual(audit_state["afterFailure"]["offset"], 100)
            self.assertIn("old-audit", audit_state["afterFailure"]["text"])
            self.assertIn("offset=0", audit_state["resetUrl"])
            self.assertEqual(audit_state["finalOffset"], 0)
            self.assert_no_page_errors(page_errors)
        finally:
            page.close()

    def test_first_visit_defaults_to_test_page(self):
        page = self.new_page()
        page_errors = []
        page.on("pageerror", lambda exc: page_errors.append(str(exc)))
        try:
            page.goto(self.base_url, wait_until="load")
            page.wait_for_selector(".sidebar-item[data-page]")
            self.close_initial_modals(page)
            expect(page.locator("#page-test")).to_have_class(re.compile(r"active"))
            expect(page.locator('.sidebar-item[data-page="test"]')).to_have_class(re.compile(r"active"))
            self.assertEqual(page.evaluate("localStorage.getItem('gms_current_page')"), "test")
            self.assert_no_page_errors(page_errors)
        finally:
            page.close()

    def test_saved_users_page_restores_auto_refresh_on_load(self):
        page = self.new_page()
        page_errors = []
        page.on("pageerror", lambda exc: page_errors.append(str(exc)))
        try:
            page.route(
                "**/api/users/list",
                lambda route: route.fulfill(
                    status=200,
                    content_type="application/json",
                    body='{"users":[]}',
                ),
            )
            page.add_init_script(
                """
                localStorage.setItem('gms_current_page', 'users');
                window.__usersAutoRefreshIntervals = [];
                const originalSetInterval = window.setInterval.bind(window);
                window.setInterval = (handler, delay, ...args) => {
                  const id = originalSetInterval(handler, delay, ...args);
                  const source = Function.prototype.toString.call(handler);
                  if (delay === 10000 && source.includes('loadUsersList')) {
                    window.__usersAutoRefreshIntervals.push(delay);
                  }
                  return id;
                };
                """
            )
            page.goto(self.base_url, wait_until="load")
            page.wait_for_selector(".sidebar-item[data-page]")
            self.close_initial_modals(page)
            page.wait_for_function("typeof window.switchPage === 'function'")
            expect(page.locator("#page-users")).to_have_class(re.compile(r"active"))
            page.wait_for_function(
                "window.__usersAutoRefreshIntervals && window.__usersAutoRefreshIntervals.length > 0"
            )
            self.assert_no_page_errors(page_errors)
        finally:
            page.close()

    def test_saved_architecture_page_sets_title_before_load_and_lazy_loads_frame(self):
        page = self.new_page()
        page_errors = []
        page.on("pageerror", lambda exc: page_errors.append(str(exc)))
        try:
            page.context.add_cookies([
                {
                    "name": "gms_current_page",
                    "value": "architecture",
                    "url": self.base_url,
                }
            ])
            page.add_init_script(
                """
                localStorage.setItem('gms_current_page', 'architecture');
                """
            )
            page.goto(self.base_url, wait_until="domcontentloaded")
            self.close_initial_modals(page)
            self.assertEqual(page.title(), "系统架构 - GMS远程测试")
            page.wait_for_function("typeof window.switchPage === 'function'")
            expect(page.locator("#page-architecture")).to_have_class(re.compile(r"active"))
            expect(page.locator("#architecture-iframe")).to_have_attribute("src", re.compile(r"/templates/architecture\.html"))
            self.assert_no_page_errors(page_errors)
        finally:
            page.close()

    def test_saved_page_cookie_sets_initial_html_title(self):
        page = self.new_page()
        try:
            page.context.add_cookies([
                {
                    "name": "gms_current_page",
                    "value": "devices",
                    "url": self.base_url,
                }
            ])
            page.goto(self.base_url, wait_until="commit")

            self.assertEqual(page.title(), "设备管理 - GMS远程测试")
        finally:
            page.close()

    def test_architecture_template_uses_local_fonts_only(self):
        html = (REPO_ROOT / "web" / "templates" / "architecture.html").read_text(encoding="utf-8")

        self.assertNotIn("fonts.font.im", html)
        self.assertNotIn("fonts.googleapis.com", html)

    def test_arrow_key_navigation_persists_page_for_reload(self):
        page = self.new_page()
        page_errors = []
        page.on("pageerror", lambda exc: page_errors.append(str(exc)))
        try:
            self.goto_shell(page)
            page.wait_for_function("typeof window.switchPage === 'function'")
            expect(page.locator("#page-test")).to_have_class(re.compile(r"active"))

            page.evaluate("document.activeElement && document.activeElement.blur()")
            page.keyboard.press("ArrowDown")
            expect(page.locator("#page-desktop")).to_have_class(re.compile(r"active"))
            self.assertEqual(page.title(), "主机桌面 - GMS远程测试")
            self.assertEqual(page.evaluate("localStorage.getItem('gms_current_page')"), "desktop")
            self.assertIn("gms_current_page=desktop", page.evaluate("document.cookie"))

            page.reload(wait_until="domcontentloaded")
            self.assertEqual(page.title(), "主机桌面 - GMS远程测试")
            page.wait_for_load_state("load")
            page.wait_for_function("typeof window.switchPage === 'function'")
            expect(page.locator("#page-desktop")).to_have_class(re.compile(r"active"))
            self.assertEqual(page.evaluate("localStorage.getItem('gms_current_page')"), "desktop")
            self.assert_no_page_errors(page_errors)
        finally:
            page.close()

    def test_devices_aoa_and_users_agent_tabs_persist_across_reload(self):
        page = self.new_page()
        page_errors = []
        page.on("pageerror", lambda exc: page_errors.append(str(exc)))
        try:
            self.goto_shell(page)
            page.wait_for_function("typeof window.switchPage === 'function'")

            page.evaluate("switchPage('devices')")
            page.evaluate("aoaViewToggle(true)")
            expect(page.locator("#aoa-panel")).to_be_visible()
            # 当前视图页签高亮：active 态与可见面板一一对应。
            self.assertEqual(
                page.evaluate(
                    "['devices-list-tab','aoa-tab']"
                    ".map(id => document.getElementById(id).classList.contains('active'))"),
                [False, True])
            page.reload(wait_until="domcontentloaded")
            page.wait_for_load_state("load")
            page.wait_for_function("typeof window.switchPage === 'function'")
            expect(page.locator("#aoa-panel")).to_be_visible()
            self.assertEqual(
                page.evaluate("localStorage.getItem('gms_current_page')"), "devices")

            page.evaluate("switchPage('users')")
            self.close_initial_modals(page)
            page.evaluate("agentAccessToggle(true)")
            expect(page.locator("#agent-access-panel")).to_be_visible()
            # 当前页签控件高亮：active 态与可见面板一一对应。
            self.assertEqual(
                page.evaluate(
                    "['users-list-tab','agent-access-tab','sdk-config-tab']"
                    ".map(id => document.getElementById(id).classList.contains('active'))"),
                [False, True, False])
            page.reload(wait_until="domcontentloaded")
            page.wait_for_load_state("load")
            page.wait_for_function("typeof window.switchPage === 'function'")
            expect(page.locator("#agent-access-panel")).to_be_visible()

            # SDK 配置页签：切换与 F5 恢复，与 Agent 接入同机制。
            page.evaluate("sdkConfigToggle(true)")
            expect(page.locator("#sdk-config-panel")).to_be_visible()
            expect(page.locator("#agent-access-panel")).not_to_be_visible()
            self.assertEqual(
                page.evaluate(
                    "['users-list-tab','agent-access-tab','sdk-config-tab']"
                    ".map(id => document.getElementById(id).classList.contains('active'))"),
                [False, False, True])
            page.reload(wait_until="domcontentloaded")
            page.wait_for_load_state("load")
            page.wait_for_function("typeof window.switchPage === 'function'")
            expect(page.locator("#sdk-config-panel")).to_be_visible()
            self.assert_no_page_errors(page_errors)
        finally:
            page.close()

    def test_sidebar_visibility_options_each_have_an_explanation(self):
        page = self.new_page()
        try:
            self.goto_shell(page)
            page.locator(".sidebar-brand").click()
            options = page.locator("#sidebar-visibility-list .sidebar-visibility-option")
            descriptions = page.locator("#sidebar-visibility-list .sidebar-description")
            self.assertGreater(options.count(), 0)
            self.assertEqual(descriptions.count(), options.count())
            for index in range(descriptions.count()):
                self.assertTrue(descriptions.nth(index).inner_text().strip())
                self.assertEqual(
                    descriptions.nth(index).evaluate(
                        "element => getComputedStyle(element).whiteSpace"
                    ),
                    "nowrap",
                )
            first_icon = options.first.locator(".sidebar-icon").bounding_box()
            first_title = options.first.locator(".sidebar-text").bounding_box()
            first_description = descriptions.first.bounding_box()
            self.assertAlmostEqual(first_icon["y"], first_title["y"], delta=4)
            self.assertAlmostEqual(first_title["y"], first_description["y"], delta=5)
        finally:
            page.close()

    def test_sidebar_settings_project_guide_is_accessible(self):
        page = self.new_page()
        page_errors = []
        page.on("pageerror", lambda exc: page_errors.append(str(exc)))
        try:
            self.goto_shell(page)
            page.locator(".sidebar-brand").click()
            page.locator('[data-sidebar-settings-tab="guide"]').click()

            guide = page.locator("#sidebar-settings-panel-guide")
            expect(guide).to_be_visible()
            expect(guide).to_contain_text("5 步快速开始测试")
            expect(page.locator("#project-guide-url")).to_have_attribute(
                "href", f"{self.base_url}/"
            )
            expect(page.locator("#project-guide-url")).to_contain_text(
                f"{self.base_url}/"
            )
            guide_images = page.locator("#sidebar-settings-panel-guide .project-guide-image-button img")
            expect(guide_images).to_have_count(13)
            for image_index in range(guide_images.count()):
                guide_images.nth(image_index).scroll_into_view_if_needed()
                expect(guide_images.nth(image_index)).to_have_js_property("naturalWidth", 1600)

            page.locator(".project-guide-image-button").first.click()
            image_modal = page.locator("#guide-image-modal")
            expect(image_modal).to_have_class(re.compile(r"\bshow\b"))
            expect(page.locator("#guide-image-title")).to_have_text("测试实例：类型、套件、模块与用例")
            page.locator("#guide-image-zoom-btn").click()
            expect(page.locator("#guide-image-preview")).to_have_class(re.compile(r"\bactual-size\b"))
            page.keyboard.press("Escape")
            expect(image_modal).not_to_have_class(re.compile(r"\bshow\b"))
            expect(guide).to_be_visible()

            page.locator('[data-sidebar-settings-tab="visibility"]').click()
            expect(page.locator("#sidebar-settings-panel-visibility")).to_be_visible()
            expect(guide).to_be_hidden()
            self.assert_no_page_errors(page_errors)
        finally:
            page.close()

def test_vpn_proxy_installer_single_line_markup_contract():
    from pathlib import Path
    root = Path(__file__).resolve().parents[2] / 'web' / 'shell' / 'dialogs'
    html = (root / 'vpn-proxy.html').read_text(encoding='utf-8')
    assert html.count('class="vpn-proxy-install-row"') == 2
    assert '<textarea id="vpn-proxy-linux-command"' not in html
    assert 'data-click="copyVpnProxyLinux"' in html
    assert 'data-click="copyVpnProxyWindows"' in html
