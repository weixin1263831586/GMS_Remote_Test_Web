"""Clipboard image composer browser regression."""

import json

from tests.runtime_ui.harness import RuntimeUiHarness, expect


class RuntimeAgentImageInputTests(RuntimeUiHarness):
    def test_paste_preview_remove_and_image_only_send(self):
        page = self.new_page()
        requests = []
        try:
            self.goto_shell(page)
            page.evaluate("switchPage('agent', null)")
            editor = page.locator("#agent-input")
            expect(editor).to_be_visible()

            def chat(route):
                data = route.request.post_data_json
                requests.append(data)
                route.fulfill(status=200, content_type="application/json", body=json.dumps({
                    "success": True, "data": {"session": {"session_id": "image-test",
                        "status": "idle", "messages": [{"id": "u", "role": "user",
                        "content": "请分析图片内容。", "data": {"image_count": 1}},
                        {"id": "a", "role": "assistant", "content": "image result"}], "steps": []}},
                }))

            page.route("**/api/agent/chat", chat)
            paste = """() => {
                const data = new DataTransfer();
                data.items.add(new File([new Uint8Array([137,80,78,71])], 'clipboard.png', {type:'image/png'}));
                document.getElementById('agent-input').dispatchEvent(new ClipboardEvent('paste', {
                    clipboardData: data, bubbles: true, cancelable: true
                }));
            }"""
            page.evaluate(paste)
            expect(page.locator("#agent-image-previews img")).to_have_count(1)
            page.get_by_role("button", name="移除图片 1").click()
            expect(page.locator("#agent-image-previews")).to_be_hidden()
            page.evaluate(paste)
            expect(page.locator("#agent-image-previews img")).to_have_count(1)
            expect(page.locator(".agent-execute-action")).to_be_disabled()
            editor.press("Enter")
            expect(page.locator("#agent-chat-messages")).to_contain_text("image result")
            expect(page.locator("#agent-chat-messages")).to_contain_text("图片 1 张")
            expect(page.locator("#agent-image-previews")).to_be_hidden()
            self.assertEqual(len(requests), 1)
            self.assertEqual(requests[0]["message"], "")
            self.assertTrue(requests[0]["images"][0].startswith("data:image/png;base64,"))
            self.assertFalse(requests[0]["execute"])
        finally:
            page.close()
