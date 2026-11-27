"""Rendered geometry regression: preserve one-line controls."""

import json
import os
from pathlib import Path

from tests.runtime_ui.harness import RuntimeUiHarness


class UiSizingRenderedTests(RuntimeUiHarness):
    def test_test_config_spacing_is_compact_on_desktop(self):
        """Keep the configuration rows aligned and leave more space for logs."""
        page = self.new_page()
        try:
            self.goto_shell(page)
            for width, height in ((1440, 900), (1792, 856)):
                page.set_viewport_size({"width": width, "height": height})
                page.evaluate("switchPage('test', null)")
                page.wait_for_function(
                    "document.getElementById('page-test').classList.contains('active')"
                )
                geometry = page.evaluate(
                    """() => {
                      const root = document.querySelector('#page-test');
                      const config = root.querySelector('.input-container');
                      const style = getComputedStyle(config);
                      const columns = [...config.querySelectorAll(
                        '.left-frame, .middle-frame, .right-frame')];
                      return {
                        top: parseFloat(style.paddingTop),
                        bottom: parseFloat(style.paddingBottom),
                        margin: parseFloat(style.marginBottom),
                        gaps: columns.map(col => parseFloat(getComputedStyle(col).rowGap)),
                        rows: columns.map(col => [...col.querySelectorAll('.form-row')]
                          .map(row => row.getBoundingClientRect().top)),
                        logHeight: root.querySelector('.log-frame').getBoundingClientRect().height,
                      };
                    }"""
                )
                with self.subTest(width=width):
                    self.assertEqual(geometry["top"], 6, geometry)
                    self.assertEqual(geometry["bottom"], 6, geometry)
                    self.assertEqual(geometry["margin"], 6, geometry)
                    self.assertEqual(geometry["gaps"], [4, 4, 4], geometry)
                    for column in geometry["rows"]:
                        self.assertEqual(len(column), 3, geometry)
                        for actual, expected in zip(column, geometry["rows"][0]):
                            self.assertAlmostEqual(actual, expected, delta=1, msg=str(geometry))
                    self.assertGreater(geometry["logHeight"], 0, geometry)
        finally:
            page.close()

    def test_suite_and_analysis_page_frames_follow_header_geometry(self):
        """Three workspace frames start directly below the actual title row."""
        page = self.new_page()
        evidence = {}
        try:
            self.goto_shell(page)
            for width, height in ((1440, 900), (680, 800), (390, 844)):
                page.set_viewport_size({"width": width, "height": height})
                snapshots = {}
                for name, first_selector in (
                    ("test-suites", ".suite-download-section"),
                    ("report-analysis", ".report-workbench-upload"),
                    ("apk-analysis", ".apk-intake-row"),
                ):
                    page.evaluate("(name) => switchPage(name, null)", name)
                    geometry = page.evaluate(
                        """({name, firstSelector}) => {
                          const root = document.getElementById('page-' + name);
                          const header = root.querySelector('.page-header-row');
                          const first = root.querySelector(firstSelector);
                          const frame = getComputedStyle(root, '::after');
                          const emptyUpload = root.querySelector(
                            '#report-upload-zone.upload-empty, #apk-upload-zone.upload-empty');
                          const uploadBorder = emptyUpload && getComputedStyle(emptyUpload);
                          if (!header || !first) throw Error('Missing workspace header/content');
                          return {
                            frameContent: frame.content,
                            frameDisplay: frame.display,
                            emptyUpload: Boolean(emptyUpload),
                            uploadBorderStyle: uploadBorder?.borderTopStyle,
                            uploadBorderWidth: uploadBorder && parseFloat(uploadBorder.borderTopWidth),
                            frameTop: parseFloat(frame.top),
                            headerHeight: header.getBoundingClientRect().height,
                            pagePaddingTop: parseFloat(getComputedStyle(root).paddingTop),
                            contentGap: first.getBoundingClientRect().top -
                              header.getBoundingClientRect().bottom,
                          };
                        }""",
                        {"name": name, "firstSelector": first_selector},
                    )
                    snapshots[name] = geometry
                    target = os.getenv("GMS_UI_PREVIEW_DIR")
                    if target and width in (1440, 390):
                        out = Path(target)
                        out.mkdir(parents=True, exist_ok=True)
                        page.screenshot(path=str(out / f"frame-{name}-{width}.png"))
                    with self.subTest(width=width, page=name):
                        self.assertNotEqual(geometry["frameContent"], "none", geometry)
                        if name == "test-suites":
                            self.assertNotEqual(geometry["frameDisplay"], "none", geometry)
                        elif geometry["emptyUpload"]:
                            # Empty analysis pages draw the same solid 1px border
                            # as the shell's other page frames, without a second outline.
                            self.assertEqual(geometry["frameDisplay"], "none", geometry)
                            self.assertEqual(geometry["uploadBorderStyle"], "solid", geometry)
                            self.assertAlmostEqual(
                                geometry["uploadBorderWidth"], 1, delta=0.1, msg=str(geometry),
                            )
                        self.assertAlmostEqual(
                            geometry["frameTop"],
                            geometry["pagePaddingTop"] + geometry["headerHeight"] + 1,
                            delta=1,
                            msg=str(geometry),
                        )
                        expected_gap = 1
                        self.assertAlmostEqual(
                            geometry["contentGap"], expected_gap,
                            delta=1, msg=str(geometry),
                        )
                evidence[str(width)] = snapshots
            target = os.getenv("GMS_UI_PREVIEW_DIR")
            if target:
                out = Path(target)
                out.mkdir(parents=True, exist_ok=True)
                (out / "computed-workspace-frame-geometry.json").write_text(
                    json.dumps(evidence, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
        finally:
            page.close()

    def test_utility_cards_and_footer_match_8px_outer_frame_inset(self):
        """Tools and Websites: first category card and footer stay 8px inside the frame."""
        page = self.new_page()
        evidence = {}
        try:
            self.goto_shell(page)
            for width, height in ((1440, 900), (390, 844)):
                page.set_viewport_size({"width": width, "height": height})
                for name in ("tools", "websites"):
                    with self.subTest(width=width, page=name):
                        page.evaluate("(name) => switchPage(name, null)", name)
                        page.wait_for_function(
                            f"document.getElementById('page-{name}').classList.contains('active')"
                        )
                        geometry = page.evaluate(
                            """(name) => {
                              const root = document.getElementById('page-' + name);
                              const header = root.querySelector('.page-header-row');
                              const host = root.querySelector('.tools-container');
                              const grid = root.querySelector('.tools-grid-frame');
                              const footerButton = root.querySelector('.tools-add-category');
                              if (!header || !host || !grid || !footerButton) {
                                throw Error('Missing shared utilities page structure');
                              }
                              // Selectors survive initToolsPageBody(), which deletes data-* attrs.
                              // Render many real category-section elements to exercise the
                              // same overflow and scrollbar geometry as populated pages.
                              const category = document.createElement('section');
                              category.className = 'category-section';
                              category.textContent = 'geometry fixture';
                              grid.replaceChildren(category,
                                ...Array.from({length: 36}, () => category.cloneNode(true)));
                              const scroll = root.querySelector('.tools-scroll');
                              scroll.scrollTop = 0;
                              const scrollbarWidth = scroll.offsetWidth - scroll.clientWidth;
                              const frame = getComputedStyle(root, '::after');
                              const rootRect = root.getBoundingClientRect();
                              const cardRect = category.getBoundingClientRect();
                              const footerRect = footerButton.getBoundingClientRect();
                              const outer = {
                                top: rootRect.top + parseFloat(frame.top),
                                left: rootRect.left + parseFloat(frame.left),
                                right: rootRect.right - parseFloat(frame.right),
                                bottom: rootRect.bottom - parseFloat(frame.bottom),
                              };
                              return {
                                top: cardRect.top - outer.top,
                                left: cardRect.left - outer.left,
                                right: outer.right - cardRect.right - scrollbarWidth,
                                renderedRight: outer.right - cardRect.right,
                                scrollbarWidth,
                                scrollable: scroll.scrollHeight > scroll.clientHeight,
                                footerBottom: outer.bottom - footerRect.bottom,
                                headerMarginBottom: parseFloat(getComputedStyle(header).marginBottom),
                                hostPadding: getComputedStyle(host).padding,
                                gridPadding: getComputedStyle(grid).padding,
                                frameBorderWidth: parseFloat(frame.borderTopWidth),
                                overflowX: root.scrollWidth - root.clientWidth,
                              };
                            }""",
                            name,
                        )
                        evidence[f"{name}-{width}"] = geometry
                        for side in ("top", "left", "right", "footerBottom"):
                            self.assertAlmostEqual(geometry[side], 8, delta=0.6, msg=str(geometry))
                        self.assertTrue(geometry["scrollable"], msg=str(geometry))
                        self.assertAlmostEqual(
                            geometry["renderedRight"] - geometry["scrollbarWidth"],
                            8, delta=0.6, msg=str(geometry),
                        )
                        self.assertAlmostEqual(geometry["headerMarginBottom"], 1, delta=0.1)
                        self.assertEqual(geometry["hostPadding"], "8px")
                        self.assertEqual(geometry["gridPadding"], "0px")
                        self.assertAlmostEqual(geometry["frameBorderWidth"], 1, delta=0.1)
                        self.assertLessEqual(geometry["overflowX"], 1)
                target = os.getenv("GMS_UI_PREVIEW_DIR")
                if target:
                    output = Path(target)
                    output.mkdir(parents=True, exist_ok=True)
                    page.screenshot(path=str(output / f"utility-insets-{width}.png"))
            target = os.getenv("GMS_UI_PREVIEW_DIR")
            if target:
                (Path(target) / "computed-utility-insets.json").write_text(
                    json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8"
                )
        finally:
            page.close()

    def test_shared_workspace_sidebar_widths(self):
        page = self.new_page()
        try:
            self.goto_shell(page)
            page.set_viewport_size({"width": 1792, "height": 960})

            def columns(name, selector):
                page.evaluate("(name) => switchPage(name, null)", name)
                grid = page.locator(selector).first
                grid.wait_for(state="visible")
                return grid.evaluate("""el => getComputedStyle(el).gridTemplateColumns
                    .split(/\s+/).map(parseFloat)""")

            for name, selector in (
                ("agent", "#page-agent .agent-layout"),
                ("notes", "#page-notes .kb-layout"),
                ("test-suites", "#page-test-suites .suite-browser-layout"),
            ):
                if name == "notes":
                    page.evaluate("switchPage('notes', null)")
                    page.locator('#page-notes [data-kb-view="personal"]').click()
                self.assertAlmostEqual(columns(name, selector)[0], 250, delta=1)

            for width, expected in ((1792, 350), (1280, 310), (1100, None)):
                page.set_viewport_size({"width": width, "height": 960})
                api = columns("api-docs", "#page-api-docs .api-body-grid")
                audit = columns("security-audit", "#page-security-audit .audit-content-grid:visible")
                if expected is None:
                    self.assertEqual(len(api), 1)
                    self.assertEqual(len(audit), 1)
                else:
                    self.assertEqual(len(api), 2)
                    self.assertEqual(len(audit), 2)
                    self.assertAlmostEqual(api[-1], expected, delta=1)
                    self.assertAlmostEqual(audit[-1], expected, delta=1)
        finally:
            page.close()

    def test_redmine_daily_brief_uses_regular_form_control_height(self):
        page = self.new_page()
        try:
            page.set_viewport_size({"width": 1440, "height": 900})
            page.route("**/api/redmine-agent/**", lambda route: route.fulfill(
                status=200, content_type="application/json", body='{"success":true,"data":{}}',
            ))
            page.goto(f"{self.base_url}/redmine-agent", wait_until="domcontentloaded")
            page.evaluate("switchTab('daily-brief')")
            for selector in ("#singleIssueAnalysisId", "#singleIssueAnalysisMode",
                             "#singleIssueAnalysisDevice [data-device-toggle]",
                             "#singleIssueAnalysisStart"):
                control = page.locator(selector)
                control.wait_for(state="visible")
                with self.subTest(selector=selector):
                    self.assertAlmostEqual(control.bounding_box()["height"], 32, delta=1)
        finally:
            page.close()

    def test_full_height_workspaces_have_one_top_boundary(self):
        """Full-height workspaces and their first panes meet the shell frame without a second seam."""
        page = self.new_page()
        names = (
            "devices-console", "reports", "architecture", "automation",
            "cluster", "redmine-agent", "gerrit-dashboard",
            "desktop", "terminal",
        )
        try:
            self.goto_shell(page)
            for width, height in ((1440, 900), (390, 844)):
                page.set_viewport_size({"width": width, "height": height})
                for name in names:
                    with self.subTest(width=width, page=name):
                        page.evaluate("(name) => switchPage(name, null)", name)
                        page.wait_for_function(
                            f"document.querySelector('#page-{name}').classList.contains('active')"
                        )
                        geometry = page.evaluate(
                            """(name) => {
                              const root = document.getElementById('page-' + name);
                              const header = root.querySelector('.page-header-row');
                              const inner = root.querySelector(
                                '.embedded-frame-shell, .list-table-wrap, .host-workspace-grid');
                              if (!header || !inner) throw Error('Missing shared page surface');
                              const frame = getComputedStyle(root, '::after');
                              const pane = inner.matches('.host-workspace-grid')
                                ? inner.querySelector('.host-workspace-pane') : null;
                              return {
                                outerTop: root.getBoundingClientRect().top + parseFloat(frame.top),
                                contentTop: inner.getBoundingClientRect().top,
                                paneTop: pane ? pane.getBoundingClientRect().top : null,
                                gridPaddingTop: inner.matches('.host-workspace-grid')
                                  ? parseFloat(getComputedStyle(inner).paddingTop) : null,
                                headerMarginBottom: parseFloat(getComputedStyle(header).marginBottom),
                                outerBorderStyle: frame.borderTopStyle,
                                outerBorderWidth: parseFloat(frame.borderTopWidth),
                                innerBorderWidth: parseFloat(getComputedStyle(inner).borderTopWidth),
                                pageEdge: parseFloat(getComputedStyle(root).paddingTop),
                              };
                            }""",
                            name,
                        )
                        self.assertAlmostEqual(
                            geometry["outerTop"], geometry["contentTop"],
                            delta=0.6, msg=str(geometry),
                        )
                        self.assertAlmostEqual(
                            geometry["headerMarginBottom"], 1,
                            delta=0.1, msg=str(geometry),
                        )
                        self.assertEqual(geometry["outerBorderStyle"], "solid")
                        self.assertAlmostEqual(geometry["outerBorderWidth"], 1, delta=0.1)
                        self.assertAlmostEqual(geometry["innerBorderWidth"], 0, delta=0.1)
                        self.assertAlmostEqual(geometry["pageEdge"], 8, delta=0.1)
                        if name in ("desktop", "terminal"):
                            self.assertAlmostEqual(
                                geometry["gridPaddingTop"], 0, delta=0.1,
                                msg=str(geometry),
                            )
                            if geometry["paneTop"] is not None:
                                self.assertAlmostEqual(
                                    geometry["outerTop"], geometry["paneTop"],
                                    delta=0.6, msg=str(geometry),
                                )
        finally:
            page.close()

    def test_embedded_dashboard_stat_geometry_matches_across_modules(self):
        """Keep KPI height consistent and verify each workspace's panel spacing."""
        regular_spacing = {"inset": 8, "chartGap": 16, "cardGap": 12}
        cluster_spacing = {"inset": 12, "chartGap": 8, "cardGap": 8}
        page = self.new_page()
        evidence = {}
        try:
            for width, height in ((1440, 900), (960, 720)):
                page.set_viewport_size({"width": width, "height": height})
                measured = {}
                for module in ("cluster", "redmine-agent", "gerrit-dashboard"):
                    page.goto(f"{self.base_url}/{module}", wait_until="domcontentloaded")
                    geometry = page.evaluate(
                        """(module) => {
                          const cluster = module === 'cluster';
                          if (!cluster) {
                            const redmine = module === 'redmine-agent';
                            const tab = document.querySelector(redmine
                              ? '#tab-stats' : '#tab-personal');
                            const content = document.querySelector(redmine
                              ? '#statsContent' : '#personalContent');
                            if (!tab || !content) throw new Error('Missing dashboard markup');
                            tab.classList.add('active');
                            if (!redmine) {
                              document.querySelector('#personalStatus').textContent = '';
                              const banner = document.querySelector('#connBanner');
                              banner.classList.remove('show');
                              banner.style.display = 'none';
                            }
                            const cards = Array.from(
                              {length: redmine ? 7 : 6},
                              (_, i) => '<div class="stat-card"><div class="value">' +
                                (i + 1) + '</div><div class="label">' +
                                (redmine ? '当前未关闭' : '待评审') +
                                '</div></div>'
                            ).join('');
                            content.innerHTML = '<section class="' +
                              (redmine ? 'stats-section' : 'list-section') +
                              '"><div class="stats-grid">' + cards +
                              '</div></section><div class="trend-grid">' +
                              '<section class="trend-panel">趋势</section></div>';
                          }
                          const header = document.querySelector('.workspace-header');
                          const grid = document.querySelector(cluster
                            ? '#dashboard .dash-cards' : '.tab-content.active .stats-grid');
                          const next = document.querySelector(cluster
                            ? '#dashboard .dash-charts-row' : '.tab-content.active .trend-grid');
                          if (!header || !grid || !next) {
                            throw new Error('Missing dashboard geometry element');
                          }
                          const cards = Array.from(grid.querySelectorAll(cluster
                            ? '.dash-stat-card' : '.stat-card'));
                          const headerRect = header.getBoundingClientRect();
                          const gridRect = grid.getBoundingClientRect();
                          const nextRect = next.getBoundingClientRect();
                          return {
                            inset: gridRect.top - headerRect.bottom,
                            chartGap: nextRect.top - gridRect.bottom,
                            cardGap: parseFloat(getComputedStyle(grid).columnGap),
                            cardHeights: cards.map(card => card.getBoundingClientRect().height),
                            cardRadii: cards.map(card => parseFloat(getComputedStyle(card).borderTopLeftRadius)),
                            icons: cluster ? cards.map(card => {
                              const icon = card.querySelector('.ui-kpi-icon');
                              if (!icon) throw new Error('Missing shared KPI icon role');
                              const rect = icon.getBoundingClientRect();
                              return {width: rect.width, height: rect.height};
                            }) : [],
                          };
                        }""",
                        module,
                    )
                    measured[module] = geometry
                    with self.subTest(viewport=width, module=module):
                        spacing = cluster_spacing if module == "cluster" else regular_spacing
                        for name, expected in spacing.items():
                            self.assertAlmostEqual(
                                geometry[name], expected, delta=1, msg=str(geometry),
                            )
                        self.assertGreater(len(geometry["cardHeights"]), 0)
                        for card_height in geometry["cardHeights"]:
                            self.assertAlmostEqual(
                                card_height, 95, delta=1, msg=str(geometry),
                            )
                        for radius in geometry['cardRadii']:
                            self.assertAlmostEqual(radius, 8, delta=1, msg=str(geometry))
                        for icon in geometry['icons']:
                            size = 42 if width > 1200 else 34
                            self.assertAlmostEqual(icon['width'], size, delta=1, msg=str(geometry))
                            self.assertAlmostEqual(icon['height'], size, delta=1, msg=str(geometry))
                evidence[str(width)] = measured
            target = os.getenv("GMS_UI_PREVIEW_DIR")
            if target:
                out = Path(target)
                out.mkdir(parents=True, exist_ok=True)
                (out / "computed-embedded-dashboard-geometry.json").write_text(
                    json.dumps(evidence, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
        finally:
            page.close()

    def test_common_pagination_geometry(self):
        page = self.new_page()
        try:
            self.goto_shell(page)
            for width in (1440, 390):
                page.set_viewport_size({"width": width, "height": 900})
                geometry = page.evaluate("""() => {
                    const host = document.createElement('div');
                    host.className = 'ui-pagination-footer';
                    host.style.cssText = 'position:fixed;left:0;top:0;z-index:-1';
                    host.innerHTML = '<div class="ui-pagination"><button>上一页</button>'
                      + '<button class="active" disabled>1</button></div>'
                      + '<select><option>20</option></select>';
                    document.body.appendChild(host);
                    try {
                        return Array.from(host.querySelectorAll('button,select')).map(el => ({
                            height: el.getBoundingClientRect().height,
                            radius: parseFloat(getComputedStyle(el).borderTopLeftRadius),
                        }));
                    } finally {
                        host.remove();
                    }
                }""")
                expected = 28 if width > 560 else 40
                for item in geometry:
                    self.assertAlmostEqual(item["height"], expected, delta=1)
                    self.assertAlmostEqual(item["radius"], 6, delta=1)
        finally:
            page.close()

    def test_shell_geometry_and_no_wrap_at_desktop_and_mobile_widths(self):
        page = self.new_page()
        evidence = {}
        errors = []
        try:
            self.goto_shell(page)
            for width, height in ((1440, 900), (1792, 1000), (390, 844)):
                page.set_viewport_size({"width": width, "height": height})
                snapshot = {}
                for name in ("users", "devices", "security-audit", "api-docs"):
                    page.evaluate("(name) => switchPage(name, null)", name)
                    page.wait_for_timeout(180)
                    measured = page.evaluate(
                        """(name) => {
                          const root = document.getElementById('page-' + name);
                          const rect = (el) => el.getBoundingClientRect();
                          const visible = (el) => rect(el).width > 0 && rect(el).height > 0;
                          const elements = (selector) => Array.from(
                            root.querySelectorAll(selector)
                          ).filter(visible);
                          const controls = {
                            users: ['#users-list-search', '#users-list-status-filter',
                                    '#users-list-refresh'],
                            'api-docs': ['.api-search-box', '.api-search-toolbar .api-action'],
                          };
                          const singleLine = {
                            users: ['.users-view-tabs .btn-xs', '.users-table thead th'],
                            devices: ['.page-header-actions button',
                                      '.device-management-table thead th'],
                            'security-audit': ['.page-header-actions button'],
                            'api-docs': ['.api-search-toolbar .api-action',
                                         '.api-path', '.api-title'],
                          };
                          const samples = [];
                          for (const selector of (controls[name] || [])) {
                            for (const el of elements(selector).slice(0, 4)) {
                              samples.push({selector, h: rect(el).height});
                            }
                          }
                          const inline = [];
                          for (const selector of (singleLine[name] || [])) {
                            for (const el of elements(selector).slice(0, 8)) {
                              inline.push({selector,
                                           whiteSpace: getComputedStyle(el).whiteSpace,
                                           text: (el.textContent || '').trim().slice(0, 30)});
                            }
                          }
                          const cards = elements('.page-stat-row > .page-stat-card')
                            .map((el) => rect(el).height);
                          const tabHeights = name === 'users'
                            ? elements('.users-view-tabs .btn-xs').map((el) => rect(el).height)
                            : [];
                          return {name, samples, inline, cards, tabHeights};
                        }""",
                        name,
                    )
                    snapshot[name] = measured
                    for el in measured["samples"]:
                        if abs(el["h"] - 32) > 1:
                            errors.append(f"{width}px {name}: {el['selector']} height {el['h']} != 32")
                    for el in measured["inline"]:
                        if el["whiteSpace"] != "nowrap":
                            errors.append(f"{width}px {name}: {el['selector']} wraps: {el}")
                    if width >= 1440:
                        for h in measured["cards"]:
                            if abs(h - 95) > 1:
                                errors.append(f"{width}px {name}: KPI height {h} != 95")
                    if name == "users":
                        for h in measured["tabHeights"]:
                            if abs(h - 24) > 1:
                                errors.append(f"{width}px users: segmented tab {h} != 24")
                    if width in (1440, 390):
                        target = os.getenv("GMS_UI_PREVIEW_DIR")
                        if target:
                            out = Path(target)
                            out.mkdir(parents=True, exist_ok=True)
                            page.screenshot(path=str(out / f"sizing-{name}-{width}.png"))
                if width >= 1440:
                    card_heights = [
                        v for item in snapshot.values() for v in item["cards"]
                    ]
                    if len(card_heights) < 10:
                        errors.append(f"{width}px: insufficient KPI cards")
                    elif max(card_heights) - min(card_heights) > 1:
                        errors.append(f"{width}px: KPI heights differ {card_heights}")
                evidence[str(width)] = snapshot
            target = os.getenv("GMS_UI_PREVIEW_DIR")
            if target:
                (Path(target) / "computed-ui-sizing.json").write_text(
                    json.dumps(evidence, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            self.assertEqual(errors, [], "\n".join(errors))
        finally:
            page.close()
