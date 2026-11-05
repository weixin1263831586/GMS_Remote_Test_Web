"""Semantic authoring is user-triggered and survives repeated workbench navigation."""
import json

from playwright.sync_api import expect

from tests.aoa_semantic_fixtures import semantic_template
from tests.runtime_ui.harness import RuntimeUiHarness


class RuntimeAoaSemanticTests(RuntimeUiHarness):
    def test_template_apply_and_single_step_preserve_state_conditions(self):
        page, requests = self.new_page(), []
        def reply(route):
            request = route.request
            if request.method != "GET":
                requests.append((request.url, request.post_data_json))
            if request.url.endswith('/recover'):
                payload = {"success": True, "commands_executed": 1, "verified_steps": 1, "native_mode_restored": True}
            else:
                payload = {"success": True, "profiles": [], "certificates": []}
            route.fulfill(content_type='application/json', body=json.dumps(payload))
        try:
            page.route('**/api/devices/aoa/**', reply)
            self.goto_shell(page)
            page.evaluate("""async () => {
                switchPage('devices');
                window.aoaLiveScreen = {start: async () => {}, stop: () => {}, isActive: () => true};
                await aoaViewToggle(true);
                aoaSyncDeviceSources([{serial_no:'TARGET', worker_id:'w', state:'available'}]);
                aoaPanelState.commands = ['click 1 1']; aoaRenderCommands();
            }""")
            page.locator('.aoa-semantic-tools summary').click()
            page.locator('#aoa-semantic-template').fill(json.dumps(semantic_template()))
            page.locator('#aoa-template-certificates').fill('owned-cert')
            page.locator('[data-click="aoaTemplateApply"]').click()
            expect(page.locator('#aoa-hid-touch-mode')).to_have_value('direct')
            expect(page.locator('#aoa-hid-session-mode')).to_have_value('per_command')
            assert requests == []
            page.locator('#aoa-debug-keep').check()
            page.locator('.aoa-command-execute').click()
            expect(page.locator('#aoa-run-status')).to_contain_text('第 1 条执行完成')
            recover = [body for url, body in requests if url.endswith('/recover')]
            assert len(recover) == 1
            assert recover[0]['metadata']['semantic_template']['steps'] == semantic_template()['steps']
            expect(page.locator('.aoa-command-execute')).to_have_text('执行')
            page.evaluate("switchPage('reports'); switchPage('devices')")
            page.evaluate("aoaTemplateRead()")
            assert json.loads(page.locator('#aoa-semantic-template').input_value()) == semantic_template()
            assert len(requests) == 1
        finally:
            page.close()

    def test_semantic_progress_preserves_command_button_geometry(self):
        page = self.new_page()
        try:
            self.goto_shell(page)
            page.evaluate("""async () => {
                switchPage('devices');
                window.aoaLiveScreen = {start: async () => {}, stop: () => {}, isActive: () => true};
                await aoaViewToggle(true);
                aoaPanelState.commands = ['click 180 481'];
                aoaPanelState.playbackActive = true;
                aoaPanelState.playbackStates = ['pending'];
                aoaRenderCommands();
            }""")
            button = page.locator('.aoa-command-execute')
            before = button.bounding_box()
            for phase, label in [('checking', '检查页面'), ('verifying', '验证结果'), ('skipped', '已跳过')]:
                page.evaluate("phase => aoaApplyPlaybackEvent({phase, index:1, total:1, command:'click 180 481'})", phase)
                expect(page.locator('.aoa-command-state')).to_have_text(label)
                assert button.bounding_box() == before
                expect(button).to_have_text('执行')
        finally:
            page.close()
