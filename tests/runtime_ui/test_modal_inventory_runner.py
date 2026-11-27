"""Mandatory per-dialog browser matrix for the isolated Actions runner.

One unittest test per source-owned modal. Every viewport produces a screenshot
and structured verdict, including a failure record rather than a skipped item.
No device/Worker operation, submission, or production resource is executed.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from unittest.mock import patch

from tests.runtime_ui.harness import RuntimeUiHarness, expect


ROOT = Path(__file__).resolve().parents[2]
SHELL_ROOT = ROOT / "web/shell/dialogs"
OUTPUT = Path(os.environ.get("GMS_MODAL_MATRIX_OUT", "/tmp/gms-modal-matrix"))
VIEWPORTS = (
    {"width": 1440, "height": 900},
    {"width": 390, "height": 844},
    {"width": 844, "height": 390},
)
WORKSPACES = {
    "redmine": ("features/redmine/ui/page.html", "/redmine-agent"),
    "gerrit": ("features/gerrit/ui/page.html", "/gerrit-dashboard"),
    "cluster": ("features/cluster/ui/page.html", "/cluster"),
    "devices": ("features/devices/ui/page.html", "/devices-console"),
    "update-monitor": ("features/gms/ui/page.html", "/gms-update-center"),
}
SOURCE_MODAL = re.compile(r'<div\b[^>]*\bid="([^"]+)"[^>]*\bclass="([^"]+)"', re.S)


def discover(source: Path) -> list[str]:
    markup = source.read_text(encoding="utf-8")
    return [
        modal_id for modal_id, classes in SOURCE_MODAL.findall(markup)
        if "modal" in classes.split() or "modal-backdrop" in classes.split()
    ]


SHELL_CASES = sorted(
    (path.relative_to(ROOT).as_posix(), modal_id)
    for path in SHELL_ROOT.glob("*.html")
    for modal_id in discover(path)
)
# Update Radar injects its shared dialog server-side; it is absent from
# the static page.html source, but must still be checked as a standalone route.
INJECTED_WORKSPACE_CASES = [
    ("radar", "features/gms/radar/ui/page.html",
     "/gms-update-radar", "radar-settings-modal"),
]
WORKSPACE_CASES = sorted(
    [
        (name, relative, url, modal_id)
        for name, (relative, url) in WORKSPACES.items()
        for modal_id in discover(ROOT / relative)
    ] + INJECTED_WORKSPACE_CASES
)

GEOMETRY = r"""content => {
  const rect = el => {
    const b = el.getBoundingClientRect();
    return {left:b.left,top:b.top,right:b.right,bottom:b.bottom,
            width:b.width,height:b.height};
  };
  const active = el => !!el && el.getClientRects().length > 0
      && getComputedStyle(el).visibility !== 'hidden';
  const outer = rect(content), header = content.querySelector(
      ':scope > .modal-header,:scope > .modal-head,:scope > .section-head');
  const body = content.querySelector(':scope > .modal-body');
  const footer = [...content.children].find(el =>
      el.matches('.modal-buttons,.modal-actions,.modal-footer,.tech-dialog-footer,.agent-access-form-actions')
      && active(el));
  const escapeScroll = el => {
    for (let node = el.parentElement; node && node !== content; node=node.parentElement) {
      if (['auto','scroll'].includes(getComputedStyle(node).overflowX)) return true;
    }
    return false;
  };
  const leaked = [...content.querySelectorAll('button,input,select,textarea,[role=button]')]
      .filter(active).filter(el => {
        const b = rect(el);
        return (b.left < outer.left - 2 || b.right > outer.right + 2)
             && !escapeScroll(el);
      }).map(el => el.id || el.textContent?.trim().slice(0,40));
  const controls = footer ? [...footer.querySelectorAll('button')].filter(active)
      .map(el => ({name:el.textContent.trim(), rect:rect(el)})) : [];
  const nestedActions = body ? [...body.querySelectorAll('.modal-buttons,.modal-footer,.modal-actions')]
      .filter(active).map(el => el.className) : [];
  const glyph = header?.querySelector('.tech-title-glyph');
  const use = glyph?.querySelector('svg use');
  const bodyStyle = body && getComputedStyle(body);
  const hasInternalScroll = !!body && [...body.querySelectorAll('*')]
      .some(el => {
          const cs=getComputedStyle(el);
          return (cs.overflowY==='auto' || cs.overflowY==='scroll')
             && el.scrollHeight > el.clientHeight + 3;
      });
  return {
    bounds:outer, viewport:{width:innerWidth,height:innerHeight},
    tech:content.classList.contains('tech-dialog'),
    frame:!!content.querySelector(':scope > .tech-frame'),
    title:header?.querySelector('.modal-title')?.textContent?.trim() || '',
    close:!!header?.querySelector('.modal-close'),
    glyph:!!glyph, glyphHref:use?.getAttribute('href') || '',
    glyphCustom:!!glyph?.querySelector('svg path, svg circle, svg ellipse, img'),
    glyphEmoji: ['🔌'].includes(glyph?.textContent?.trim()),
    header:header ? rect(header) : null,
    body:body ? rect(body) : null,
    bodyScroll:body ? {height:body.clientHeight,content:body.scrollHeight,
      overflow:bodyStyle.overflowY,internal:hasInternalScroll} : null,
    footer:footer ? {bounds:rect(footer),
      justify:getComputedStyle(footer).justifyContent,
      position:getComputedStyle(footer).position,controls} : null,
    nestedActions,leaked,
  };
}"""


class ModalInventoryRunnerTests(RuntimeUiHarness):
    def _install_read_fixtures(self, page):
        # Use no production config, credentials, device or Worker operations.
        fixtures = {
            "**/api/config/read": {"success": True, "ubuntu_host": "127.0.0.1"},
            "**/api/config/client-ssh-credentials": {"success": True, "credentials": []},
            "**/api/config/static-routes": {
                "success": True, "data": {"enabled": False, "routes": []}},
            "**/api/email/smtp": {"success": True, "data": {"enabled": False}},
            "**/api/email/outbox?*": {"success": True, "data": {"items": [], "total": 0}},
            "**/api/gms-update-radar/settings": {"success": True, "data": {
                "enabled": False, "scan_time": "04:00", "timezone": "Asia/Shanghai",
                "recipients": ["reader@example.invalid"], "notify": "changes",
                "dashboard_url": self.base_url + "/gms-update-radar",
                "tasks": ["packages", "exemptions", "bulletins", "requirements",
                          "mainline", "artifacts", "security_patches"],
            }},
        }
        def read_fixture(data):
            def fulfill_read(route):
                self.assertEqual(route.request.method, "GET", route.request.url)
                route.fulfill(json=data)
            return fulfill_read

        for pattern, payload in fixtures.items():
            page.route(pattern, read_fixture(payload))

    def _prepare_content(self, page, modal_id: str):
        # A visible frame alone cannot validate a lazily populated workspace.
        if modal_id == "sidebar-visibility-modal":
            page.evaluate("openSidebarVisibilityModal('system', 'runtime')")
            expect(page.locator("#system-config-body input").first).to_be_visible()
            expect(page.locator("#system-runtime-config-form #system-config-footer")).to_be_visible()
            panel = page.locator("#sidebar-settings-panel-system")
            expect(panel.locator(".modal-buttons")).to_have_count(0)
            for selector in ("#system-config-footer", "#system-smtp-form .settings-action-row",
                             ".config-section-accent-routes .settings-action-row",
                             '[data-system-settings-section="external"] .settings-action-row'):
                row = panel.locator(selector)
                expect(row).to_be_visible()
                assert row.evaluate("(el) => getComputedStyle(el).display === 'flex'")
                assert row.evaluate("(el) => getComputedStyle(el).paddingTop === '0px' && getComputedStyle(el).paddingBottom === '0px'")
                if page.viewport_size["width"] >= 1000:
                    assert row.evaluate("(el) => el.clientHeight <= el.querySelector('button').offsetHeight + 12")
            expect(panel.locator("[data-system-settings-section]")).to_have_count(4)
            expect(panel.locator(".sidebar-settings-tabs")).to_have_count(0)
            page.wait_for_function("document.querySelector('#sidebar-settings-panel-system').scrollHeight > document.querySelector('#sidebar-settings-panel-system').clientHeight")
            panel.evaluate("(el) => { el.scrollTop = el.scrollHeight; }")
            assert panel.evaluate("(el) => el.scrollTop > 0")
            expect(page.locator("#gms-assistant-url")).to_be_in_viewport()
            expect(page.locator('[data-sidebar-settings-tab="system"]')).to_be_in_viewport()
            expect(page.locator("#elevate-modal")).to_be_hidden()
        elif modal_id == "radar-settings-modal":
            page.evaluate("() => { void GmsUpdateRadarSettings.open(); }")
            expect(page.locator("#radar-time")).to_have_value("04:00")
            expect(page.locator("#radar-task-count")).to_have_text("已选择 7 / 7 项")
            expect(page.locator("#radar-settings-save")).to_be_enabled()
        elif modal_id == "guide-image-modal":
            page.evaluate("""() => {
                const image = document.getElementById('guide-image-preview');
                const svg = '<svg xmlns="http://www.w3.org/2000/svg" width="800" height="450">'
                    + '<rect width="800" height="450" fill="#252038"/>'
                    + '<text x="200" y="225" fill="white" font-size="32">GMS UI fixture</text></svg>';
                image.src = 'data:image/svg+xml,' + encodeURIComponent(svg);
                image.alt = '隔离测试图片';
                document.getElementById('guide-image-caption').textContent = '图片预览布局验证';
            }""")
            page.wait_for_function("document.getElementById('guide-image-preview').naturalWidth === 800")
        elif modal_id == "test-results-modal":
            page.evaluate("""() => {
                renderTestResults([{session:'fixture-session-01', pass:1216, fail:18,
                    project:'RK3588 fixture', result_directory:'fixture-results'}],
                    ['Session', 'Pass', 'Fail', 'Result Directory', 'Project']);
                document.getElementById('test-results-modal-status').textContent = '共 1 条测试结果';
            }""")
            expect(page.locator("#test-results-tbody tr").first).to_be_visible()
        elif modal_id == "report-diagnosis-modal":
            page.evaluate("""() => {
                window.currentReportAnalysisData = {
                    summary:{total:1240,pass:1216,fail:18}, details:{test_type:'CTS'}};
                renderReportDiagnosis({report_name:'CTS rendering fixture',
                    test_name:'ConnectivityTest.testConnection', module:'CtsNetTestCases',
                    test_type:'CTS', summary:'连接超时，需要核查设备与测试主机之间的网络。',
                    error_message:'java.net.SocketTimeoutException: connection timed out',
                    ai_result:{root_cause:'请结合原始日志验证网络状态。',
                        root_cause_status:'hypothesis', root_cause_confidence:'medium',
                        suggestions:['核查设备连接与主机网络。', '保留失败日志。']}});
            }""")
            expect(page.locator("#report-diagnostic-result")).to_contain_text("网络")
        elif modal_id == "ui-control-modal":
            page.evaluate("""() => {
                const image = document.getElementById('ui-control-screenshot');
                image.src = 'data:image/svg+xml,' + encodeURIComponent(
                    '<svg xmlns="http://www.w3.org/2000/svg" width="360" height="640">'
                    + '<rect width="360" height="640" fill="#252038"/>'
                    + '<text x="80" y="320" fill="white">Device fixture</text></svg>');
                document.getElementById('ui-control-status').textContent = '隔离屏幕与 UI 元素预览';
                renderUiControlElements(Array.from({length:12}, (_, i) => ({
                    text:'测试控件 ' + i, class_name:'android.widget.Button',
                    resource_id:'fixture:id/control_' + i, center:[100, 40 + i * 40],
                    interactions:['clickable']})));
            }""")
            page.wait_for_function("document.getElementById('ui-control-screenshot').naturalWidth === 360")
            expect(page.locator("#ui-control-elements .ui-ctrl-el")).to_have_count(12)
            metrics = page.locator(".ui-control-workspace").evaluate("""workspace => {
                const screen = workspace.querySelector('.ui-control-screen-panel').getBoundingClientRect();
                const image = workspace.querySelector('img').getBoundingClientRect();
                const elements = workspace.querySelector('.ui-control-elements-panel').getBoundingClientRect();
                return {imageBottom:image.bottom, screenBottom:screen.bottom,
                    screenRight:screen.right, elementsTop:elements.top, elementsLeft:elements.left};
            }""")
            self.assertLessEqual(metrics["imageBottom"], metrics["screenBottom"] + 1)
            if page.viewport_size["width"] <= 800:
                self.assertGreaterEqual(metrics["elementsTop"], metrics["screenBottom"] - 1)
            else:
                self.assertGreaterEqual(metrics["elementsLeft"], metrics["screenRight"] - 1)
        elif modal_id == "agent-access-scopes-modal":
            page.evaluate("""() => {
                const list = document.getElementById('agent-access-token-scope-list');
                list.replaceChildren();
                for (const scope of ['devices:read', 'devices:control', 'tests:read',
                        'tests:execute', 'reports:read', 'firmware:read']) {
                    const row = document.createElement('div');
                    row.className = 'tech-field-card';
                    row.textContent = scope;
                    list.appendChild(row);
                }
            }""")

    def _record(self, page, *, area: str, modal_id: str, viewport: dict,
                opening_error: str = "") -> dict:
        key = f"{area}--{modal_id}--{viewport['width']}x{viewport['height']}"
        safe = re.sub(r"[^a-zA-Z0-9_.-]", "_", key)
        OUTPUT.mkdir(parents=True, exist_ok=True)
        report = {"key": key, "source": area, "modal": modal_id,
                  "viewport": viewport, "status": "failed"}
        if opening_error:
            report["error"] = opening_error
        modal = page.locator(f"#{modal_id}")
        screenshot = OUTPUT / f"{safe}.png"
        try:
            self.assertEqual(modal.count(), 1, key)
            self.assertTrue(modal.is_visible(), key)
            content = modal.locator(":scope > .modal-content")
            self.assertEqual(content.count(), 1, key)
            page.screenshot(path=str(screenshot), animations="disabled")
            report["screenshot"] = screenshot.name
            metrics = content.evaluate(GEOMETRY)
            report["layout"] = metrics
            bounds = metrics["bounds"]
            width, height = viewport["width"], viewport["height"]
            self.assertGreater(bounds["width"], 140, key)
            self.assertGreater(bounds["height"], 80, key)
            self.assertGreaterEqual(bounds["left"], -2, key)
            self.assertGreaterEqual(bounds["top"], -2, key)
            self.assertLessEqual(bounds["right"], width + 2, key)
            self.assertLessEqual(bounds["bottom"], height + 2, key)
            if metrics["tech"]:
                self.assertTrue(metrics["frame"], key)
                self.assertTrue(metrics["title"], key)
                self.assertTrue(metrics["glyph"], key)
                self.assertTrue(metrics["close"], key)
                self.assertTrue(
                    metrics["glyphHref"].startswith("/static/icons/ui.svg#")
                    or metrics["glyphCustom"] or metrics["glyphEmoji"], key)
            self.assertEqual(metrics["leaked"], [], key)
            self.assertEqual(metrics["nestedActions"], [], key)
            footer = metrics["footer"]
            if footer:
                self.assertEqual(footer["justify"], "flex-end", key)
                self.assertLessEqual(footer["bounds"]["bottom"], bounds["bottom"] + 1, key)
                self.assertGreaterEqual(footer["bounds"]["top"], bounds["top"] - 1, key)
                if metrics["body"]:
                    self.assertLessEqual(
                        metrics["body"]["bottom"], footer["bounds"]["top"] + 1, key)
                for button in footer["controls"]:
                    rect = button["rect"]
                    self.assertGreaterEqual(rect["left"], bounds["left"] - 1, key)
                    self.assertLessEqual(rect["right"], bounds["right"] + 1, key)
                    self.assertGreaterEqual(rect["top"], footer["bounds"]["top"] - 1, key)
                    self.assertLessEqual(rect["bottom"], footer["bounds"]["bottom"] + 1, key)
            body_scroll = metrics["bodyScroll"]
            if body_scroll and body_scroll["content"] > body_scroll["height"] + 6:
                self.assertTrue(
                    body_scroll["overflow"] in ("scroll", "auto") or body_scroll["internal"],
                    f"{key}: content taller than visible body without usable scrolling")
            if not opening_error:
                report["status"] = "passed"
        except Exception as exc:
            report["error"] = "; ".join(filter(None, (
                opening_error, f"{type(exc).__name__}: {exc}")))
            if not screenshot.exists():
                try:
                    page.screenshot(path=str(screenshot), animations="disabled")
                    report["screenshot"] = screenshot.name
                except Exception as screenshot_exc:
                    report["screenshot_error"] = str(screenshot_exc)
        finally:
            (OUTPUT / f"{safe}.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        return report

    def _check_case(self, *, area: str, url: str, modal_id: str) -> None:
        page = self.new_page()
        failures = []
        try:
            self._install_read_fixtures(page)
            if area == "shell":
                self.goto_shell(page)
            else:
                page.goto(self.base_url + url, wait_until="domcontentloaded")
            for viewport in VIEWPORTS:
                page.set_viewport_size(viewport)
                try:
                    if area == "shell":
                        page.evaluate("(id) => { ModalManager.closeAll(); ModalManager.open(id); }",
                                      modal_id)
                    else:
                        page.evaluate("""id => {
                            const el = document.getElementById(id);
                            if (!el) throw new Error('missing modal: '+id);
                            if (el.classList.contains('modal-backdrop')) {
                                el.hidden = false;
                                window.syncClusterModalState?.();
                            } else if (window.EmbeddedModalController?.open) {
                                EmbeddedModalController.open(id);
                            } else {
                                el.classList.add('show');
                            }
                        }""", modal_id)
                    self._prepare_content(page, modal_id)
                    report = self._record(
                        page, area=area, modal_id=modal_id, viewport=viewport)
                    if report["status"] != "passed":
                        failures.append(report["key"] + ": " + report.get("error", "unknown"))
                except Exception as exc:
                    # Even an opening failure must have its own persisted
                    # viewport verdict and screenshot; never silently skip it.
                    report = self._record(
                        page, area=area, modal_id=modal_id, viewport=viewport,
                        opening_error=f"{type(exc).__name__}: {exc}")
                    failures.append(
                        f"{area}/{modal_id} {viewport}: {exc}; "
                        + report.get("error", "could not open"))
                finally:
                    try:
                        if area == "shell":
                            page.evaluate("(id) => ModalManager.close(id)", modal_id)
                        else:
                            page.evaluate("""id => {
                                const el = document.getElementById(id);
                                if (el?.classList.contains('modal-backdrop')) el.hidden = true;
                                else if (window.EmbeddedModalController?.close)
                                    EmbeddedModalController.close(id);
                                else el?.classList.remove('show');
                            }""", modal_id)
                    except Exception:
                        pass
        finally:
            page.close()
        self.assertEqual(failures, [], "\n".join(failures))

    def test_opening_failure_is_preserved_in_evidence(self):
        page = self.new_page()
        page.add_init_script("""document.addEventListener('DOMContentLoaded', () => {
            const original = ModalManager.open.bind(ModalManager);
            ModalManager.open = id => {
                original(id);
                if (id === 'category-modal') throw new Error('fixture initialization failure');
            };
        });""")
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            with (
                patch(__name__ + ".OUTPUT", output),
                patch.object(self, "new_page", return_value=page),
                self.assertRaisesRegex(AssertionError, "fixture initialization failure"),
            ):
                self._check_case(area="shell", url="/", modal_id="category-modal")
            records = [json.loads(path.read_text()) for path in output.glob("*.json")]
            self.assertEqual(len(records), len(VIEWPORTS))
            for record in records:
                self.assertEqual(record["status"], "failed")
                self.assertIn("fixture initialization failure", record["error"])
                self.assertTrue((output / record["screenshot"]).is_file())

    def test_inventory_matches_all_shell_source_templates(self):
        self.assertGreaterEqual(len(list(SHELL_ROOT.glob("*.html"))), 40)
        self.assertGreaterEqual(len(SHELL_CASES), 40)
        self.assertEqual(
            len({modal_id for _, modal_id in SHELL_CASES}),
            len(SHELL_CASES),
            "duplicate modal IDs across shell templates",
        )
        page = self.new_page()
        try:
            self.goto_shell(page)
            observed = set(page.locator(".modal[id]").evaluate_all(
                "els => els.map(el => el.id)"))
            expected = {modal_id for _, modal_id in SHELL_CASES}
            self.assertEqual(expected - observed, set(), "missing source-owned modal")
            self.assertEqual(observed - expected, set(),
                             "unregistered shell modals need a test fixture")
        finally:
            page.close()

    def test_workspace_inventory_is_nonempty(self):
        self.assertGreaterEqual(len(WORKSPACE_CASES), 15)
        for name, relative, url, modal_id in WORKSPACE_CASES:
            self.assertTrue((ROOT / relative).is_file(), (name, url, modal_id))


def _register_test(name: str, area: str, url: str, modal_id: str) -> None:
    def run(self):
        self._check_case(area=area, url=url, modal_id=modal_id)
    run.__name__ = name
    setattr(ModalInventoryRunnerTests, name, run)


for relpath, identifier in SHELL_CASES:
    stem = Path(relpath).stem.replace("-", "_")
    _register_test(f"test_shell_{stem}_{identifier.replace('-', '_')}",
                   "shell", "/", identifier)

for area_name, _relative, route, identifier in WORKSPACE_CASES:
    _register_test(f"test_workspace_{area_name}_{identifier.replace('-', '_')}",
                   area_name, route, identifier)
