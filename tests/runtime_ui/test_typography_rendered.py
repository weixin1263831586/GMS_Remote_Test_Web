"""Browser-computed typography roles at desktop and mobile viewport widths."""

import json
import os
from pathlib import Path

from tests.runtime_ui.harness import RuntimeUiHarness


class TypographyRenderedTests(RuntimeUiHarness):
    def check_sizes(self, page, cases, label):
        found = page.evaluate(
            """(cases) => cases.map(([selector, expected]) => {
                const element = document.querySelector(selector);
                if (!element) return {selector, expected, missing: true};
                const style = getComputedStyle(element);
                return {
                    selector, expected,
                    size: Number.parseFloat(style.fontSize),
                    font: style.fontFamily,
                    weight: style.fontWeight
                };
            })""",
            cases,
        )
        failures = [
            f"{label}: {item}" for item in found
            if item.get("missing") or item["size"] != item["expected"]
        ]
        return found, failures

    def test_shell_typography_with_viewport_evidence(self):
        page = self.new_page()
        evidence = {}
        failures = []
        cases = [
            ["#page-users .page-header-row > .section-title", 14],
            ["#page-devices .page-header-row > .section-title", 14],
            ["#page-agent .page-header-row > .section-title", 14],
            ["#page-apk-analysis .page-header-row > .section-title", 14],
            ["#page-report-analysis .page-header-row > .section-title", 14],
            ["#page-apk-analysis .page-title-icon", 14],
            ["#page-users .page-title-icon", 14],
            ["#page-test-suites #btn-download-suite", 12],
            ["#page-test-suites #suite-download-url", 13],
            ["#page-apk-analysis .apk-upload-content > button", 12],
            ["#page-apk-analysis #apk-task-history", 13],
            ["#page-report-analysis .report-upload-content > button", 12],
            ["#page-devices .device-management-table thead th", 12],
            ["#page-security-audit .audit-log-table thead th", 12],
            ["#page-test .adb-button-frame button", 12],
        ]
        try:
            self.goto_shell(page)
            for width, height in ((1440, 900), (390, 844)):
                with self.subTest(width=width):
                    page.set_viewport_size({"width": width, "height": height})
                    page.evaluate("() => document.fonts.ready.then(() => true)")
                    samples, problems = self.check_sizes(page, cases, "Shell")
                    evidence[str(width)] = samples
                    failures.extend(problems)
                    target = os.getenv("GMS_UI_PREVIEW_DIR")
                    if target:
                        out = Path(target)
                        out.mkdir(parents=True, exist_ok=True)
                        for name in ("users", "test-suites", "apk-analysis", "security-audit"):
                            page.evaluate("(name) => switchPage(name, null)", name)
                            page.wait_for_timeout(120)
                            page.screenshot(path=str(out / f"{name}-{width}.png"))
            target = os.getenv("GMS_UI_PREVIEW_DIR")
            if target:
                (Path(target) / "computed-typography.json").write_text(
                    json.dumps(evidence, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
            self.assertEqual(failures, [], "\n".join(failures))
        finally:
            page.close()

    def test_shell_page_heading_and_icons_use_14px_500(self):
        page = self.new_page()
        try:
            self.goto_shell(page)
            measured = page.evaluate(
                """() => {
                    const titleRows = Array.from(document.querySelectorAll(
                        '.page-content .page-header-row > .section-title'
                    )).map(title => {
                        const icon = title.querySelector('.page-title-icon');
                        const style = getComputedStyle(title);
                        const iconStyle = icon ? getComputedStyle(icon) : null;
                        return {
                            page: title.closest('.page-content')?.id,
                            size: style.fontSize,
                            weight: style.fontWeight,
                            iconSize: iconStyle?.fontSize ?? null,
                            titleGap: style.columnGap,
                            iconMarginRight: iconStyle?.marginRight ?? null,
                            imageWidth: icon?.tagName === 'IMG' ? iconStyle.width : null,
                            imageHeight: icon?.tagName === 'IMG' ? iconStyle.height : null
                        };
                    });
                    const testSections = Array.from(document.querySelectorAll(
                        '#page-test .section-title'
                    )).map(node => ({
                        size: getComputedStyle(node).fontSize,
                        weight: getComputedStyle(node).fontWeight
                    }));
                    return {titleRows, testSections};
                }"""
            )
            self.assertGreaterEqual(len(measured["titleRows"]), 21)
            for item in measured["titleRows"]:
                self.assertEqual(item["size"], "14px", item)
                self.assertEqual(item["weight"], "500", item)
                self.assertEqual(item["iconSize"], "14px", item)
                self.assertEqual(item["titleGap"], "8px" if item["page"] == "page-apk-analysis" else "6px", item)
                self.assertEqual(item["iconMarginRight"], "0px", item)
                if item["page"] == "page-apk-analysis":
                    self.assertEqual(item["imageWidth"], "16px", item)
                    self.assertEqual(item["imageHeight"], "14px", item)
            self.assertGreaterEqual(len(measured["testSections"]), 3)
            for item in measured["testSections"]:
                self.assertEqual(item["size"], "14px", item)
                self.assertEqual(item["weight"], "500", item)
        finally:
            page.close()

    def test_independent_pages_use_same_computed_control_roles(self):
        page = self.new_page()
        failures = []
        try:
            routes = (
                ("/automation", ((".ats-actions button", 12),)),
                ("/cluster", ((".workspace-tab", 12),)),
                ("/devices-console", (("#refresh-ports", 12),)),
                ("/redmine-agent", ((".workspace-tab", 12), ("#searchInput", 13))),
                ("/gerrit-dashboard", (("button", 12), ("#query", 13))),
            )
            for route, cases in routes:
                with self.subTest(route=route):
                    page.goto(self.base_url + route, wait_until="domcontentloaded")
                    page.evaluate("() => document.fonts.ready.then(() => true)")
                    _, problems = self.check_sizes(page, cases, route)
                    failures.extend(problems)
            self.assertEqual(failures, [], "\n".join(failures))
        finally:
            page.close()


    def test_modal_routing_and_header_controls_are_semantic(self):
        page = self.new_page()
        failures = []
        try:
            self.goto_shell(page)
            self.close_initial_modals(page)
            page.evaluate("""() => {
                const fixture = document.createElement('section');
                fixture.id = 'type-role-fixture';
                fixture.className = 'page-header-actions';
                fixture.innerHTML = '<button type="button">普通操作</button>' +
                    '<select><option>选择主机</option></select>' +
                    '<table class="list-table"><thead><tr><th>名称</th></tr></thead></table>' +
                    '<button type="button" class="btn-terminal">终端操作</button>' +
                    '<button type="button" class="tools-add-category">添加分类</button>';
                document.body.appendChild(fixture);
                for (const id of ['adb-proxy-assignments', 'usbip-assignments']) {
                    const list = document.getElementById(id);
                    const row = document.createElement('div');
                    row.className = 'adb-proxy-assignment';
                    row.innerHTML = '<div class="adb-proxy-assignment-info">source → worker · USB</div>';
                    list.appendChild(row);
                }
            }""")
            page.evaluate("""() => {
                const fixture = document.getElementById('type-role-fixture');
                fixture.insertAdjacentHTML('beforeend',
                    '<button id="type-category-tab" class="category-tab">常用工具</button>' +
                    '<span id="type-file-browser-help" class="file-browser-error-help">主机连接提示</span>' +
                    '<span id="type-history-mail-count" class="history-mail-count">共 100 条</span>');
                const mode = document.createElement('span');
                mode.setAttribute('data-terminal-pane-mode-label', '');
                mode.id = 'type-terminal-mode';
                mode.textContent = '终端';
                document.getElementById('page-terminal').appendChild(mode);
            }""")
            cases = (
                ("#type-category-tab", 12),
                ("#type-file-browser-help", 12),
                ("#type-history-mail-count", 12),
                ("#type-terminal-mode", 12),
                ("#page-desktop .host-workspace-title-state", 12),
                ("#page-terminal .host-workspace-title-state", 12),
                ("#type-role-fixture > button:first-child", 12),
                ("#type-role-fixture > select", 13),
                ("#type-role-fixture th", 12),
                ("#type-role-fixture .btn-terminal", 12),
                ("#type-role-fixture .tools-add-category", 12),
                ("#adb-proxy-assignments .adb-proxy-assignment-info", 13),
                ("#usbip-assignments .adb-proxy-assignment-info", 13),
            )
            for width, height in ((1440, 900), (390, 844)):
                with self.subTest(viewport=width):
                    page.set_viewport_size({"width": width, "height": height})
                    _, problems = self.check_sizes(page, cases, "shell + routing")
                    failures.extend(problems)
            self.assertEqual(failures, [], "\n".join(failures))
        finally:
            page.close()

    def test_normal_description_roles_are_not_compact(self):
        page = self.new_page()
        failures = []
        try:
            for route, selector, size in (
                ("/automation", ".panel-description", 13),
                ("/cluster", ".dash-test-row", 12),
                ("/redmine-agent", ".field-content", 13),
            ):
                with self.subTest(route=route):
                    page.goto(self.base_url + route, wait_until="domcontentloaded")
                    page.evaluate("""selector => {
                        const node = document.createElement('div');
                        node.id = 'typography-prose-fixture';
                        node.className = selector.slice(1);
                        node.textContent = '普通正文与信息不得使用紧凑注释字号';
                        document.body.appendChild(node);
                    }""", selector)
                    _, problems = self.check_sizes(page, (
                        ("#typography-prose-fixture", size),
                    ), route)
                    failures.extend(problems)
                    page.evaluate("() => document.getElementById('typography-prose-fixture')?.remove()")
            self.assertEqual(failures, [], "\n".join(failures))
        finally:
            page.close()
