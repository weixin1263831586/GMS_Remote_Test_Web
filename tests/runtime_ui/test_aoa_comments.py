"""Command notes survive real editing, structural changes and JSON roundtrips."""
import json
from pathlib import Path

from playwright.sync_api import expect

from tests.runtime_ui.harness import RuntimeUiHarness


class RuntimeAoaCommentTests(RuntimeUiHarness):
    def test_annotations_follow_commands_through_save_export_import_and_replay(self):
        page, writes, replays = self.new_page(), [], []
        profile = {"id": "notes", "name": "Annotated", "wait_adb_seconds": 60,
                   "commands": ["click 180 520", "sleep 800", "back", "sleep 2000"],
                   "metadata": {"command_comments": ["点击 Start", "等待 Start", "返回", "等待返回"]}}
        def reply(route):
            if route.request.method == 'GET':
                result = {"success": True, "profiles": [profile]}
            elif route.request.url.endswith('/recover'):
                replays.append(route.request.post_data_json)
                result = {"success": True, "commands_executed": 2, "adb_recovered": True}
            else:
                writes.append(route.request.post_data_json)
                profile.update(writes[-1])
                result = {"success": True, "profile": profile}
            route.fulfill(content_type='application/json', body=json.dumps(result))
        try:
            page.route('**/api/devices/aoa/**', reply)
            page.route('**/api/devices/management*', lambda route: route.fulfill(
                content_type='application/json', body=json.dumps({"success": True, "devices": [], "groups": []})))
            page.route('**/api/cluster/devices*', lambda route: route.fulfill(
                content_type='application/json', body=json.dumps({"success": True, "devices": []})))
            self.goto_shell(page)
            page.evaluate("""async () => {
                switchPage('devices'); await aoaViewToggle(true);
                window.showConfirmDialog = async () => true;
            }""")
            page.get_by_role('button', name='载入选中配置', exact=True).click()
            first = page.locator('#aoa-command-list > li').first
            before = first.bounding_box()
            first.locator('.aoa-command-comment').fill('点击 Start <img src=x onerror=alert(1)>')
            first.locator('.aoa-command-comment').press('Enter')
            expect(first.locator('.aoa-command-comment')).to_have_value('点击 Start <img src=x onerror=alert(1)>')
            assert first.bounding_box() == before
            code_box = first.locator('code').bounding_box()
            note_box = first.locator('.aoa-command-comment').bounding_box()
            assert abs(code_box['y'] + code_box['height'] / 2 - note_box['y'] - note_box['height'] / 2) < 1
            expect(first.locator('img')).to_have_count(0)
            assert writes == []
            first.get_by_role('button', name='删除', exact=True).click()
            expect(page.locator('#aoa-command-list > li').first.locator('.aoa-command-comment')).to_have_value('等待 Start')
            page.get_by_role('button', name='撤销', exact=True).click()
            expect(page.locator('#aoa-command-count')).to_have_text('1')
            page.locator('#aoa-command-input').fill('home')
            page.locator('[data-click="aoaCommandAdd"]').click()
            assert page.evaluate('aoaCurrentComments()') == ['等待 Start', '']
            page.locator('#aoa-command-list > li').last.locator('.aoa-command-comment').fill('返回桌面')
            # Saving the profile also commits an open comment editor.
            page.get_by_role('button', name='保存当前命令', exact=True).click()
            expect(page.locator('#aoa-run-status')).to_contain_text('已保存')
            assert writes[-1]['commands'] == ['sleep 800', 'home']
            assert writes[-1]['metadata']['command_comments'] == ['等待 Start', '返回桌面']
            with page.expect_download() as download:
                page.locator('[data-click="aoaCommandsExport"]').click()
            exported = json.loads(Path(download.value.path()).read_text())
            assert exported['metadata']['command_comments'] == ['等待 Start', '返回桌面']
            page.locator('[data-click="aoaCommandsClear"]').click()
            page.locator('#aoa-command-file').set_input_files({
                'name': 'annotated.json', 'mimeType': 'application/json', 'buffer': json.dumps(exported).encode()})
            page.wait_for_function("[...document.querySelectorAll('.aoa-command-comment')].map(node => node.value).join('|') === '等待 Start|返回桌面'")
            page.evaluate("aoaPanelState.profiles[0].metadata.command_comments = ['其他配置注释A', '其他配置注释B']")
            page.locator('#aoa-hid-touch-mode').select_option('direct')
            assert page.evaluate('aoaCurrentComments()') == ['等待 Start', '返回桌面']
            page.evaluate("aoaSyncDeviceSources([{serial_no:'TARGET',worker_id:'w',state:'usb_only',transport:'local_usb'}])")
            page.locator('[data-click="aoaReplaySelected"]').click()
            expect(page.locator('#aoa-run-status')).to_have_text('回放完成')
            assert replays[0]['commands'] == ['sleep 800', 'home']
            assert replays[0]['metadata']['command_comments'] == ['等待 Start', '返回桌面']
        finally:
            page.close()

    def test_new_column_order_widths_and_responsive_layout(self):
        page = self.new_page()
        try:
            page.route('**/api/devices/aoa/**', lambda route: route.fulfill(
                content_type='application/json', body=json.dumps({"success": True, "profiles": []})))
            self.goto_shell(page)
            page.evaluate("async () => {switchPage('devices'); await aoaViewToggle(true);}")
            for width, height in [(1920, 1080), (1366, 768), (1280, 720)]:
                page.set_viewport_size({'width': width, 'height': height})
                screen = page.locator('.aoa-screen-column').bounding_box()
                commands = page.locator('.aoa-command-column').bounding_box()
                config = page.locator('.aoa-config-column').bounding_box()
                assert screen['x'] < commands['x'] < config['x']
                assert screen['width'] < config['width'] < commands['width']
                assert page.locator('.aoa-config-column').evaluate('(node) => node.scrollHeight <= node.clientHeight + 1')
            page.set_viewport_size({'width': 1000, 'height': 800})
            assert page.locator('.aoa-config-column').bounding_box()['y'] > page.locator('.aoa-command-column').bounding_box()['y']
            page.set_viewport_size({'width': 640, 'height': 960})
            assert page.locator('.aoa-screen-column').bounding_box()['y'] < page.locator('.aoa-command-column').bounding_box()['y'] < page.locator('.aoa-config-column').bounding_box()['y']
        finally:
            page.close()
