"""Dialog content, overflow, async cancellation and rendered review gallery."""
from __future__ import annotations

import html
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
        self.assertTrue(report["alignedCorners"], report)
        self.assertFalse(report["ticksOverlapClose"], report)
        self.assertEqual(report["leaks"], [], report)
        self.assertEqual(report["clipped"], [], report)
        for footer in report["footer"]:
            self.assertLessEqual(footer["bottom"], bounds["bottom"] + 1, report)
        return report

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
            ids = page.locator('.modal[id]').evaluate_all('els => els.map(el => el.id)')
            self.assertGreaterEqual(len(ids), 42)
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
                        capture('shell-' + modal_id, f'#{modal_id} > .modal-content')
                        page.evaluate('(id) => ModalManager.close(id)', modal_id)
                for code in [
                    'selectReportSource()',
                    "showRedmineAuthDialog('https://example.invalid/issues/1',null,null,null,null,{})",
                    "createAnalysisModal('gallery', 'Agent 分析', '正在读取分析结果')",
                    "openRedmineReplyModal('CtsNetTestCases','testConnection',0,'123')",
                    'ensureSecurityAuditDetailModal(); ModalManager.open("security-audit-detail-modal")',
                    'ensureOutboxDetailModal(); ModalManager.open("outbox-detail-modal")',
                    'displayAIAnalysis({analysis:"渲染验证数据",suggestions:["检查连接"]},"testConnection")',
                ]:
                    page.evaluate(code)
                    modal_id = page.locator('.modal.show').last.get_attribute('id')
                    with self.subTest(dynamic=modal_id, viewport=viewport):
                        capture(modal_id, f'#{modal_id} .modal-content')
                    page.evaluate('ModalManager.closeAll()')
                page.evaluate('checkRouting()')
                expect(page.locator('#route-check-dialog')).to_be_visible()
                capture('route-check', '#route-check-dialog .modal-content')
                page.evaluate('ModalManager.closeAll()')

            for path in ['/redmine-agent', '/gerrit-dashboard', '/cluster', '/devices-console', '/gms-update-monitor']:
                page.goto(self.base_url + path, wait_until='networkidle')
                ids = page.locator('.modal[id],.modal-backdrop[id]').evaluate_all('els => els.map(el => el.id)')
                for viewport in [{"width": 1672, "height": 941}, {"width": 390, "height": 844}]:
                    page.set_viewport_size(viewport)
                    for modal_id in ids:
                        with self.subTest(path=path, modal=modal_id, viewport=viewport):
                            page.evaluate("""id => {
                                const el = document.getElementById(id);
                                if (el.classList.contains('modal-backdrop')) {
                                    el.hidden = false;
                                    window.syncClusterModalState?.();
                                } else EmbeddedModalController.open(id);
                            }""", modal_id)
                            capture(path.strip('/') + '-' + modal_id, f'#{modal_id} .modal-content')
                            page.evaluate("""id => {
                                const el = document.getElementById(id);
                                if (el.classList.contains('modal-backdrop')) {
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
            self.assertEqual(palette['surface'], palette['shellSurface'])
            self.assertEqual(palette['primary'], palette['shellPrimary'])
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
