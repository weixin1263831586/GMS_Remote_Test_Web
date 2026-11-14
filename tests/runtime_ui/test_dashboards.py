"""Embedded dashboards, refresh lifecycle and shell integration."""

import json
import re

from tests.runtime_ui.harness import RuntimeUiHarness, expect


class RuntimeDashboardsTests(RuntimeUiHarness):
    def test_redmine_daily_brief_analysis_button_opens_modal(self):
        page = self.new_page()

        def fulfill_redmine(route):
            route.fulfill(
                status=200,
                content_type="application/json",
                body='{"success":true,"data":{}}',
            )

        page.route("**/api/redmine-agent/**", fulfill_redmine)
        page_errors = []
        page.on("pageerror", lambda exc: page_errors.append(str(exc)))
        try:
            page.goto(f"{self.base_url}/redmine-agent", wait_until="domcontentloaded")
            page.wait_for_function("typeof renderDailyBriefInner === 'function'")
            page.evaluate("switchTab('daily-brief')")
            page.evaluate(
                """() => {
                    dailyBriefCache = {
                        run: {brief_date: '2026-09-15', status: 'completed', report_json: {}},
                        issues: [{
                            issue_id: 101,
                            status: 'completed',
                            subject: 'Legacy daily brief modal',
                            result: {
                                problem_summary: '分析摘要',
                                evidence: '旧版取证文本',
                                recommended_actions: {action: '复查日志'},
                                similar_issues: {issue_id: 100},
                                missing_information: '完整日志'
                            }
                        }]
                    };
                    document.getElementById('dailyBriefCard').innerHTML = renderDailyBriefInner(dailyBriefCache);
                }"""
            )
            page.locator('#dailyBriefCard [data-click="showDailyBriefIssue"]').click()
            # 「查看分析」已与单号分析统一为 singleIssueAnalysisModal 布局
            # （openSingleIssueReportModal）；dailyBriefIssueModal- 前缀仅
            # 剩旧格式 fallback 路径会创建。
            expect(page.locator('[id^="singleIssueAnalysisModal-"]')).to_have_class(
                re.compile(r"show")
            )
            page.keyboard.press("Escape")
            expect(page.locator('[id^="singleIssueAnalysisModal-"]')).not_to_have_class(
                re.compile(r"show")
            )
            self.assert_no_page_errors(page_errors)
        finally:
            page.close()

    def test_redmine_case_view_survives_refresh(self):
        page = self.new_page()
        try:
            page.goto(f"{self.base_url}/redmine-agent", wait_until="domcontentloaded")
            page.wait_for_function("typeof switchCaseView === 'function'")
            page.evaluate("switchTab('cases'); switchCaseView('cases')")
            expect(page.locator("#viewBtnCases")).to_have_class(
                re.compile(r"\bactive\b")
            )

            page.reload(wait_until="domcontentloaded")
            page.wait_for_function("currentTab === 'cases' && caseView === 'cases'")

            expect(page.locator('[data-tab="cases"]')).to_have_class(
                re.compile(r"\bactive\b")
            )
            expect(page.locator("#viewBtnCases")).to_have_class(
                re.compile(r"\bactive\b")
            )
            self.assertEqual(
                page.evaluate(
                    "({top: currentTab, inner: caseView, saved: "
                    "sessionStorage.getItem('redmineCaseView')})"
                ),
                {"top": "cases", "inner": "cases", "saved": "cases"},
            )
            self.assertIn("tab=cases", page.url)
            self.assertIn("case_view=cases", page.url)
        finally:
            page.close()

    def test_gerrit_dashboard_tab_survives_refresh(self):
        page = self.new_page()
        try:
            page.goto(f"{self.base_url}/gerrit-dashboard", wait_until="domcontentloaded")
            page.wait_for_function("typeof switchTab === 'function'")
            page.evaluate("switchTab('query')")
            expect(page.locator('[data-tab="query"]')).to_have_class(
                re.compile(r"\bactive\b")
            )

            page.reload(wait_until="domcontentloaded")
            page.wait_for_function("currentTab === 'query'")

            expect(page.locator('[data-tab="query"]')).to_have_class(
                re.compile(r"\bactive\b")
            )
            expect(page.locator("#tab-query")).to_have_class(re.compile(r"\bactive\b"))
            self.assertEqual(
                page.evaluate("sessionStorage.getItem('gerritCurrentTab')"),
                "query",
            )
            self.assertIn("tab=query", page.url)
        finally:
            page.close()

    def test_hidden_embedded_pages_pause_background_refresh(self):
        page = self.new_page()
        try:
            self.goto_shell(page)
            frames = [
                ("cluster", "#cluster-frame", "clusterRefreshInterval"),
                ("automation", "#automation-frame", "automationRefreshInterval"),
                ("redmine-agent", "#redmine-agent-frame", "redmineStatusRefreshInterval"),
            ]
            for page_name, selector, timer_name in frames:
                with self.subTest(page=page_name):
                    page.evaluate("name => switchPage(name, null)", page_name)
                    page.wait_for_function(
                        "selector => document.querySelector(selector).closest("
                        "'.embedded-frame-shell').dataset.frameState === 'ready'",
                        arg=selector,
                    )
                    frame = self.frame_for(page, selector)
                    page.wait_for_function(
                        "selector => document.querySelector(selector).contentWindow."
                        "GmsEmbeddedWorkspace?.isVisible() === true",
                        arg=selector,
                    )
                    self.assertTrue(frame.evaluate(f"Boolean({timer_name})"))

                    page.evaluate("switchPage('test', null)")
                    page.wait_for_function(
                        "selector => document.querySelector(selector).contentWindow."
                        "GmsEmbeddedWorkspace?.isVisible() === false",
                        arg=selector,
                    )
                    self.assertFalse(frame.evaluate(f"Boolean({timer_name})"))

                    page.evaluate("name => switchPage(name, null)", page_name)
                    page.wait_for_function(
                        "selector => document.querySelector(selector).contentWindow."
                        "GmsEmbeddedWorkspace?.isVisible() === true",
                        arg=selector,
                    )
                    self.assertTrue(frame.evaluate(f"Boolean({timer_name})"))
        finally:
            page.close()

    def test_non_embedded_page_refresh_timers_stop_after_navigation(self):
        page = self.new_page()
        try:
            self.goto_shell(page)
            timers = [
                ("users", "usersRefreshInterval"),
                ("devices", "devicesRefreshInterval"),
                ("reports", "reportsRefreshInterval"),
            ]
            for page_name, timer_name in timers:
                with self.subTest(page=page_name):
                    page.evaluate("name => switchPage(name, null)", page_name)
                    page.wait_for_function(f"Boolean({timer_name})")
                    page.evaluate("switchPage('test', null)")
                    page.wait_for_function(f"!{timer_name}")
        finally:
            page.close()

    def test_embedded_dashboards_avoid_initial_shift_and_horizontal_leaks(self):
        page = self.new_page()
        try:
            page.add_init_script(
                """
                window.__initialLayoutShifts = [];
                try {
                  new PerformanceObserver(list => {
                    for (const entry of list.getEntries()) {
                      if (!entry.hadRecentInput) {
                        window.__initialLayoutShifts.push(entry.value);
                      }
                    }
                  }).observe({type: 'layout-shift', buffered: true});
                } catch (_error) {}
                """
            )
            self.goto_shell(page)
            page.evaluate("switchPage('cluster', null)")
            page.wait_for_function(
                "document.querySelector('#cluster-frame').closest("
                "'.embedded-frame-shell').dataset.frameState === 'ready'"
            )
            cluster = self.frame_for(page, "#cluster-frame")
            cluster.wait_for_timeout(1200)
            cluster_layout = cluster.evaluate(
                """() => ({
                  score: (window.__initialLayoutShifts || [])
                    .reduce((sum, value) => sum + value, 0),
                  cardCount: document.querySelectorAll(
                    '#dashboard-stats .dash-stat-card'
                  ).length,
                  busy: document.querySelector('#dashboard-stats')
                    .getAttribute('aria-busy'),
                })"""
            )
            self.assertLess(cluster_layout["score"], 0.001, cluster_layout)
            self.assertEqual(cluster_layout["cardCount"], 5, cluster_layout)
            self.assertEqual(cluster_layout["busy"], "false", cluster_layout)

            page.evaluate("switchPage('redmine-agent', null)")
            page.wait_for_function(
                "document.querySelector('#redmine-agent-frame').closest("
                "'.embedded-frame-shell').dataset.frameState === 'ready'"
            )
            redmine = self.frame_for(page, "#redmine-agent-frame")
            redmine.wait_for_timeout(600)
            for viewport in [
                {"width": 1440, "height": 960},
                {"width": 960, "height": 720},
            ]:
                with self.subTest(viewport=viewport):
                    page.set_viewport_size(viewport)
                    page.wait_for_timeout(120)
                    widths = redmine.evaluate(
                        """() => ({
                          viewport: innerWidth,
                          body: document.body.scrollWidth,
                          html: document.documentElement.scrollWidth,
                          header: document.querySelector('header').scrollWidth,
                        })"""
                    )
                    self.assertLessEqual(widths["body"], widths["viewport"] + 1, widths)
                    self.assertLessEqual(widths["html"], widths["viewport"] + 1, widths)
                    self.assertLessEqual(widths["header"], widths["viewport"] + 1, widths)
        finally:
            page.close()

    def test_redmine_and_gerrit_refresh_failures_preserve_current_dashboards(self):
        page = self.new_page()
        page_errors = []
        page.on("pageerror", lambda exc: page_errors.append(str(exc)))
        try:
            page.goto(f"{self.base_url}/redmine-agent", wait_until="domcontentloaded")
            page.wait_for_function("typeof loadStatistics === 'function'")
            page.evaluate(
                """() => {
                  const content = document.querySelector('#statsContent');
                  content.innerHTML = '<section>old-redmine-dashboard</section>';
                  content.dataset.loaded = 'true';
                  _statsConfigCacheTs = Date.now();
                  statsConfig.stale_days = 3;
                  window.__originalRedmineApiForStableRefresh = api;
                  api = url => String(url).includes('/statistics/workload')
                    ? new Promise((resolve, reject) => {
                        window.__rejectStableRedmine = reject;
                      })
                    : String(url).endsWith('/statistics')
                      ? Promise.resolve({})
                      : window.__originalRedmineApiForStableRefresh(url);
                  window.__stableRedminePending = loadStatistics(true);
                }"""
            )
            expect(page.locator("#statsContent")).to_contain_text(
                "old-redmine-dashboard"
            )
            expect(page.locator("#statsContent")).to_have_attribute(
                "aria-busy", "true"
            )
            page.evaluate(
                "window.__rejectStableRedmine(new Error('simulated refresh failure'))"
            )
            page.evaluate(
                """async () => {
                  await window.__stableRedminePending;
                  api = window.__originalRedmineApiForStableRefresh;
                }"""
            )
            expect(page.locator("#statsContent")).to_contain_text(
                "old-redmine-dashboard"
            )
            expect(page.locator("#statsContent")).to_have_attribute(
                "aria-busy", "false"
            )

            page.goto(f"{self.base_url}/gerrit-dashboard", wait_until="domcontentloaded")
            page.wait_for_function("typeof loadPersonal === 'function'")
            page.evaluate(
                """() => {
                  const content = document.querySelector('#personalContent');
                  content.innerHTML = '<section>old-gerrit-dashboard</section>';
                  content.dataset.loaded = 'true';
                  currentTab = 'personal';
                  config.default_owner = 'ui-admin@example.com';
                  window.__originalGerritApiForStableRefresh = api;
                  api = url => String(url).includes('/statistics/personal')
                    ? new Promise((resolve, reject) => {
                        window.__rejectStableGerrit = reject;
                      })
                    : window.__originalGerritApiForStableRefresh(url);
                  window.__stableGerritPending = loadPersonal(true);
                }"""
            )
            expect(page.locator("#personalContent")).to_contain_text(
                "old-gerrit-dashboard"
            )
            expect(page.locator("#personalContent")).to_have_attribute(
                "aria-busy", "true"
            )
            page.evaluate(
                "window.__rejectStableGerrit(new Error('simulated refresh failure'))"
            )
            page.evaluate(
                """async () => {
                  await window.__stableGerritPending;
                  api = window.__originalGerritApiForStableRefresh;
                }"""
            )
            expect(page.locator("#personalContent")).to_contain_text(
                "old-gerrit-dashboard"
            )
            expect(page.locator("#personalContent")).to_have_attribute(
                "aria-busy", "false"
            )
            self.assert_no_page_errors(page_errors)
        finally:
            page.close()

    def test_sidebar_arrow_navigation_focuses_embedded_active_tab(self):
        page = self.new_page()
        page_errors = []
        page.on("pageerror", lambda exc: page_errors.append(str(exc)))
        try:
            self.goto_shell(page)
            page.wait_for_function("typeof window.switchPage === 'function'")
            # 保持用户实际的侧栏排序：从 Gerrit 的真实上一个可见页面按
            # Down 到 Gerrit，验证完整跨 iframe 焦点链路。
            previous_page = page.evaluate(
                """() => {
                    const pages = Array.from(document.querySelectorAll('.sidebar-item'))
                        .filter(item => item.style.display !== 'none')
                        .map(item => item.dataset.page);
                    const index = pages.indexOf('gerrit-dashboard');
                    if (index === -1) throw new Error('Gerrit sidebar item is hidden');
                    return pages[(index - 1 + pages.length) % pages.length];
                }"""
            )
            page.evaluate("target => switchPage(target, null)", previous_page)
            page.wait_for_function(f"currentPage === {json.dumps(previous_page)}")
            page.locator("body").focus()
            page.keyboard.press("ArrowDown")
            page.wait_for_function("currentPage === 'gerrit-dashboard'")

            gerrit = self.frame_for(page, "#gerrit-dashboard-frame")
            gerrit.wait_for_function(
                """() => document.activeElement?.matches(
                    '[role=tab][aria-selected="true"]:not([disabled])'
                )"""
            )
            gerrit.locator('[role="tab"][aria-selected="true"]').press("ArrowRight")
            expect(gerrit.locator('[data-tab="query"]')).to_have_attribute(
                "aria-selected", "true"
            )
            # 离开 Tab iframe 到普通页面时，焦点必须归还给 Shell；随后
            # 无需点击即可继续上下切换，不能残留在已隐藏的 Gerrit iframe。
            next_page = page.evaluate(
                """() => {
                    const pages = Array.from(document.querySelectorAll('.sidebar-item'))
                        .filter(item => item.style.display !== 'none')
                        .map(item => item.dataset.page);
                    return pages[(pages.indexOf(currentPage) + 1) % pages.length];
                }"""
            )
            gerrit.locator('[data-tab="query"]').press("ArrowDown")
            page.wait_for_function(f"currentPage === {json.dumps(next_page)}")
            self.assertEqual(
                page.evaluate("document.activeElement.id"), f"page-{next_page}"
            )
            following_page = page.evaluate(
                """() => {
                    const pages = Array.from(document.querySelectorAll('.sidebar-item'))
                        .filter(item => item.style.display !== 'none')
                        .map(item => item.dataset.page);
                    return pages[(pages.indexOf(currentPage) + 1) % pages.length];
                }"""
            )
            page.keyboard.press("ArrowDown")
            page.wait_for_function(f"currentPage === {json.dumps(following_page)}")
            self.assert_no_page_errors(page_errors)
        finally:
            page.close()

    def test_cluster_dashboard_half_screen_panels_do_not_overlap(self):
        page = self.new_page()
        try:
            page.goto(f"{self.base_url}/cluster", wait_until="domcontentloaded")
            page.wait_for_function(
                "document.querySelectorAll('#dashboard-stats .dash-stat-card').length === 5"
            )

            for viewport in [
                {"width": 960, "height": 720},
                {"width": 760, "height": 720},
            ]:
                with self.subTest(viewport=viewport):
                    page.set_viewport_size(viewport)
                    page.wait_for_timeout(150)
                    report = page.evaluate(
                        """() => {
                          const visible = element => {
                            const style = getComputedStyle(element);
                            return style.display !== 'none'
                              && style.visibility !== 'hidden'
                              && element.getClientRects().length > 0;
                          };
                          const rect = element => {
                            const value = element.getBoundingClientRect();
                            return {
                              id: element.id || String(element.className || ''),
                              left: value.left, right: value.right,
                              top: value.top, bottom: value.bottom,
                              width: value.width, height: value.height,
                            };
                          };
                          const intersections = selector => {
                            const items = Array.from(document.querySelectorAll(selector))
                              .filter(visible).map(rect);
                            const overlaps = [];
                            for (let left = 0; left < items.length; left += 1) {
                              for (let right = left + 1; right < items.length; right += 1) {
                                const a = items[left], b = items[right];
                                const width = Math.min(a.right, b.right) - Math.max(a.left, b.left);
                                const height = Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top);
                                if (width > 1 && height > 1) overlaps.push([a, b]);
                              }
                            }
                            return overlaps;
                          };
                          const panels = Array.from(document.querySelectorAll('.dash-panel'))
                            .filter(visible).map(rect);
                          const pane = document.querySelector('#tab-dashboard');
                          return {
                            cardOverlaps: intersections('.dash-stat-card'),
                            panelOverlaps: intersections('.dash-panel'),
                            panels,
                            paneOverflowY: getComputedStyle(pane).overflowY,
                            documentWidth: document.documentElement.scrollWidth,
                            viewportWidth: innerWidth,
                          };
                        }"""
                    )
                    self.assertEqual(report["cardOverlaps"], [], report)
                    self.assertEqual(report["panelOverlaps"], [], report)
                    self.assertEqual(len(report["panels"]), 4, report)
                    self.assertTrue(
                        all(panel["height"] >= 239 for panel in report["panels"]),
                        report,
                    )
                    self.assertEqual(report["paneOverflowY"], "auto", report)
                    self.assertLessEqual(
                        report["documentWidth"], report["viewportWidth"] + 1, report
                    )

            resize_count = page.evaluate(
                """async () => {
                  window.__dashResizeCount = 0;
                  dashCharts.__test = {
                    isDisposed: () => false,
                    resize: () => { window.__dashResizeCount += 1; },
                  };
                  scheduleDashboardChartsResize();
                  await new Promise(resolve => requestAnimationFrame(() => resolve()));
                  return window.__dashResizeCount;
                }"""
            )
            self.assertGreaterEqual(resize_count, 1)
            page.set_viewport_size({"width": 820, "height": 720})
            page.wait_for_function("window.__dashResizeCount >= 2")
            page.evaluate("delete dashCharts.__test")
        finally:
            page.close()

    def test_embedded_dashboard_notification_bridge_reaches_main_shell(self):
        page = self.new_page()
        try:
            self.goto_shell(page)
            # Navigate through the shell API so unrelated first-run modals do
            # not intercept the sidebar pointer action in isolated runs.
            page.evaluate("switchPage('gerrit-dashboard', null)")
            gerrit = self.frame_for(page, "#gerrit-dashboard-frame")
            # frame_for may briefly observe the lazy iframe's initial
            # about:blank document (origin "null") under a busy full-suite
            # run; wait for the real same-origin dashboard before posting.
            gerrit.wait_for_function("window.location.origin !== 'null'")
            gerrit.evaluate(
                """
                window.parent.postMessage({
                    type: 'gms-dashboard-notification',
                    title: '运行时通知测试',
                    message: 'iframe bridge ok',
                    level: 'success'
                }, window.location.origin);
                """
            )
            expect(page.locator(".notification-badge")).to_be_visible()
        finally:
            page.close()

    def test_redmine_and_gerrit_iframe_modals_close_with_escape(self):
        page = self.new_page()
        try:
            self.goto_shell(page)

            page.locator('.sidebar-item[data-page="redmine-agent"]').click()
            redmine = self.frame_for(page, "#redmine-agent-frame")
            redmine.wait_for_function("typeof showSettingsModal === 'function'")
            redmine.evaluate("showSettingsModal()")
            expect(redmine.locator("#settingsModal")).to_have_class(re.compile(r"show"))
            redmine.wait_for_function(
                "document.activeElement?.id === 'settingStaleDays'"
            )
            redmine.evaluate("document.dispatchEvent(new KeyboardEvent('keydown', {key:'Escape'}))")
            expect(redmine.locator("#settingsModal")).not_to_have_class(re.compile(r"show"))

            page.locator('.sidebar-item[data-page="gerrit-dashboard"]').click()
            gerrit = self.frame_for(page, "#gerrit-dashboard-frame")
            gerrit.wait_for_function("typeof showSettings === 'function'")
            gerrit.evaluate("showSettings()")
            expect(gerrit.locator("#settingsModal")).to_have_class(re.compile(r"show"))
            gerrit.wait_for_function(
                "document.activeElement?.id === 'settingBaseUrl'"
            )
            gerrit.evaluate("document.dispatchEvent(new KeyboardEvent('keydown', {key:'Escape'}))")
            expect(gerrit.locator("#settingsModal")).not_to_have_class(re.compile(r"show"))
        finally:
            page.close()

    def test_iframe_notify_user_reaches_main_shell_from_real_frames(self):
        page = self.new_page()
        try:
            self.goto_shell(page)
            page.locator('.sidebar-item[data-page="redmine-agent"]').click()
            redmine = self.frame_for(page, "#redmine-agent-frame")
            redmine.wait_for_function("typeof notifyUser === 'function'")
            redmine.evaluate("notifyUser('Redmine iframe 通知测试', 'ok', 'success')")
            expect(page.locator(".notification-badge")).to_be_visible()

            page.locator('.sidebar-item[data-page="gerrit-dashboard"]').click()
            gerrit = self.frame_for(page, "#gerrit-dashboard-frame")
            gerrit.wait_for_function("typeof notifyUser === 'function'")
            gerrit.evaluate("notifyUser('Gerrit iframe 通知测试', 'ok', 'success')")
            expect(page.locator(".notification-badge")).to_be_visible()
        finally:
            page.close()

    def test_redmine_dashboard_safe_controls_and_modals(self):
        page = self.new_page()
        def fulfill_redmine(route):
            route.fulfill(
                status=200,
                content_type="application/json",
                body='{"success":true,"running":false,"last_result":{},"data":{},"items":[]}',
            )

        page.route("**/api/redmine/**", fulfill_redmine)
        page.route("**/api/redmine-agent/**", fulfill_redmine)
        page_errors = []
        page.on("pageerror", lambda exc: page_errors.append(str(exc)))
        try:
            self.goto_shell(page)
            page.locator('.sidebar-item[data-page="redmine-agent"]').click()
            redmine = self.frame_for(page, "#redmine-agent-frame")
            redmine.wait_for_load_state('domcontentloaded')
            redmine.wait_for_function("typeof switchTab === 'function'")

            for tab_name in ["department", "stats", "project", "issues", "runs"]:
                redmine.evaluate("tab => switchTab(tab)", tab_name)
                expect(redmine.locator(f'.tab[data-tab="{tab_name}"]')).to_have_class(re.compile(r"active"))

            redmine.locator('.tab[data-tab="stats"]').focus()
            redmine.locator('.tab[data-tab="stats"]').press("ArrowRight")
            expect(redmine.locator('.tab[data-tab="project"]')).to_have_class(re.compile(r"active"))
            redmine.locator('.tab[data-tab="project"]').press("End")
            expect(redmine.locator('.tab[data-tab="runs"]')).to_have_class(re.compile(r"active"))
            redmine.locator('.tab[data-tab="runs"]').press("Home")
            expect(redmine.locator('.tab[data-tab="department"]')).to_have_class(re.compile(r"active"))

            self.assert_frame_modal_closes_with_escape(redmine, "showSettingsModal()", "#settingsModal")
            self.assert_frame_modal_closes_with_escape(redmine, "showAddUserModal()", "#addUserModal")
            self.assert_frame_modal_closes_with_escape(redmine, "showAddDepartmentModal()", "#addDepartmentModal")
            self.assert_frame_modal_closes_with_escape(redmine, "showAddProjectModal()", "#addProjectModal")

            redmine.wait_for_function("typeof setTrendStartDate === 'function'")
            redmine.evaluate("setTrendStartDate('daily', '每日')")
            expect(redmine.locator("#trendStartModal")).to_have_class(re.compile(r"show"))
            self.press_escape_in_frame(redmine)
            expect(redmine.locator("#trendStartModal")).not_to_have_class(re.compile(r"show"))

            redmine.evaluate("refreshCurrentTab()")
            self.assert_no_page_errors(page_errors)
        finally:
            page.close()

    def test_gerrit_dashboard_safe_controls_and_modals(self):
        page = self.new_page()
        page_errors = []
        page.on("pageerror", lambda exc: page_errors.append(str(exc)))
        try:
            self.goto_shell(page)
            page.locator('.sidebar-item[data-page="gerrit-dashboard"]').click()
            gerrit = self.frame_for(page, "#gerrit-dashboard-frame")
            gerrit.wait_for_function("typeof switchTab === 'function'")

            for tab_name in ["personal", "department", "query"]:
                gerrit.evaluate("tab => switchTab(tab)", tab_name)
                expect(gerrit.locator(f'.tab[data-tab="{tab_name}"]')).to_have_class(re.compile(r"active"))

            gerrit.locator('.tab[data-tab="personal"]').focus()
            gerrit.locator('.tab[data-tab="personal"]').press("ArrowRight")
            expect(gerrit.locator('.tab[data-tab="query"]')).to_have_class(re.compile(r"active"))
            gerrit.locator('.tab[data-tab="query"]').press("Home")
            expect(gerrit.locator('.tab[data-tab="department"]')).to_have_class(re.compile(r"active"))

            self.assert_frame_modal_closes_with_escape(gerrit, "showSettings()", "#settingsModal")
            self.assert_frame_modal_closes_with_escape(gerrit, "showAddPersonalModal()", "#addPersonalModal")
            self.assert_frame_modal_closes_with_escape(gerrit, "showAddDepartmentModal()", "#addDepartmentModal")
            self.assert_frame_modal_closes_with_escape(gerrit, "showAddDepartmentOwnerModal()", "#addDepartmentOwnerModal")

            gerrit.wait_for_function("typeof setTrendStartDate === 'function'")
            gerrit.evaluate("setTrendStartDate('daily', '每日')")
            expect(gerrit.locator("#trendStartModal")).to_have_class(re.compile(r"show"))
            self.press_escape_in_frame(gerrit)
            expect(gerrit.locator("#trendStartModal")).not_to_have_class(re.compile(r"show"))

            gerrit.evaluate("refreshCurrentTab()")
            self.assert_no_page_errors(page_errors)
        finally:
            page.close()

    def test_gerrit_route_check_reuses_test_page_dialog(self):
        page = self.new_page()
        page_errors = []
        ping_requests = []
        page.on("pageerror", lambda exc: page_errors.append(str(exc)))
        page.route(
            "**/api/config/read",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body='{"success":true,"ubuntu_host":"hcq@172.16.14.233"}',
            ),
        )
        page.route(
            "**/api/ssh/ping",
            lambda route: (
                ping_requests.append(route.request.post_data_json),
                route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=(
                        '{"success":true,"reachable":true,"same_network":true,'
                        '"test_host_ip":"172.16.14.233","client_ip":"172.16.14.88",'
                        '"test_network":"172.16.14.0","client_network":"172.16.14.0",'
                        '"latency":"1ms"}'
                    ),
                ),
            )[-1],
        )
        try:
            self.goto_shell(page)
            page.evaluate(
                """() => {
                    state.clusterMode = false;
                    state.clusterEnabled = false;
                }"""
            )

            page.locator("#check-routing-btn").click()
            expect(page.locator("#route-check-dialog")).to_be_visible()
            test_dialog = page.locator("#route-check-dialog").evaluate(
                """dialog => ({
                    className: dialog.className,
                    contentClass: dialog.firstElementChild.className,
                    title: dialog.querySelector('h3').textContent.trim(),
                    labels: [...dialog.querySelectorAll('label')].map(node => node.textContent.trim()),
                    actions: [...dialog.querySelectorAll('.route-check-actions button')]
                        .map(node => node.textContent.trim())
                })"""
            )
            page.locator("#route-check-dialog .route-check-close").click()
            expect(page.locator("#route-check-dialog")).to_have_count(0)

            page.locator('.sidebar-item[data-page="gerrit-dashboard"]').click()
            gerrit = self.frame_for(page, "#gerrit-dashboard-frame")
            gerrit.wait_for_function("typeof checkGerritRoute === 'function'")
            gerrit.evaluate(
                """() => {
                    const banner = document.getElementById('connBanner');
                    banner.classList.add('show');
                    banner.querySelector('button').click();
                }"""
            )

            expect(page.locator("#route-check-dialog")).to_be_visible()
            gerrit_dialog = page.locator("#route-check-dialog").evaluate(
                """dialog => ({
                    className: dialog.className,
                    contentClass: dialog.firstElementChild.className,
                    title: dialog.querySelector('h3').textContent.trim(),
                    labels: [...dialog.querySelectorAll('label')].map(node => node.textContent.trim()),
                    actions: [...dialog.querySelectorAll('.route-check-actions button')]
                        .map(node => node.textContent.trim())
                })"""
            )
            self.assertEqual(gerrit_dialog, test_dialog)
            expect(gerrit.locator("#routeCheckModal")).not_to_have_class(
                re.compile(r"\bshow\b")
            )
            expect(page.locator("#test-host-ip")).to_have_value("172.16.14.233")
            page.locator("#client-ip").fill("172.16.14.88")
            page.locator("#ping-test-btn").click()
            expect(page.locator("#ping-result")).to_contain_text("连通性测试通过")
            self.assertEqual(
                ping_requests,
                [{
                    "test_host_ip": "172.16.14.233",
                    "client_ip": "172.16.14.88",
                }],
            )
            self.assert_no_page_errors(page_errors)
        finally:
            page.close()

    def test_auxiliary_dashboards_safe_buttons_do_not_throw(self):
        page = self.new_page()
        page_errors = []
        page.on("pageerror", lambda exc: page_errors.append(str(exc)))
        try:
            page.goto(f"{self.base_url}/gms-update-monitor", wait_until="domcontentloaded")
            page.wait_for_selector("button")
            for tab_name in ["changes", "artifacts", "packages", "requirements"]:
                page.evaluate("tab => setTab(tab)", tab_name)
                expect(page.locator(f'button[data-tab="{tab_name}"]')).to_have_class(re.compile(r"active"))
                page.evaluate("reload(true)")
                page.evaluate("page(1)")
                page.evaluate("page(-1)")

            page.goto(f"{self.base_url}/mainline-known-issues", wait_until="domcontentloaded")
            page.wait_for_selector("button")
            page.evaluate("reload(true)")
            page.evaluate("page(1)")
            page.evaluate("page(-1)")

            page.goto(f"{self.base_url}/automation", wait_until="domcontentloaded")
            page.evaluate("switchWorkflowPane('runs')")
            page.wait_for_selector("button[data-status]")
            for status in ["", "queued", "testing", "completed"]:
                page.evaluate("status => setStatusFilter(status)", status)
                selector = 'button[data-status="' + status + '"]'
                expect(page.locator(selector)).to_have_class(re.compile(r"active"))
            page.evaluate("loadAll()")

            self.assert_no_page_errors(page_errors)
        finally:
            page.close()

    def test_redmine_tab_loading_stage_center_matches_shell_skeleton(self):
        """切 Tab 加载舞台（iframe 内 fixed）与 shell 骨架屏交接圆心零位移。

        回归：iframe 页 html 常驻滚动条让 inset:0 舞台与 shell 骨架屏、
        内容列的居中基准差半个滚动条宽，文案行高（iframe 默认 normal vs
        shell 1.6）又差出几像素——用户看到「Tab 加载瞬间动画原点往左漂移
        一点」。舞台契约：舞台锚定 iframe 布局视口（== 页头/内容列圆心），
        shell 骨架屏注入等宽右 pad（实测平台滚动条宽度）。挂起数据接口
        让两边动画同时在场，在主视口坐标系断言三方星轨/页头圆心重合。
        """
        page = self.new_page()
        try:
            page.route("**/api/redmine-agent/**", lambda route: None)
            self.goto_shell(page)
            page.evaluate("switchPage('redmine-agent', null)")

            frame = None
            for _ in range(60):
                try:
                    frame = self.frame_for(page, "#redmine-agent-frame")
                except AssertionError:
                    frame = None
                if frame and frame.query_selector(".loading-orbit--stage .loading-orbit-bar"):
                    break
                page.wait_for_timeout(100)
            self.assertIsNotNone(frame, "iframe 文档未出现 Tab 加载舞台")

            shell_geometry = page.evaluate(
                """() => {
                    const frame = document.querySelector('#redmine-agent-frame');
                    const shell = frame.closest('.embedded-frame-shell');
                    const bar = shell.querySelector('.embedded-frame-skeleton-bar')
                        .getBoundingClientRect();
                    const text = shell.querySelector('.loading-orbit-text');
                    const status = shell.querySelector('.embedded-frame-loading');
                    return {
                        frameState: shell.dataset.frameState,
                        bar: {x: bar.x, y: bar.y, width: bar.width, height: bar.height},
                        textLineHeight: text ? getComputedStyle(text).lineHeight : '',
                        padVar: status ? status.style.getPropertyValue('--iframe-scrollbar-width') : '(no status)',
                        probeWidth: window.__platformScrollbarWidthPx,
                        skeletonPad: getComputedStyle(
                            shell.querySelector('.embedded-frame-skeleton')
                        ).paddingRight,
                    };
                }"""
            )
            self.assertEqual(shell_geometry["frameState"], "loading", shell_geometry)

            stage_box = frame.query_selector(
                ".loading-orbit--stage .loading-orbit-bar"
            ).bounding_box()
            header_box = frame.query_selector("header").bounding_box()
            stage_text_line_height = frame.evaluate(
                """() => getComputedStyle(
                    document.querySelector('.loading-orbit--stage .loading-orbit-text')
                ).lineHeight"""
            )
            scrollbar_width = frame.evaluate(
                "window.innerWidth - document.documentElement.clientWidth"
            )
            frame_debug = frame.evaluate(
                """() => ({
                    innerWidth: window.innerWidth,
                    clientWidth: document.documentElement.clientWidth,
                    scrollbarWidthProp: getComputedStyle(document.documentElement).scrollbarWidth,
                    stageWidth: document.querySelector('.loading-orbit--stage')
                        .getBoundingClientRect().width,
                })"""
            )

            stage_center = (
                stage_box["x"] + stage_box["width"] / 2,
                stage_box["y"] + stage_box["height"] / 2,
            )
            skeleton_center = (
                shell_geometry["bar"]["x"] + shell_geometry["bar"]["width"] / 2,
                shell_geometry["bar"]["y"] + shell_geometry["bar"]["height"] / 2,
            )
            header_center_x = header_box["x"] + header_box["width"] / 2
            context = (
                f"shell={shell_geometry} stage={stage_box} header_x={header_center_x} "
                f"scrollbar={scrollbar_width} frame_debug={frame_debug}"
            )
            # 骨架屏（shell 侧）↔ 舞台（iframe 侧）：切 Tab 瞬间圆心零位移。
            self.assertAlmostEqual(
                skeleton_center[0], stage_center[0], delta=0.6,
                msg=f"交接瞬间圆心横向偏移: {context}",
            )
            self.assertAlmostEqual(
                skeleton_center[1], stage_center[1], delta=0.6,
                msg=f"交接瞬间圆心纵向偏移: {context}",
            )
            # 舞台 ↔ 页头/内容列：揭示瞬间圆心零位移（历史上左跳半个滚动条宽）。
            self.assertAlmostEqual(
                stage_center[0], header_center_x, delta=0.6,
                msg=f"舞台与内容列圆心横向偏移: {context}",
            )
            self.assertAlmostEqual(
                shell_geometry["bar"]["width"], stage_box["width"], delta=1,
                msg=f"星轨尺寸不一致: {context}",
            )
            self.assertEqual(shell_geometry["textLineHeight"], stage_text_line_height, shell_geometry)
        finally:
            page.close()

    def test_cluster_loading_orbit_centers_in_grid_container(self):
        """主机集群 Worker 管理区的加载动画在多列 grid 中仍居中。

        回归：#workers 是 .cards 多列 grid，注入的 .loading-orbit 以前
        只占第一列单元格，动画跑到页面最左边；现在 grid-column:1/-1
        横跨整行后必须与容器同心。挂起集群接口让动画保持在场。
        """
        page = self.new_page()
        try:
            page.route("**/api/cluster/**", lambda route: None)
            self.goto_shell(page)
            page.evaluate("switchPage('cluster', null)")

            frame = None
            for _ in range(60):
                try:
                    frame = self.frame_for(page, "#cluster-frame")
                except AssertionError:
                    frame = None
                if frame and frame.query_selector("#workers .loading-orbit-bar"):
                    break
                page.wait_for_timeout(100)
            self.assertIsNotNone(frame, "iframe 文档未出现 Worker 区加载动画")

            geometry = frame.evaluate(
                """() => {
                    const pane = document.getElementById('tab-management');
                    if (pane && pane.hidden && typeof selectClusterDashboardTab === 'function') {
                        selectClusterDashboardTab('management', {persist: false});
                    }
                    const workers = document.getElementById('workers').getBoundingClientRect();
                    const bar = document.querySelector('#workers .loading-orbit-bar')
                        .getBoundingClientRect();
                    const orbit = document.querySelector('#workers > .loading-orbit')
                        .getBoundingClientRect();
                    return {
                        workersCenterX: workers.x + workers.width / 2,
                        orbitWidth: orbit.width,
                        workersWidth: workers.width,
                        barCenterX: bar.x + bar.width / 2,
                    };
                }"""
            )
            self.assertGreater(geometry["orbitWidth"], 0, geometry)
            self.assertAlmostEqual(
                geometry["orbitWidth"], geometry["workersWidth"], delta=1,
                msg=f"加载占位未横跨 grid 整行: {geometry}",
            )
            self.assertAlmostEqual(
                geometry["barCenterX"], geometry["workersCenterX"], delta=1,
                msg=f"加载动画未与容器同心: {geometry}",
            )
        finally:
            page.close()
