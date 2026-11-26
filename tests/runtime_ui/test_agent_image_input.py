"""Clipboard image composer browser regression."""

import base64
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
                        "content": "请分析图片内容。", "data": {"image_count": 1,
                            "image_attachments": [{"id": "a" * 32, "mime": "image/png"}]}},
                        {"id": "a", "role": "assistant", "content": "image result"}], "steps": []}},
                }))

            page.route("**/api/agent/chat", chat)
            page.route("**/api/agent/sessions/image-test/images/*", lambda route: route.fulfill(
                status=200, content_type="image/png",
                body=base64.b64decode(requests[0]["images"][0].split(",", 1)[1])))
            paste = """async () => {
                const canvas = document.createElement('canvas');
                canvas.width = 8;
                canvas.height = 8;
                canvas.getContext('2d').fillRect(0, 0, 8, 8);
                const blob = await new Promise(resolve => canvas.toBlob(resolve, 'image/png'));
                const data = new DataTransfer();
                data.items.add(new File([blob], 'clipboard.png', {type:'image/png'}));
                document.getElementById('agent-input').dispatchEvent(new ClipboardEvent('paste', {
                    clipboardData: data, bubbles: true, cancelable: true
                }));
            }"""
            page.evaluate(paste)
            expect(page.locator("#agent-image-previews img")).to_have_count(1)
            page.wait_for_function("document.querySelector('#agent-image-previews img')?.naturalWidth === 8")
            preview = page.locator(".agent-image-preview")
            remove = page.get_by_role("button", name="移除图片 1")
            editor.hover()
            expect(remove).to_have_css("opacity", "0")
            expect(remove).to_have_css("pointer-events", "none")
            preview.hover()
            expect(remove).to_have_css("opacity", "1")
            expect(remove).to_have_css("position", "absolute")
            expect(remove).to_have_text("×")
            editor.hover()
            expect(remove).to_have_css("opacity", "0")
            remove.focus()
            expect(remove).to_have_css("opacity", "1")
            preview.hover()
            remove.click()
            expect(page.locator("#agent-image-previews")).to_be_hidden()
            page.evaluate(paste)
            expect(page.locator("#agent-image-previews img")).to_have_count(1)
            expect(page.locator(".agent-execute-action")).to_be_disabled()
            editor.press("Enter")
            expect(page.locator("#agent-chat-messages")).to_contain_text("image result")
            expect(page.locator("#agent-chat-messages")).to_contain_text("图片 1 张")
            attachment = page.locator(".agent-message-images button")
            expect(attachment).to_have_count(1)
            expect(attachment.locator("img")).to_have_attribute(
                "src", "/api/agent/sessions/image-test/images/" + "a" * 32)
            page.wait_for_function("document.querySelector('.agent-message-images img')?.naturalWidth === 8")
            url = page.url
            tabs = len(page.context.pages)
            attachment.click()
            dialog = page.get_by_role("dialog", name="图片预览")
            expect(dialog).to_be_visible()
            expect(dialog.locator("img")).to_be_visible()
            expect(dialog.locator("img")).to_have_attribute(
                "src", "/api/agent/sessions/image-test/images/" + "a" * 32)
            expect(dialog.locator(".modal-content, .modal-header, .tech-frame, button")).to_have_count(0)
            expect(dialog.locator("img")).to_have_css("width", "8px")
            expect(dialog.locator("img")).to_have_css("height", "8px")
            dialog.locator("img").click()
            expect(dialog).to_be_visible()
            page.keyboard.press("Escape")
            expect(dialog).to_have_count(0)
            expect(attachment).to_be_visible()
            attachment.click()
            expect(dialog).to_be_visible()
            dialog.click(position={"x": 4, "y": 4})
            expect(dialog).to_have_count(0)
            self.assertEqual(page.url, url)
            self.assertEqual(len(page.context.pages), tabs)
            expect(page.locator("#agent-image-previews")).to_be_hidden()
            self.assertEqual(len(requests), 1)
            self.assertEqual(requests[0]["message"], "")
            self.assertTrue(requests[0]["images"][0].startswith("data:image/png;base64,"))
            self.assertFalse(requests[0]["execute"])
        finally:
            page.close()
