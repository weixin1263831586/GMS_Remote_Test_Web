"""Morning-report diagnostics display truthful counts and score provenance."""

import json

from tests.runtime_ui.harness import RuntimeUiHarness, expect


class DailyBriefDiagnosticsTests(RuntimeUiHarness):
    def test_historical_report_displays_failed_attention_and_evidence_score(self):
        page = self.new_page()
        page.route("**/api/redmine-agent/**", lambda route: route.fulfill(
            status=200, content_type="application/json", body='{"success":true,"data":{}}',
        ))
        errors = []
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        try:
            page.goto(f"{self.base_url}/redmine-agent", wait_until="domcontentloaded")
            page.wait_for_function("typeof renderDailyBriefInner === 'function'")
            page.evaluate("switchTab('daily-brief')")
            issues = [{
                "issue_id": index, "subject": "Synthetic issue", "status": "completed",
                "ai_execution": {"device_evidence_status": "not_collected"},
                "result": {"confidence": 0.7, "result_format": "kkagent_markdown",
                           "evidence_quality": "verified"},
            } for index in range(1, 6)] + [{
                "issue_id": index, "subject": "Synthetic failure", "status": "failed",
                "ai_execution": {"device_evidence_status": "not_collected"},
                "error_type": "context_limit_exceeded" if index <= 7 else (
                    "incomplete_ai_output" if index == 8 else "evidence_gate_failed"
                ),
                "error": "Actual provider context limit: 16384 tokens",
            } for index in range(6, 10)]
            brief = {"run": {"status": "partial", "report_json": {"counts": {
                "total": 9, "needs_human_review": 0, "evidence_verified": 5,
            }}}, "issues": issues}
            page.route("**/api/redmine-agent/daily-brief/latest", lambda route: route.fulfill(
                status=200, content_type="application/json",
                body=json.dumps({"success": True, "data": brief}),
            ))
            page.evaluate("loadDailyBrief()")
            card = page.locator('#dailyBriefCard')
            expect(card.locator('.daily-brief-metric').filter(has_text='需人工确认').locator('b')).to_have_text('4')
            verified = card.locator('.daily-brief-metric').filter(has_text='证据已验证')
            expect(verified.locator('b')).to_have_text('5')
            expect(verified).to_have_attribute('title', '资料证据通过门禁；实机取证状态单独显示')
            self.assertEqual(card.inner_text().count('证据评分 0.7'), 5)
            self.assertNotIn('模型自评', card.inner_text())
            self.assertEqual(card.inner_text().count('模型上下文超限'), 2)
            self.assertEqual(card.inner_text().count('AI 未生成最终报告'), 1)
            for _ in range(2):
                page.evaluate("switchTab('issues')")
                page.evaluate("switchTab('daily-brief')")
                expect(card.locator('.daily-brief-state.failed')).to_have_count(4)
                self.assertEqual(card.inner_text().count('AI 未生成最终报告'), 1)
            self.assertEqual(card.inner_text().count('未执行实机取证'), 9)
            expect(card.locator('.daily-brief-state.failed').first).to_have_attribute(
                'title', 'Actual provider context limit: 16384 tokens',
            )
            legacy = page.evaluate("""() => renderDailyBriefInner({
                run: {status: 'completed'},
                issues: [{issue_id: 11, status: 'completed', result: {confidence: 0.9}}]
            })""")
            self.assertIn('模型自评 0.9', legacy)
            page.evaluate("""() => {
                singleIssueAnalysisHistory = [{
                    run: {run_id: 'synthetic-single', status: 'completed'},
                    issues: [{issue_id: 11, subject: 'Synthetic partial diagnosis',
                        status: 'completed', result: {
                            evidence_quality: 'partial', needs_human_review: true
                        }}]
                }];
                renderSingleIssueAnalysisHistory();
            }""")
            expect(page.locator('#singleIssueAnalysisHistory .daily-brief-state')).to_have_text(
                '✓ 已完成 · 证据部分闭环 · 需人工确认',
            )
            self.assert_no_page_errors(errors)
        finally:
            page.close()
