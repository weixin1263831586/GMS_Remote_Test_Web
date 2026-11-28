"""Real-browser geometry checks for newly unified workspaces (no business actions)."""

import json
import os
from pathlib import Path

from tests.runtime_ui.harness import RuntimeUiHarness


class UiControlsMatrixTests(RuntimeUiHarness):
    def test_audit_suite_and_apk_controls_keep_one_line_and_correct_density(self):
        page = self.new_page()
        failures = []
        seen = {}
        try:
            self.goto_shell(page)
            # Use a local UI-only elevation fixture so the screenshot records
            # the underlying workbench, not an unrelated admin gate.
            page.evaluate("() => { state.elevated = true; state.elevatedUntil = Date.now() + 300000; }")
            for width, height in ((1440, 900), (1792, 900), (390, 844)):
                page.set_viewport_size({"width": width, "height": height})
                for name, selector in (
                    ("security-audit", ".audit-filter-bar .audit-toolbar-button"),
                    ("test-suites", ".suite-search-field > .btn-xs"),
                    ("apk-analysis", ".filter-btn[data-apk-tab]"),
                ):
                    page.evaluate("(name) => switchPage(name, null)", name)
                    if name == "apk-analysis":
                        # Reuse the existing UI fixture setup, no APK processing.
                        page.evaluate("""() => {
                          setApkUploadEmpty(false);
                          document.getElementById('apk-analysis-result').style.display = 'block';
                          document.getElementById('apk-analysis-status').style.display = 'block';
                        }""")
                    page.wait_for_timeout(120)
                    if page.locator(".modal.show").count():
                        self.close_initial_modals(page)
                    measured = page.evaluate("""({name, selector}) => {
                      const root = document.getElementById('page-' + name);
                      return [...root.querySelectorAll(selector)]
                        .filter(el => el.getBoundingClientRect().width > 0
                                   && el.getBoundingClientRect().height > 0)
                        .map(el => ({
                          text: (el.textContent || '').trim().slice(0, 30),
                          height: el.getBoundingClientRect().height,
                          whiteSpace: getComputedStyle(el).whiteSpace
                        }));
                    }""", {"name": name, "selector": selector})
                    seen[f"{width}/{name}"] = measured
                    if not measured:
                        failures.append(f"{width}px {name}: controls not visible")
                    for item in measured:
                        if item["whiteSpace"] != "nowrap":
                            failures.append(f"{width}px {name}: wraps {item}")
                        if name == "apk-analysis":
                            expected = 38 if width <= 850 else 42
                            if abs(item["height"] - expected) > 1:
                                failures.append(f"{width}px APK tab height {item} != {expected}")
                        elif item["height"] > 44:
                            failures.append(f"{width}px {name}: unexpectedly tall {item}")
                    # Inputs and their adjacent submit actions must stay aligned;
                    # a single row must not mix 28px and 32px controls.
                    pair = page.evaluate("""name => {
                      const selectors = {
                        'security-audit': ['#audit-search-input', '.audit-log-filters .audit-toolbar-button'],
                        'test-suites': ['#suite-file-search', '.suite-search-submit'],
                        'apk-analysis': ['#apk-file-search', '.apk-search-container > button']
                      }[name];
                      const root = document.getElementById('page-' + name);
                      const [a, b] = selectors.map(s => root.querySelector(s));
                      const isVisible = el => el && el.getBoundingClientRect().width > 0
                        && el.getBoundingClientRect().height > 0;
                      if (!isVisible(a) || !isVisible(b)) return null;
                      const x = a.getBoundingClientRect(), y = b.getBoundingClientRect();
                      return { heightDelta: Math.abs(x.height - y.height),
                        centerDelta: Math.abs((x.top + x.bottom) / 2 - (y.top + y.bottom) / 2) };
                    }""", name)
                    # Responsive audit filters intentionally occupy separate rows.
                    # Only compare centers for controls actually sharing a row.
                    if pair and (pair["heightDelta"] > 2 or (width >= 768 and pair["centerDelta"] > 2)):
                        failures.append(f"{width}px {name}: search input / button misaligned {pair}")
                    target = os.getenv("GMS_UI_PREVIEW_DIR")
                    if target and width in (1440, 390):
                        dest = Path(target)
                        dest.mkdir(parents=True, exist_ok=True)
                        page.screenshot(path=str(dest / f"controls-{name}-{width}.png"))
            if os.getenv("GMS_UI_PREVIEW_DIR"):
                (Path(os.environ["GMS_UI_PREVIEW_DIR"]) / "controls-computed.json").write_text(
                    json.dumps(seen, ensure_ascii=False, indent=2), encoding="utf-8"
                )
            self.assertEqual(failures, [], "\n".join(failures) + f"\nSamples: {seen}")
        finally:
            page.close()


    def test_plain_actions_keep_their_label_and_busy_geometry(self):
        page = self.new_page()
        try:
            self.goto_shell(page)
            result = page.evaluate("""() => {
                const selectors = [
                    '#apk-task-history-refresh',
                    '#users-list-refresh',
                    '#refresh-suites-btn',
                    '#refresh-devices-btn',
                    '#page-api-docs .api-search-toolbar .api-action',
                    '#page-security-audit .audit-toolbar-button',
                    '#page-devices .inventory-refresh'
                ];
                const controls = selectors.flatMap(selector => [...document.querySelectorAll(selector)]);
                const bad = controls.filter(el => el.querySelector('svg, .test-action-icon') ||
                    /[↻🔄]/u.test(el.textContent)).map(el => el.id || el.textContent.trim());
                const original = document.getElementById('apk-task-history-refresh');
                const probe = original.cloneNode(true);
                probe.removeAttribute('id');
                probe.disabled = false;
                probe.textContent = '刷新';
                probe.style.cssText = 'position:fixed;top:8px;left:8px';
                document.body.append(probe);
                const before = probe.getBoundingClientRect().width;
                const style = getComputedStyle(probe);
                const identifiable = style.borderTopStyle === 'solid' && parseFloat(style.borderTopWidth) >= 1 && style.backgroundColor !== 'rgba(0, 0, 0, 0)';
                setActionButtonBusy(probe, true, '刷新中');
                const busy = {label: probe.textContent, disabled: probe.disabled,
                    width: probe.getBoundingClientRect().width};
                setActionButtonBusy(probe, false);
                const after = {label: probe.textContent, disabled: probe.disabled,
                    width: probe.getBoundingClientRect().width};
                probe.remove();
                return {count: controls.length, bad, before, busy, after, identifiable};
            }""")
            self.assertGreaterEqual(result["count"], 7)
            self.assertEqual(result["bad"], [])
            self.assertTrue(result["identifiable"])
            self.assertGreater(result["before"], 0)
            self.assertEqual(result["busy"]["label"], "刷新中")
            self.assertTrue(result["busy"]["disabled"])
            self.assertEqual(result["after"]["label"], "刷新")
            self.assertFalse(result["after"]["disabled"])
            self.assertAlmostEqual(result["before"], result["busy"]["width"], delta=1)
            self.assertAlmostEqual(result["before"], result["after"]["width"], delta=1)
        finally:
            page.close()


    def test_refresh_action_rendering_and_screenshots(self):
        page = self.new_page()
        samples = {}
        try:
            self.goto_shell(page)
            page.set_viewport_size({"width": 1792, "height": 900})
            targets = (
                ("users", "#users-list-refresh"),
                ("apk-analysis", "#apk-task-history-refresh"),
                ("api-docs", "#page-api-docs .api-search-toolbar .api-action"),
            )
            for name, selector in targets:
                page.evaluate("(name) => switchPage(name, null)", name)
                page.locator(selector).first.wait_for(state="visible")
                samples[name] = self._action_geometry(page, selector)
                self._capture_action_page(page, name)
            for name, route, selector in (
                ("cluster", "/cluster", "#refresh"),
                ("redmine", "/redmine-agent", "#refreshBtn"),
                ("gerrit", "/gerrit-dashboard", "#refreshBtn"),
            ):
                page.goto(self.base_url + route, wait_until="domcontentloaded")
                page.locator(selector).first.wait_for(state="visible")
                samples[name] = self._action_geometry(page, selector)
                self._capture_action_page(page, name)
            directory = os.getenv("GMS_UI_PREVIEW_DIR")
            if directory:
                (Path(directory) / "button-geometry.json").write_text(
                    json.dumps(samples, ensure_ascii=False, indent=2), encoding="utf-8"
                )
            for name, records in samples.items():
                self.assertTrue(records, name)
                for record in records:
                    with self.subTest(page=name, label=record["label"]):
                        self.assertEqual(record["icons"], 0, record)
                        self.assertGreaterEqual(record["height"], 24, record)
                        self.assertLessEqual(record["height"], 44, record)
                        self.assertTrue(record["outlined"], record)
                        self.assertTrue(record["filled"], record)
                        self.assertTrue(record["fits"], record)
        finally:
            page.close()

    @staticmethod
    def _action_geometry(page, selector):
        return page.locator(selector).evaluate_all("""controls => controls
            .filter(button => button.getBoundingClientRect().width > 0 &&
                              button.getBoundingClientRect().height > 0)
            .map(button => {
                const style = getComputedStyle(button);
                const rect = button.getBoundingClientRect();
                return {
                    label: button.textContent.trim(),
                    icons: button.querySelectorAll('svg, .test-action-icon').length,
                    width: rect.width, height: rect.height,
                    radius: style.borderTopLeftRadius,
                    color: style.color, background: style.backgroundColor,
                    border: style.borderTopColor,
                    outlined: style.borderTopStyle === 'solid' && parseFloat(style.borderTopWidth) >= 1,
                    filled: style.backgroundColor !== 'rgba(0, 0, 0, 0)',
                    fits: button.scrollWidth <= button.clientWidth + 1,
                    disabled: button.disabled,
                };
            })""")

    @staticmethod
    def _capture_action_page(page, name):
        directory = os.getenv("GMS_UI_PREVIEW_DIR")
        if directory:
            target = Path(directory)
            target.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(target / f"button-{name}-1792.png"))


    def test_action_width_reserves_longest_label_in_all_states(self):
        page = self.new_page()
        try:
            self.goto_shell(page)
            for route in (None, "/redmine-agent", "/gerrit-dashboard", "/cluster"):
                if route:
                    page.goto(self.base_url + route, wait_until="domcontentloaded")
                result = page.evaluate("""() => {
                    const host = document.createElement('div');
                    host.style.cssText = 'position:fixed;left:8px;top:8px;display:flex;gap:6px';
                    document.body.append(host);
                    const records = [];
                    for (const [size, idle, busy, done, width] of [
                        ['short', '刷新', '刷新中', '已刷新', 64],
                        ['short', '重置', '重置中', '已重置', 64],
                        ['plain', '下载套件', '下载中', '下载完成', 76],
                        ['long', '确认并发送', '发送中', '发送成功', 92]
                    ]) {
                        const button = document.createElement('button');
                        button.className = 'btn-xs secondary';
                        button.dataset.busySize = size;
                        host.append(button);
                        const states = [];
                        for (const label of [idle, busy, done, idle]) {
                            button.textContent = label;
                            button.disabled = label === busy;
                            const rect = button.getBoundingClientRect();
                            states.push({width:rect.width,height:rect.height,
                                fits:button.scrollWidth <= button.clientWidth + 1});
                        }
                        records.push({expected:width, states});
                    }
                    host.remove();
                    return records;
                }""")
                for item in result:
                    with self.subTest(route=route, width=item["expected"]):
                        for state in item["states"]:
                            self.assertAlmostEqual(state["width"], item["expected"], delta=1)
                            self.assertAlmostEqual(state["height"], item["states"][0]["height"], delta=1)
                            self.assertTrue(state["fits"], item)
        finally:
            page.close()


    def test_shared_filter_toolbars_use_one_desktop_row(self):
        page = self.new_page()
        samples = {}
        try:
            self.goto_shell(page)
            page.evaluate("() => { state.elevated = true; state.elevatedUntil = Date.now() + 300000; }")
            targets = (
                ("users", "#users-main-view .users-list-toolbar"),
                ("users", "#agent-access-panel .agent-access-toolbar"),
                ("users", "#sdk-config-panel .agent-access-toolbar"),
                ("devices", ".inventory-toolbar"),
                ("api-docs", ".api-search-toolbar"),
                ("security-audit", ".audit-log-filters"),
                ("security-audit", ".audit-mail-filters"),
                ("security-audit", ".audit-disk-filters"),
                ("notes", "[data-kb-source-form]"),
                ("test-suites", ".suite-download-row"),
            )
            for width, height in ((1792, 900), (1440, 900), (390, 844)):
                page.set_viewport_size({"width": width, "height": height})
                for name, selector in targets:
                    page.evaluate("(name) => switchPage(name, null)", name)
                    # Display the requested local UI fixture without invoking privileged
                    # mutations or external data sources.
                    page.evaluate("""({name,selector}) => {
                        if (name === 'users') {
                            for (const id of ['users-main-view','agent-access-panel','sdk-config-panel']) {
                                const panel = document.getElementById(id);
                                panel.style.display = selector.startsWith('#'+id) ? 'flex' : 'none';
                            }
                        }
                        if (name === 'security-audit') {
                            const ids = ['audit-main-view','audit-outbox-panel','audit-disk-panel'];
                            const selectors = ['.audit-log-filters','.audit-mail-filters','.audit-disk-filters'];
                            for (let i=0; i<ids.length; i++) document.getElementById(ids[i]).style.display = selectors[i]===selector ? 'flex' : 'none';
                        }
                        if (name === 'notes') {
                            document.querySelector('#page-notes .kb-layout').style.display = 'none';
                            document.querySelector('[data-kb-external]').hidden = false;
                        }
                    }""", {"name":name,"selector":selector})
                    bar = page.locator(selector).first
                    bar.wait_for(state="visible")
                    result = bar.evaluate("""bar => {
                        const box = bar.getBoundingClientRect();
                        const controls = [...bar.querySelectorAll('input:not([type=checkbox]), select, button')]
                            .filter(el => el.getBoundingClientRect().width > 0 && el.getBoundingClientRect().height > 0);
                        const rects = controls.map(el => el.getBoundingClientRect());
                        const centers = rects.map(r => (r.top+r.bottom)/2);
                        const root = bar.closest('#users-main-view,#agent-access-panel,#sdk-config-panel,#devices-main-view,#audit-main-view,#audit-outbox-panel,#audit-disk-panel');
                        const stat = root?.querySelector(':scope > .page-stat-row');
                        const next = bar.nextElementSibling;
                        const statsBox = stat?.getBoundingClientRect();
                        const contentBox = next?.getBoundingClientRect();
                        const spacing = statsBox && contentBox ? {before:box.top-statsBox.bottom, after:contentBox.top-box.bottom, statTop:statsBox.top, toolbarTop:box.top, toolbarBottom:box.bottom, left:box.left, right:box.right} : null;
                        return {spacing,shared:bar.classList.contains('ui-workspace-toolbar--filter'),
                            height:box.height,padding:getComputedStyle(bar).padding,
                            controls:rects.map(r=>r.height),
                            oneRow: Math.max(...centers)-Math.min(...centers)<=1,
                            contained:rects.every(r=>r.left>=box.left-1 && r.right<=box.right+1)};
                    }""")
                    samples[f"{width}/{selector}"] = result
                    with self.subTest(width=width, selector=selector):
                        self.assertTrue(result["shared"], result)
                        self.assertTrue(result["controls"], result)
                        self.assertTrue(result["contained"], result)
                        self.assertTrue(all(abs(h-32)<=1 for h in result["controls"]), result)
                        if width >= 1440:
                            self.assertTrue(result["oneRow"], result)
                            if name in {"users", "devices", "security-audit"}:
                                self.assertIsNotNone(result["spacing"], result)
                                self.assertAlmostEqual(result["spacing"]["before"], 8, delta=1)
                                self.assertAlmostEqual(result["spacing"]["after"], 8, delta=1)
                    if os.getenv("GMS_UI_PREVIEW_DIR") and width == 1792:
                        target=Path(os.environ["GMS_UI_PREVIEW_DIR"])
                        target.mkdir(parents=True,exist_ok=True)
                        label=selector.split()[0].strip("#.[]").replace("data-kb-source-form","external-sources")
                        page.screenshot(path=str(target / f"filter-toolbar-{name}-{label}.png"))
            for width in (1792, 1440):
                aligned = [samples[f"{width}/{selector}"]["spacing"] for name, selector in targets
                           if name in {"users", "devices", "security-audit"}]
                for key in ("statTop", "toolbarTop", "toolbarBottom", "left", "right"):
                    values = [record[key] for record in aligned]
                    self.assertLessEqual(max(values) - min(values), 1, (width, key, values))
            if os.getenv("GMS_UI_PREVIEW_DIR"):
                (Path(os.environ["GMS_UI_PREVIEW_DIR"]) / "filter-toolbar-geometry.json").write_text(
                    json.dumps(samples, ensure_ascii=False, indent=2), encoding="utf-8")
        finally:
            page.close()
