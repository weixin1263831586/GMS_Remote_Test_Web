"""Agent entry probes once, shares in-flight work and keeps manual retry available."""

from tests.runtime_ui.harness import RuntimeUiHarness, expect


class RuntimeAgentModelProbeTests(RuntimeUiHarness):
    def test_entry_auto_probes_once_without_toast_and_manual_retry_still_works(self):
        page = self.new_page()
        try:
            self.goto_shell(page)
            page.evaluate("""() => {
                const originalFetch = window.fetch;
                window.modelProbeRequests = [];
                window.modelProbeToasts = [];
                const originalToast = window.showToast;
                window.showToast = (message, ...args) => {
                    if (/模型/.test(message)) modelProbeToasts.push(message);
                    else originalToast(message, ...args);
                };
                window.fetch = (input, options) => {
                    if (String(input).startsWith('/api/config/ai')) {
                        modelProbeRequests.push(String(input));
                        return new Promise(resolve => {window.finishModelProbe = () => resolve(new Response(JSON.stringify({
                            success: true, data: {status: {local_provider: 'local', providers: [{
                                provider: 'local', name: 'Local', model: 'test-model', local: true,
                                configured: true, credential_configured: true, available: true, state: 'available', latency_ms: 42,
                            }]}}
                        }), {headers: {'Content-Type': 'application/json'}}));});
                    }
                    return originalFetch(input, options);
                };
                switchPage('agent', null);
                initAgentPage();
                initAgentPage();
            }""")
            expect(page.locator('#agent-model-check')).to_be_disabled()
            expect(page.locator('#agent-model-status')).to_have_text('分析模型：检测中…')
            assert page.evaluate('modelProbeRequests') == ['/api/config/ai?probe=true']
            page.evaluate('finishModelProbe()')
            expect(page.locator('#agent-model-status')).to_have_text('本地模型：可用 · 42ms')
            expect(page.locator('#agent-model-check')).to_be_enabled()
            assert page.evaluate('modelProbeToasts') == []
            page.evaluate("switchPage('test', null); switchPage('agent', null)")
            assert page.evaluate('modelProbeRequests.length') == 1
            page.locator('#agent-model-check').click()
            expect(page.locator('#agent-model-check')).to_be_disabled()
            assert page.evaluate('modelProbeRequests') == ['/api/config/ai?probe=true', '/api/config/ai?probe=true']
            page.evaluate('finishModelProbe()')
            expect(page.locator('#agent-model-check')).to_be_enabled()
            assert page.evaluate('modelProbeToasts') == ['AI 分析模型可用']
        finally:
            page.close()
