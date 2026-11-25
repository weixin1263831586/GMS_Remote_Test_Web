"""Daily radar settings, tool entry, change evidence, and explicit mail actions."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from features.gms.radar.repository import RadarStore
from tests.runtime_ui.harness import RuntimeUiHarness, expect


class RuntimeUpdateRadarTests(RuntimeUiHarness):
    def test_manual_scan_explains_access_and_rechecks_after_account_switch(self):
        page = self.new_page()
        restricted = {'enabled': True}
        requested_settings = []

        def status(route):
            if restricted['enabled']:
                route.fulfill(content_type='application/json', body=json.dumps({
                    'success': True,
                    'data': {'enabled': False, 'scan_time': '04:00',
                             'timezone': 'Asia/Shanghai', 'next_run_at': None,
                             'tasks': ['packages'], 'can_manage': False},
                }))
            else:
                route.continue_()

        try:
            page.on('request', lambda request: requested_settings.append(request.url)
                    if '/api/gms-update-radar/settings' in request.url else None)
            page.route('**/api/gms-update-radar/status', status)
            page.goto(self.base_url + '/gms-update-radar', wait_until='domcontentloaded')
            expect(page.locator('#scan')).to_be_disabled()
            expect(page.locator('#modify')).to_be_disabled()
            expect(page.locator('#message')).to_contain_text('请在主页面切换管理员账号')
            self.assertIn('管理员账号', page.locator('#scan').get_attribute('title'))
            self.assertEqual(requested_settings, [], 'read-only page must not probe private settings')

            # Refresh must recover permissions without reloading the whole page.
            restricted['enabled'] = False
            page.locator('#refresh').click()
            expect(page.locator('#scan')).to_be_enabled()
            expect(page.locator('#modify')).to_be_enabled()
            expect(page.locator('#message')).not_to_contain_text('切换管理员账号')
            self.assertEqual(requested_settings, [])

            # An active server-side scan still disables manual triggering.
            store = RadarStore(Path(self.runtime_dir.name) / 'gms_update_radar_v2.sqlite3')
            run_id = store.claim('manual', owner_id='ui-test')
            self.assertIsNotNone(run_id)
            page.locator('#refresh').click()
            expect(page.locator('#scan')).to_be_disabled()
            self.assertIn('正在运行', page.locator('#scan').get_attribute('title'))
            store.finish(run_id, 'completed',
                         {'categories': {}, 'errors': {}, 'changes_total': 0, 'tasks': []}, [])
            page.locator('#refresh').click()
            expect(page.locator('#scan')).to_be_enabled()
        finally:
            page.close()

    def test_radar_settings_history_and_mail_actions_after_reentry(self):
        page = self.new_page()
        popup = None
        writes = []
        page.context.on('request', lambda request: writes.append((request.method, request.url))
                        if '/api/gms-update-radar' in request.url and request.method not in ('GET', 'HEAD') else None)
        try:
            self.goto_shell(page)
            for _ in range(2):
                page.evaluate("switchPage('tools', null)")
                expect(page.get_by_text('GMS更新雷达', exact=True)).to_have_count(1)
                page.evaluate("switchPage('test', null)")
            page.evaluate("switchPage('tools', null)")
            # 编辑按钮由 tools 页 data-tools-action 委托渲染（utility-tools.js）。
            page.locator('[data-tools-action="radar-edit"]').click()
            expect(page.locator('#radar-settings-modal')).to_be_visible()
            expect(page.locator('#radar-task-count')).to_have_text('已选择 7 / 7 项')
            page.keyboard.press('Escape')
            expect(page.locator('#radar-settings-modal')).to_be_hidden()
            self.assertEqual(writes, [])
            with page.expect_popup() as opened:
                page.locator('.tool-card').filter(has=page.get_by_text('GMS更新雷达', exact=True)).click()
            popup = opened.value
            popup.wait_for_load_state('domcontentloaded')
            expect(popup.locator('#scan')).to_be_enabled()
            self.assertEqual(writes, [])
            popup.get_by_role('button', name='修改', exact=True).click()
            expect(popup.locator('#radar-settings-modal')).to_be_visible()
            expect(popup.locator('#radar-time')).to_have_value('04:00')
            expect(popup.locator('#radar-notify')).to_have_value('changes')
            popup.locator('#radar-recipients').fill('team@example.test; second@example.test')
            popup.locator('#radar-all-tasks').uncheck()
            popup.locator('[name="radar-task"][value="packages"]').check()
            popup.locator('[name="radar-task"][value="exemptions"]').check()
            expect(popup.locator('#radar-task-count')).to_have_text('已选择 2 / 7 项')
            self.assertTrue(popup.locator('#radar-all-tasks').evaluate('node => node.indeterminate'))
            future = (datetime.now(timezone.utc) + timedelta(hours=12)).astimezone(ZoneInfo('Asia/Shanghai')).strftime('%H:%M')
            popup.locator('#radar-time').fill(future)
            popup.locator('#radar-enabled').check()
            popup.get_by_role('button', name='保存修改').click()
            expect(popup.locator('#radar-settings-modal')).to_be_hidden()
            expect(popup.locator('#message')).to_contain_text('已保存')
            self.assertEqual([method for method, _ in writes], ['PUT'])
            popup.reload(wait_until='domcontentloaded')
            expect(popup.locator('#scan')).to_be_enabled()
            popup.get_by_role('button', name='修改', exact=True).click()
            expect(popup.locator('#radar-time')).to_have_value(future)
            expect(popup.locator('#radar-enabled')).to_be_checked()
            expect(popup.locator('#radar-notify')).to_have_value('changes')
            expect(popup.locator('#radar-task-count')).to_have_text('已选择 2 / 7 项')
            popup.keyboard.press('Escape')
            expect(popup.locator('#modify')).to_be_focused()
            self.assertEqual([method for method, _ in writes], ['PUT'])

            store = RadarStore(Path(self.runtime_dir.name) / 'gms_update_radar_v2.sqlite3')
            run_id = store.claim('manual', owner_id='ui-test')
            summary = {'categories': {key: {'before': 1, 'after': 1, 'added': 0, 'modified': 0, 'removed': 0}
                                      for key in ('packages', 'exemptions')}, 'errors': {}, 'changes_total': 1,
                       'tasks': ['packages', 'exemptions']}
            summary['categories']['packages']['modified'] = 1
            evidence = '<script>window.radarEvidenceExecuted=true</script>'
            store.finish(run_id, 'completed', summary, [{'category': 'packages', 'key': 'demo', 'type': 'modified',
                                                       'before': {'file_name': 'old-demo.zip'}, 'after': {'file_name': evidence}}])
            store.mark_email(run_id, 'failed', 'SMTP 测试失败，记录已保留')

            def scan(route):
                route.fulfill(content_type='application/json', body=json.dumps({'success': True, 'data': {'run_id': run_id}}))

            popup.route('**/api/gms-update-radar/scan', scan)
            popup.locator('#scan').click()
            expect(popup.locator('#detail-title')).to_contain_text(f'扫描 #{run_id}')
            expect(popup.locator('#mail-error')).to_contain_text('记录已保留')
            expect(popup.locator('#counts tbody tr')).to_have_count(2)
            expect(popup.locator('#changes .radar-pill')).to_have_text('修改')
            popup.locator('#changes details summary').click()
            expect(popup.locator('#changes pre').first).to_contain_text('old-demo.zip')
            expect(popup.locator('#changes pre').last).to_contain_text(evidence)
            self.assertFalse(popup.evaluate('Boolean(window.radarEvidenceExecuted)'))
            popup.get_by_role('button', name='预览邮件').click()
            expect(popup.locator('#email-preview-html')).to_be_visible()
            expect(popup.frame_locator('#email-preview-html').get_by_text('监控项统计')).to_be_visible()
            expect(popup.frame_locator('#email-preview-html').get_by_text('old-demo.zip')).to_be_visible()
            self.assertFalse(popup.evaluate('Boolean(window.radarEvidenceExecuted)'))

            def mail(route):
                if route.request.method == 'POST':
                    store.mark_email(run_id, 'sent')
                    route.fulfill(content_type='application/json', body='{"success":true,"data":{"email_status":"sent"}}')
                else:
                    route.continue_()

            popup.route(f'**/api/gms-update-radar/runs/{run_id}/email', mail)
            popup.get_by_role('button', name='重发本次汇总').click()
            expect(popup.locator('#detail-state')).to_contain_text('已发送')
            self.assertEqual([method for method, _ in writes], ['PUT', 'POST', 'POST'])
            for width in (1440, 390):
                popup.set_viewport_size({'width': width, 'height': 900})
                self.assertTrue(popup.evaluate('document.documentElement.scrollWidth <= innerWidth'))
            popup.locator('#modify').focus()
            popup.keyboard.press('Enter')
            expect(popup.locator('#radar-settings-modal')).to_be_visible()
            self.assertTrue(popup.locator('#radar-settings-modal .modal-content').evaluate(
                'node => node.getBoundingClientRect().right <= innerWidth'
            ))
            popup.keyboard.press('Escape')
            expect(popup.locator('#modify')).to_be_focused()
        finally:
            if popup:
                popup.close()
            page.close()
