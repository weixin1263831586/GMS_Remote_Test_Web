"""Dialog content, overflow, async cancellation and rendered review gallery."""
from __future__ import annotations

import html
import itertools
import json
import os
from pathlib import Path

from tests.runtime_ui.harness import RuntimeUiHarness, expect


# These are rendering fixtures, never Controller/Worker operations.
DIAGNOSIS = {
    "report_name": "CTS rendering fixture", "test_name": "ConnectivityTest.testConnection",
    "module": "CtsNetTestCases", "test_type": "CTS", "suite_version": "16_r3",
    "summary": "连接超时，需要进一步核查设备与测试主机之间的网络。",
    "error_message": "java.net.SocketTimeoutException: connection timed out",
    "ai_result": {
        "root_cause": "设备连接超时；请结合原始日志验证网络状态。",
        "root_cause_status": "hypothesis", "root_cause_confidence": "medium",
        "suggestions": ["核查设备连接与主机网络。", "保留失败日志并重新执行单测。"],
    },
}

LAYOUT = """content => {
  const bounds = content.getBoundingClientRect();
  const visible = el => el.getClientRects().length && getComputedStyle(el).visibility !== 'hidden';
  const contained = el => {
    for (let p = el.parentElement; p && p !== content; p = p.parentElement) {
      if (['auto','scroll'].includes(getComputedStyle(p).overflowX)) return true;
    }
    return false;
  };
  return {
    bounds: {left: bounds.left, right: bounds.right, top: bounds.top, bottom: bounds.bottom},
    frame: !!content.querySelector('.tech-frame'),
    typographyMismatches: [...content.querySelectorAll('.modal-title,label,input,select,textarea,button:not(.modal-close)')]
      .filter(visible).flatMap(el => {
        const wanted = el.classList.contains('modal-title') ? '16px'
          : el.tagName === 'BUTTON' ? '12px' : '13px';
        const actual = getComputedStyle(el).fontSize;
        return actual === wanted ? [] : [{element: el.id || el.tagName, wanted, actual}];
      }),
    cutSurface: (() => {
      const frame = content.querySelector(':scope > .tech-frame');
      return !frame || getComputedStyle(content).clipPath !== 'none';
    })(),
    headerSingleOwner: (() => {
      const header = [...content.children].find(el =>
        el.matches('.modal-header,.modal-head,.section-head') && el.querySelector(':scope > .tech-header-rule'));
      if (!header) return true;
      const rule = header.querySelector(':scope > .tech-header-rule');
      const rail = rule.querySelector('path');
      const highlight = rule.querySelector('.tech-rule-highlight');
      return getComputedStyle(header).borderBottomWidth === '0px'
        && getComputedStyle(header, '::after').content === 'none'
        && !!highlight && !!rail && highlight.getTotalLength() > 0
        && rail.isPointInStroke(highlight.getPointAtLength(highlight.getTotalLength() / 2));
    })(),
    alignedCorners: (() => {
      const frame = content.querySelector('.tech-frame');
      const edge = frame?.querySelector('path');
      const accent = frame?.querySelector('.tech-frame-accent');
      if (!edge || !accent) return false;
      const length = accent.getTotalLength();
      return Array.from({length: 17}, (_, i) => accent.getPointAtLength(length * i / 16))
        .every(point => edge.isPointInStroke(point));
    })(),
    ticksOverlapClose: [...content.querySelectorAll('.tech-header-rule .tech-rule-ticks')].some(ticks => {
      const a = ticks.getBoundingClientRect();
      return [...content.querySelectorAll('.modal-close')].filter(visible).some(close => {
        const b = close.getBoundingClientRect();
        return a.left < b.right && a.right > b.left && a.top < b.bottom && a.bottom > b.top;
      });
    }),
    fieldCardOverlaps: (() => {
      const groups = new Map();
      for (const card of [...content.querySelectorAll('.tech-field-card')].filter(visible)) {
        if (['absolute','fixed'].includes(getComputedStyle(card).position)) continue;
        if (!groups.has(card.parentElement)) groups.set(card.parentElement, []);
        groups.get(card.parentElement).push(card);
      }
      const collisions = [];
      for (const cards of groups.values()) {
        for (let i = 0; i < cards.length; ++i) {
          for (let j = i + 1; j < cards.length; ++j) {
            const a = cards[i].getBoundingClientRect();
            const b = cards[j].getBoundingClientRect();
            const overlapX = Math.min(a.right, b.right) - Math.max(a.left, b.left);
            const overlapY = Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top);
            if (overlapX > 4 && overlapY > 4) {
              collisions.push([
                cards[i].querySelector('label')?.textContent.trim() || cards[i].id,
                cards[j].querySelector('label')?.textContent.trim() || cards[j].id,
                Math.round(overlapX), Math.round(overlapY)
              ]);
            }
          }
        }
      }
      return collisions.slice(0, 10);
    })(),
    fieldStackOverflows: [...content.querySelectorAll('.tech-field-card.tech-field-stack')]
      .filter(visible).flatMap(card => {
        const parent = card.getBoundingClientRect();
        return [...card.children].filter(visible)
          .filter(el => !['absolute','fixed'].includes(getComputedStyle(el).position))
          .filter(el => {
            const rect = el.getBoundingClientRect();
            return rect.left < parent.left - 2 || rect.right > parent.right + 2
              || rect.top < parent.top - 2 || rect.bottom > parent.bottom + 2;
          })
          .map(el => ({
            label: card.querySelector('label')?.textContent.trim() || card.id,
            element: el.tagName.toLowerCase(), text: (el.textContent || '').trim().slice(0, 32)
          }));
      }).slice(0, 10),
    leaks: [...content.querySelectorAll('input, select, textarea, button, [role="button"]')]
      .filter(visible).filter(el => {
        const b = el.getBoundingClientRect();
        return (b.left < bounds.left - 2 || b.right > bounds.right + 2) && !contained(el);
      }).map(el => el.id || el.textContent.slice(0,40)),
    clipped: [...content.querySelectorAll('button')].filter(visible)
      .filter(el => el.clientWidth && el.scrollWidth > el.clientWidth + 2)
      .map(el => el.id || el.textContent.slice(0,40)),
    footer: [...content.children].filter(el => el.matches('.modal-buttons,.modal-actions,.modal-footer'))
      .filter(visible).map(el => {
        const b = el.getBoundingClientRect();
        return {top: b.top, bottom: b.bottom};
      })
  };
}"""


class ModalRedesignTests(RuntimeUiHarness):
    def inspect_dialog(self, page, selector):
        content = page.locator(selector)
        expect(content).to_be_visible()
        page.wait_for_function("""selector => {
          const frame = document.querySelector(selector)?.querySelector('.tech-frame');
          if (!frame?.hasAttribute('data-frame-ready') || !frame.querySelector('.tech-frame-accent')) return false;
          const box = frame.getBoundingClientRect();
          return Math.abs(frame.viewBox.baseVal.width - box.width) < 1
            && Math.abs(frame.viewBox.baseVal.height - box.height) < 1;
        }""", arg=selector)
        report = content.evaluate(LAYOUT)
        bounds = report["bounds"]
        viewport = page.viewport_size
        self.assertGreaterEqual(bounds["left"], -1, report)
        self.assertGreaterEqual(bounds["top"], -1, report)
        self.assertLessEqual(bounds["right"], viewport["width"] + 1, report)
        self.assertLessEqual(bounds["bottom"], viewport["height"] + 1, report)
        self.assertTrue(report["frame"], report)
        self.assertEqual(report["typographyMismatches"], [], report)
        self.assertTrue(report["alignedCorners"], report)
        self.assertTrue(report["cutSurface"], report)
        self.assertTrue(report["headerSingleOwner"], report)
        self.assertFalse(report["ticksOverlapClose"], report)
        self.assertEqual(report["fieldCardOverlaps"], [], report)
        self.assertEqual(report["fieldStackOverflows"], [], report)
        self.assertEqual(report["leaks"], [], report)
        self.assertEqual(report["clipped"], [], report)
        for footer in report["footer"]:
            self.assertLessEqual(footer["bottom"], bounds["bottom"] + 1, report)
        return report

    def test_results_header_minimize_is_aligned_and_clear_of_frame(self):
        page = self.new_page()
        try:
            self.goto_shell(page)
            self.close_initial_modals(page)
            for viewport in (
                {"width": 1672, "height": 941},
                {"width": 920, "height": 680},
                {"width": 390, "height": 844},
            ):
                with self.subTest(viewport=viewport):
                    page.set_viewport_size(viewport)
                    page.evaluate("ModalManager.open('test-results-modal')")
                    header = page.locator(
                        "#test-results-modal .tech-dialog--test-results > .modal-header"
                    )
                    expect(header).to_be_visible()
                    bounds = header.evaluate("""header => {
                        const box = el => el.getBoundingClientRect();
                        const outer = box(header.closest('.modal-content'));
                        const rail = box(header);
                        const min = box(header.querySelector('.test-results-minimize'));
                        const close = box(header.querySelector('.test-results-header-actions > .modal-close'));
                        const svg = header.querySelector(':scope > .tech-header-rule');
                        const ruleRect = box(svg);
                        const line = svg.querySelector('path').getBBox();
                        const lineEnd = ruleRect.left + (line.x + line.width) * ruleRect.width / 1000;
                        return {
                            minimizeWidth: min.width,
                            minimizeHeight: min.height,
                            closeWidth: close.width,
                            closeHeight: close.height,
                            decorationClearance: min.left - lineEnd,
                            topInset: min.top - outer.top,
                            bottomInset: rail.bottom - min.bottom,
                            rightInset: outer.right - close.right,
                            minCloseGap: close.left - min.right,
                            centerDelta: Math.abs((min.top + min.bottom) / 2 -
                                (close.top + close.bottom) / 2),
                            closeInside: close.bottom <= rail.bottom - 8 &&
                                close.top >= rail.top + 8,
                        };
                    }""")
                    self.assertGreaterEqual(bounds["topInset"], 10, bounds)
                    self.assertGreaterEqual(bounds["bottomInset"], 9, bounds)
                    self.assertGreaterEqual(bounds["rightInset"], 12, bounds)
                    self.assertEqual(bounds["minimizeWidth"], 30, bounds)
                    self.assertEqual(bounds["minimizeHeight"], 30, bounds)
                    self.assertEqual(bounds["closeWidth"], 30, bounds)
                    self.assertEqual(bounds["closeHeight"], 30, bounds)
                    self.assertGreaterEqual(bounds["decorationClearance"], 8, bounds)
                    self.assertGreaterEqual(bounds["minCloseGap"], 6, bounds)
                    self.assertLessEqual(bounds["centerDelta"], 1.5, bounds)
                    self.assertTrue(bounds["closeInside"], bounds)
                    page.evaluate("ModalManager.close('test-results-modal')")

            page.evaluate("ModalManager.open('test-results-modal')")
            page.evaluate("minimizeTestResultsModal()")
            expect(page.locator("#test-results-modal")).to_be_hidden()
            expect(page.locator("#test-results-minimized")).to_be_visible()
            page.evaluate("restoreTestResultsModal()")
            expect(page.locator("#test-results-modal")).to_be_visible()
            expect(page.locator("#test-results-minimized")).to_be_hidden()
        finally:
            page.close()

    def test_static_and_dynamic_dialog_gallery(self):
        page = self.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(error.stack or str(error)))
        output = Path(os.environ["GMS_MODAL_GALLERY_OUT"]) if os.getenv("GMS_MODAL_GALLERY_OUT") else None
        records = []
        if output:
            output.mkdir(parents=True, exist_ok=True)

        def capture(key, selector):
            report = self.inspect_dialog(page, selector)
            file = f"{key}-{page.viewport_size['width']}.png"
            if output:
                page.screenshot(path=str(output / file), animations="disabled")
            records.append({"id": key, "viewport": page.viewport_size, "file": file, "layout": report})

        try:
            self.goto_shell(page)
            self.close_initial_modals(page)
            page.route("**/api/config/read", lambda route: route.fulfill(json={"success": True, "ubuntu_host": "127.0.0.1"}))
            # All config subpanels use isolated GET fixtures. Otherwise a 403
            # may unexpectedly open the elevation dialog over the screenshot.
            page.route("**/api/config/client-ssh-credentials", lambda route: route.fulfill(
                json={"success": True, "credentials": []}
            ))
            page.route("**/api/config/static-routes", lambda route: route.fulfill(
                json={"success": True, "data": {"enabled": False, "routes": []}}
            ))
            page.route("**/api/email/smtp", lambda route: route.fulfill(
                json={"success": True, "data": {"enabled": False}}
            ))
            page.route("**/api/email/outbox?*", lambda route: route.fulfill(
                json={"success": True, "data": {"items": [], "total": 0}}
            ))
            page.route("**/api/security-audit/detail/*", lambda route: route.fulfill(
                json={"success": True, "data": {
                    "record": {"id": "fixture-audit-01", "timestamp": "2026-11-17T00:00:00Z",
                               "source": "ui-fixture", "operation": "render verification",
                               "method": "GET", "path": "/fixture/readonly", "status_code": 200},
                    "related_logs": {"recent_client_logs": ["render fixture: read-only"]}
                }}
            ))
            page.route("**/api/email/outbox/fixture-*", lambda route: route.fulfill(
                json={"success": True, "data": {
                    "id": "fixture-mail-01", "created_at": "2026-11-17T00:00:00Z",
                    "channel": "manual", "username": "ui-fixture", "sent": True,
                    "to_addrs": ["reader@example.invalid"], "subject": "邮件详情布局验证",
                    "body_preview": "此内容仅用于隔离的 Playwright 弹框预览。"
                }}
            ))
            ids = page.locator('.modal[id]').evaluate_all('els => els.map(el => el.id)')
            self.assertGreaterEqual(len(ids), 41)
            for viewport in [{"width": 1672, "height": 941}, {"width": 390, "height": 844}]:
                page.set_viewport_size(viewport)
                page.evaluate("""() => {
                    document.getElementById('firmware-target-devices').textContent = 'fixture-device-01';
                    window.currentReportAnalysisData = {summary:{total:1240,pass:1216,fail:18},details:{test_type:'CTS'}};
                }""")
                page.evaluate("renderReportDiagnosis", DIAGNOSIS)
                for modal_id in ids:
                    with self.subTest(modal=modal_id, viewport=viewport):
                        page.evaluate('(id) => { ModalManager.closeAll(); ModalManager.open(id); }', modal_id)
                        # These two dialogs require their real controllers/data to
                        # build meaningful content. Opening only the bare template
                        # produces deceptively blank screenshots.
                        if modal_id == 'sidebar-visibility-modal':
                            page.evaluate("openSidebarVisibilityModal('system', 'runtime')")
                            expect(page.locator('#system-config-body input').first).to_be_visible()
                            expect(page.locator('#system-config-footer')).to_be_visible()
                        elif modal_id == 'guide-image-modal':
                            page.evaluate("""() => {
                                const image = document.getElementById('guide-image-preview');
                                const fixture = '<svg xmlns="http://www.w3.org/2000/svg" width="800" height="450"><rect width="800" height="450" fill="#252038"/><rect x="95" y="90" width="610" height="270" rx="16" fill="#42335b" stroke="#c28bf5" stroke-width="4"/><text x="400" y="240" text-anchor="middle" fill="#f2e0ff" font-size="32">GMS UI fixture</text></svg>';
                                image.src = 'data:image/svg+xml,' + encodeURIComponent(fixture);
                                image.alt = '隔离测试图片';
                                document.getElementById('guide-image-caption').textContent = '仅用于校验图片预览、缩放与关闭的渲染布局';
                            }""")
                            page.wait_for_function(
                                "document.getElementById('guide-image-preview').naturalWidth === 800"
                            )
                        elif modal_id == 'test-results-modal':
                            page.evaluate("""() => {
                                renderTestResults([
                                    {session: 'fixture-session-01', pass: 1216, fail: 18,
                                     project: 'RK3588 fixture', result_directory: 'fixture-results'}
                                ], ['Session', 'Pass', 'Fail', 'Result Directory', 'Project']);
                                document.getElementById('test-results-modal-status').textContent =
                                    '共 1 条测试结果 · 仅用于渲染验收';
                            }""")
                            expect(page.locator('#test-results-tbody tr').first).to_be_visible()
                        capture('shell-' + modal_id, f'#{modal_id} > .modal-content')
                        if modal_id == 'weekly-report-modal':
                            # A typo in ":first-of-type" previously disabled the
                            # toolbar's grouped panel border without tripping geometry.
                            expect(page.locator('#weekly-report-modal .modal-body > div').first).to_have_css(
                                'border-top-width', '1px'
                            )
                        page.evaluate('(id) => ModalManager.close(id)', modal_id)
                for code in [
                    'selectReportSource()',
                    "showRedmineAuthDialog('https://example.invalid/issues/1',null,null,null,null,{})",
                    "createAnalysisModal('gallery', 'Agent 分析', '正在读取分析结果')",
                    "openRedmineReplyModal('CtsNetTestCases','testConnection',0,'123')",
                    "showSecurityAuditDetail('fixture-audit-01')",
                    "showOutboxDetail('fixture-mail-01')",
                    'displayAIAnalysis({analysis:"渲染验证数据",suggestions:["检查连接"]},"testConnection")',
                ]:
                    page.evaluate(code)
                    modal_id = page.locator('.modal.show').last.get_attribute('id')
                    with self.subTest(dynamic=modal_id, viewport=viewport):
                        capture(modal_id, f'#{modal_id} .modal-content')
                        if modal_id == 'security-audit-detail-modal':
                            expect(page.locator('#security-audit-detail-body')).to_contain_text('基本信息')
                            self.assertNotIn('加载中', page.locator('#security-audit-detail-body').inner_text())
                        if modal_id == 'outbox-detail-modal':
                            expect(page.locator('#outbox-detail-body')).to_contain_text('邮件详情布局验证')
                            self.assertNotIn('加载中', page.locator('#outbox-detail-body').inner_text())
                    page.evaluate('ModalManager.closeAll()')
                page.evaluate('checkRouting()')
                expect(page.locator('#route-check-dialog')).to_be_visible()
                capture('route-check', '#route-check-dialog .modal-content')
                page.evaluate('ModalManager.closeAll()')

            for path in ['/redmine-agent', '/gerrit-dashboard', '/cluster', '/devices-console', '/gms-update-center']:
                page.goto(self.base_url + path, wait_until='domcontentloaded')
                ids = page.locator('.modal[id],.modal-backdrop[id]').evaluate_all('els => els.map(el => el.id)')
                for viewport in [{"width": 1672, "height": 941}, {"width": 390, "height": 844}]:
                    page.set_viewport_size(viewport)
                    for modal_id in ids:
                        with self.subTest(path=path, modal=modal_id, viewport=viewport):
                            page.evaluate("""id => {
                                const el = document.getElementById(id);
                                if (el instanceof HTMLDialogElement) {
                                    el.showModal();
                                } else if (el.classList.contains('modal-backdrop')) {
                                    el.hidden = false;
                                    window.syncClusterModalState?.();
                                } else EmbeddedModalController.open(id);
                            }""", modal_id)
                            capture(path.strip('/') + '-' + modal_id, f'#{modal_id} .modal-content')
                            page.evaluate("""id => {
                                const el = document.getElementById(id);
                                if (el instanceof HTMLDialogElement) {
                                    el.close();
                                } else if (el.classList.contains('modal-backdrop')) {
                                    el.hidden = true;
                                    window.syncClusterModalState?.();
                                } else EmbeddedModalController.close(id);
                            }""", modal_id)
            page.goto(self.base_url + '/redmine-agent', wait_until='domcontentloaded')
            page.route('**/api/redmine-agent/**', lambda route: route.fulfill(json={"success": True, "turns": [], "events": [], "status": "completed"}))
            for viewport in [{"width": 1672, "height": 941}, {"width": 390, "height": 844}]:
                page.set_viewport_size(viewport)
                page.evaluate("dailyBriefCache = {issues:[{issue_id:123,ai_statistics:{execution_count:1}}]}")
                for code in [
                    'openSingleIssueAnalysisHint()',
                    "openRedmineReplyModal('123','渲染测试回复',{summary:'渲染测试'})",
                    'showDailyBriefIssueStatistics(123)',
                    'showDailyBriefIssueFallback(123)',
                    "showAnalysisTimelineModal('fixture-run',123,{history:true})",
                    "openSingleIssueReportModal({run:{status:'completed'},issues:[{issue_id:123,subject:'渲染测试',ai_statistics:{execution_count:1}}]},true)",
                    "showIssueFullSession('fixture-run',123,'渲染测试')",
                ]:
                    page.evaluate(code)
                    modal_id = page.locator('.modal.show').last.get_attribute('id')
                    capture('redmine-dynamic-' + modal_id, f'#{modal_id} .modal-content')
                    page.evaluate('(id) => EmbeddedModalController.remove(id)', modal_id)
            self.assert_no_page_errors(errors)
        finally:
            if output:
                (output / 'results.json').write_text(json.dumps(records, ensure_ascii=False, indent=2))
                cards = ''.join(f'<figure><figcaption>{html.escape(r["id"])} · {r["viewport"]["width"]}px</figcaption><a href="{r["file"]}"><img loading="lazy" src="{r["file"]}"></a></figure>' for r in records)
                (output / 'index.html').write_text('<!doctype html><meta charset="utf-8"><title>弹框实际渲染核验</title><style>body{background:#15111f;color:#e9def5;font:14px sans-serif;padding:24px}main{display:grid;grid-template-columns:repeat(auto-fit,minmax(340px,1fr));gap:20px}figure{margin:0;padding:12px;border:1px solid #64477c}img{width:100%}figcaption{padding:8px}</style><h1>弹框实际渲染核验</h1><p>隔离环境 · 渲染测试数据 · 桌面与手机尺寸 · 未执行设备操作</p><main>' + cards + '</main>')
            page.close()

    def test_redmine_internal_issue_form_labels_are_not_squeezed(self):
        """Metadata fields must not become three 100px columns on mobile."""
        page = self.new_page()
        try:
            for viewport in [
                {"width": 1672, "height": 941},
                {"width": 390, "height": 844},
                {"width": 844, "height": 390},
            ]:
                with self.subTest(viewport=viewport):
                    page.set_viewport_size(viewport)
                    page.goto(self.base_url + "/redmine-agent", wait_until="domcontentloaded")
                    page.wait_for_function("typeof EmbeddedModalController !== 'undefined'")
                    page.evaluate("EmbeddedModalController.open('internalCreateModal')")
                    modal = page.locator("#internalCreateModal .modal-content")
                    expect(modal).to_be_visible()
                    fields = page.locator(
                        "#internalCreateModal .redmine-internal-create-meta-grid > .tech-field-card"
                    ).evaluate_all("""nodes => nodes.map(node => {
                        const label = node.querySelector('label');
                        const input = node.querySelector('input');
                        const r = node.getBoundingClientRect();
                        const l = label.getBoundingClientRect();
                        const i = input.getBoundingClientRect();
                        return {text: label.textContent.trim(),
                                cardTop: r.top, cardBottom: r.bottom,
                                cardWidth: r.width, labelHeight: l.height,
                                labelBottom: l.bottom, inputTop: i.top,
                                inputBottom: i.bottom,
                                inputWidth: i.width};
                    })""")
                    self.assertEqual(len(fields), 3, fields)
                    self.assertEqual(
                        [item["text"] for item in fields],
                        ["Tracker ID", "优先级 ID", "指派人 ID"],
                    )
                    for item in fields:
                        self.assertGreater(item["cardWidth"], 160, item)
                        self.assertGreater(item["inputWidth"], 100, item)
                        self.assertLessEqual(item["labelHeight"], 26, item)
                        self.assertLessEqual(item["inputBottom"], item["cardBottom"] + 1, item)
                    for above, below in itertools.pairwise(fields):
                        self.assertLessEqual(above["cardBottom"], below["cardTop"] + 1, fields)
        finally:
            page.close()

    def test_redmine_settings_field_cards_never_overlap(self):
        """Each tall settings card must keep its controls inside its own border.

        The generic modal viewport checks cannot detect overlapping siblings,
        which previously let Redmine settings inputs sit underneath next labels.
        """
        page = self.new_page()
        try:
            for viewport in [
                {"width": 1672, "height": 941},
                {"width": 390, "height": 844},
                {"width": 844, "height": 390},
            ]:
                with self.subTest(viewport=viewport):
                    page.set_viewport_size(viewport)
                    page.goto(self.base_url + "/redmine-agent?settings=1", wait_until="domcontentloaded")
                    content = page.locator("#redmine-system-settings")
                    expect(content).to_be_visible()
                    rows = page.locator("#redmine-system-settings fieldset > .tech-field-stack").evaluate_all(
                        """nodes => nodes.map(node => {
                          const parent = node.getBoundingClientRect();
                          const children = [...node.children]
                            .filter(el => getComputedStyle(el).display !== 'none')
                            .map(el => el.getBoundingClientRect());
                          return {label: node.querySelector('label')?.textContent?.trim(),
                                  top: parent.top, bottom: parent.bottom,
                                  childBottom: Math.max(...children.map(box => box.bottom))};
                        })"""
                    )
                    self.assertGreaterEqual(len(rows), 8, rows)
                    for row in rows:
                        self.assertLessEqual(row["childBottom"], row["bottom"] + 1, row)
                    for above, below in itertools.pairwise(rows):
                        self.assertLessEqual(above["bottom"], below["top"] + 1, [above, below])
                    scrolling = page.locator("html").evaluate(
                        """el => { el.scrollTop = el.scrollHeight; return {
                          scrollTop: el.scrollTop, scrollHeight: el.scrollHeight,
                          clientHeight: el.clientHeight}; }"""
                    )
                    self.assertGreater(scrolling["scrollHeight"], scrolling["clientHeight"])
                    self.assertGreater(scrolling["scrollTop"], 0)
        finally:
            page.close()

    def test_async_input_forms_cancel_validate_and_keep_focus(self):
        page = self.new_page()
        try:
            self.goto_shell(page)
            self.close_initial_modals(page)
            page.evaluate("""() => {
                window.promptResult = 'pending';
                GmsDialogs.form('发送邮件', [{name:'to',label:'收件人',required:true},{name:'cc',label:'抄送'}])
                  .then(result => window.promptResult = result);
            }""")
            modal = page.locator('.modal.show')
            expect(modal.locator('input').first).to_be_focused()
            modal.locator('[type=submit]').click()
            self.assertEqual(page.evaluate('window.promptResult'), 'pending')
            modal.locator('input').first.fill('reader@example.invalid')
            modal.locator('input').nth(1).fill('copy@example.invalid')
            modal.locator('[type=submit]').click()
            page.wait_for_function("window.promptResult !== 'pending'")
            self.assertEqual(page.evaluate('window.promptResult'), {'to':'reader@example.invalid','cc':'copy@example.invalid'})
            for close in ['escape', 'backdrop', 'button']:
                page.evaluate("""() => {
                    window.promptResult = 'pending';
                    GmsDialogs.form('<img src=x onerror=alert(1)>', [{name:'name',label:'<script>untrusted</script>'}])
                      .then(value => window.promptResult = value);
                }""")
                expect(page.locator('.modal.show img,.modal.show script')).to_have_count(0)
                if close == 'escape':
                    page.keyboard.press('Escape')
                elif close == 'backdrop':
                    page.mouse.click(2, 2)
                else:
                    page.locator('.modal.show [data-cancel]').click()
                page.wait_for_function("window.promptResult === null")
                expect(page.locator('.modal.show')).to_have_count(0)
            # Standalone pages without an overlay controller use a native focus trap.
            page.goto(self.base_url + '/devices-console', wait_until='domcontentloaded')
            page.evaluate("document.getElementById('binding-modal').hidden = false")
            page.evaluate("window.confirmResult = null; void GmsDialogs.confirm('删除绑定','仅验证取消流程').then(v => window.confirmResult = v)")
            expect(page.locator('dialog[open]')).to_be_visible()
            page.keyboard.press('Escape')
            page.wait_for_function('window.confirmResult === false')
            expect(page.locator('dialog[open]')).to_have_count(0)
            self.assertFalse(page.locator('#binding-modal').evaluate('el => el.hidden'))
        finally:
            page.close()

    def test_diagnosis_panels_keep_long_content_separate_and_scrollable(self):
        page = self.new_page()
        try:
            self.goto_shell(page)
            self.close_initial_modals(page)
            page.evaluate('renderReportDiagnosis', {
                **DIAGNOSIS,
                'stack_trace': 'at fixture.Class.method(Rendering.java:12)\n' * 100,
                'ai_result': {**DIAGNOSIS['ai_result'], 'suggestions': ['保留日志并核查网络。'] * 40},
            })
            page.evaluate("ModalManager.open('report-diagnosis-modal')")
            modal = page.locator('#report-diagnosis-modal')
            body = modal.locator('.report-diagnosis-body')
            for viewport in [
                {'width': 1440, 'height': 960}, {'width': 768, 'height': 720},
                {'width': 390, 'height': 844}, {'width': 844, 'height': 390},
            ]:
                with self.subTest(viewport=viewport):
                    page.set_viewport_size(viewport)
                    body.evaluate('el => el.scrollTop = 0')
                    summary = modal.locator('.report-diagnosis-summary').bounding_box()
                    result = modal.locator('.report-diagnosis-result').bounding_box()
                    if viewport['width'] <= 800:
                        self.assertLessEqual(summary['y'] + summary['height'], result['y'])
                        # All summary cards must fit before the following panel.
                        last_summary_card = modal.locator('#report-diagnostic-summary > :last-child').bounding_box()
                        self.assertLessEqual(last_summary_card['y'] + last_summary_card['height'], result['y'])
                        scroll = body
                    else:
                        self.assertLessEqual(summary['x'] + summary['width'], result['x'])
                        scroll = modal.locator('#report-diagnostic-result')
                    scroll.evaluate('el => el.scrollTop = el.scrollHeight')
                    self.assertGreater(scroll.evaluate('el => el.scrollTop'), 0)
                    last_result_card = modal.locator('#report-diagnostic-result > :last-child').bounding_box()
                    visible_body = body.bounding_box()
                    footer = modal.locator('.modal-buttons').bounding_box()
                    self.assertLessEqual(last_result_card['y'] + last_result_card['height'], visible_body['y'] + visible_body['height'] + 1)
                    self.assertLessEqual(visible_body['y'] + visible_body['height'], footer['y'] + 1)
        finally:
            page.close()


    def test_firmware_aoa_labels_and_inputs_stay_on_the_same_row(self):
        page = self.new_page()
        try:
            self.goto_shell(page)
            self.close_initial_modals(page)
            page.evaluate("ModalManager.open('firmware-modal')")
            palette = page.locator('#firmware-modal').evaluate("""modal => {
                const theme = getComputedStyle(document.documentElement);
                const token = name => {
                    const probe = document.createElement('span');
                    probe.style.color = theme.getPropertyValue(name).trim();
                    return probe.style.color;
                };
                return {surface: getComputedStyle(modal.querySelector('.modal-content')).backgroundColor,
                    shellSurface: token('--light-bg'),
                    primary: getComputedStyle(modal.querySelector('.modal-buttons .btn-primary')).backgroundColor,
                    shellPrimary: token('--primary-color')};
            }""")
            # The reference style deliberately uses a deeper violet than
            # the neutral shell tokens. Protect the actual design colors.
            self.assertEqual(palette['surface'], 'rgb(28, 25, 41)')
            self.assertEqual(palette['primary'], 'rgb(133, 67, 196)')
            for viewport in [{"width": 1440, "height": 960}, {"width": 768, "height": 720}, {"width": 390, "height": 844}]:
                page.set_viewport_size(viewport)
                for input_id in ['firmware-aoa-profile', 'firmware-aoa-boot-wait']:
                    with self.subTest(viewport=viewport, field=input_id):
                        label = page.locator(f'label[for="{input_id}"]').bounding_box()
                        field = page.locator('#' + input_id).bounding_box()
                        self.assertAlmostEqual(label['y'] + label['height'] / 2, field['y'] + field['height'] / 2, delta=2)
                        self.assertGreater(field['x'], label['x'] + label['width'])
        finally:
            page.close()
