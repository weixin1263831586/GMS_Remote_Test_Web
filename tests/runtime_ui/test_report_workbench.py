"""Report workbench: preserve failure indices, actions and APK dimensions."""
import os
from pathlib import Path

from tests.runtime_ui.harness import RuntimeUiHarness, expect


class RuntimeReportWorkbenchTests(RuntimeUiHarness):
    def test_report_failure_selection_and_reentry(self):
        page = self.new_page()
        page_errors = []
        page.on("pageerror", lambda error: page_errors.append(str(error)))
        try:
            self.goto_shell(page)
            page.evaluate("switchPage('report-analysis', null)")
            expect(page.locator("#report-upload-zone")).to_be_visible()
            self.assertTrue(page.locator("#report-upload-zone").evaluate(
                "element => element.classList.contains('upload-empty')"
            ))
            self.assertEqual(
                page.locator("#report-file-input").get_attribute("accept"),
                ".xml,.zip,.rar,.tar,.tar.gz,.txt,.log",
            )

            page.evaluate("""() => displayReportAnalysis({
                report_name: 'Redmine-48231-2026.11.16_17.04.22.327_8108_CtsWindowManagerDeviceActivity.zip',
                summary: {total: 17, pass: 15, fail: 2, pass_rate: '88.24%'},
                details: {
                    test_type: 'CTS', suite_version: '16_r2',
                    android_version: '16', soc_platform: 'RK3588'
                },
                failures: [{
                    module: 'CtsAppTestCases',
                    name: 'com.example.ActivityManagerTest#testBackground',
                    reason: 'AssertionError: expected true\\n at Example.java:42'
                }, {
                    module: 'CtsSecurityTestCases',
                    name: 'com.example.PackageSignatureTest#testSignature',
                    reason: 'SecurityException: invalid signature\\n at Verify.java:88'
                }],
            })""")
            expect(page.locator("#report-active-file")).to_be_visible()
            expect(page.locator("#report-active-name")).to_contain_text("48231")
            expect(page.locator("#report-summary")).to_contain_text("88.24%")
            expect(page.locator(".report-overview-heading")).to_have_count(0)
            expect(page.get_by_role("button", name="发送邮件")).to_be_visible()
            expect(page.get_by_role("button", name="清除")).to_be_visible()
            self.assertEqual(page.locator(".report-overview-line").count(), 1)
            expect(page.locator("#report-failure-list .report-failure-card")).to_have_count(2)
            expect(page.locator("#report-failure-list .report-failure-preview")).to_have_count(0)
            expect(page.locator("#report-failure-detail .report-detail-fields")).to_have_count(0)
            expect(page.locator("#report-failure-detail .report-detail-source")).to_contain_text(
                "未发现原始 host_log"
            )
            self.assertIsNotNone(page.locator("#report-file-input").get_attribute("multiple"))
            page.evaluate("""() => {
                window.currentReportAnalysisData.analysis_sources = {
                    host_logs: ['logs/inv_1/host_log_1.txt']
                };
                selectReportFailure(0);
            }""")
            expect(page.locator("#report-failure-detail .report-detail-source")).to_contain_text(
                "已发现 host_log，但未匹配该用例"
            )
            expect(page.locator("#report-failure-detail")).to_be_visible()
            expect(page.locator("#report-failure-detail .report-stack")).to_contain_text("Example.java:42")
            expect(page.locator("#report-failure-header-actions .report-failure-action")).to_have_count(3)
            expect(page.locator("#report-failure-detail .report-failure-action")).to_have_count(0)
            self.assertEqual(
                page.locator("#report-failure-header-actions .report-failure-action.diagnose").get_attribute("data-a0"),
                "0",
            )
            page.evaluate("""() => {
                window.currentReportAnalysisData.failures[1].raw_log = [
                    '11-16 17:13:33 I/ModuleListener: [1/1] m10r0800015 '
                        + 'CtsSecurityTestCases com.example.PackageSignatureTest#testSignature '
                        + 'FAILURE: SecurityException: invalid signature',
                    ' at Verify.java:88'
                ].join('\\n');
            }""")
            page.locator('[data-report-failure-index="1"]').click()
            expect(page.locator("#report-failure-detail .report-stack")).to_contain_text("Verify.java:88")
            expect(page.locator("#report-failure-detail .report-stack")).to_contain_text(
                "11-16 17:13:33 I/ModuleListener:"
            )
            expect(page.locator("#report-failure-detail .report-stack")).to_contain_text("FAILURE:")
            expect(page.locator("#report-failure-detail .report-detail-source")).to_have_count(0)
            expect(page.locator("#report-failure-detail .report-detail-fields")).to_have_count(0)
            expect(page.locator("#report-failure-detail .report-detail-heading")).to_contain_text(
                "PackageSignatureTest"
            )
            self.assertEqual(
                page.locator("#report-failure-header-actions .report-failure-action.diagnose").get_attribute("data-a0"),
                "1",
            )
            self.assertEqual(
                page.locator("#report-failure-header-actions .report-failure-action.reply").get_attribute("data-a3"),
                "48231",
            )
            expect(page.locator('#report-failure-list .report-failure-card.selected')).to_have_count(1)
            expect(page.locator('#report-failure-header-actions .report-failure-action.test')).to_be_visible()
            expect(page.locator('#report-failure-header-actions .report-failure-action.diagnose')).to_be_visible()

            for width, height in ((1792, 856), (1440, 900), (1280, 800)):
                with self.subTest(viewport=(width, height)):
                    page.set_viewport_size({"width": width, "height": height})
                    metrics = page.evaluate("""() => {
                        const rect = selector =>
                            document.querySelector(selector).getBoundingClientRect();
                        const page = rect('#page-report-analysis');
                        const drop = rect('#report-upload-zone');
                        const current = rect('#report-active-file');
                        const results = rect('#report-failures');
                        const list = rect('#report-failure-list');
                        const detail = rect('#report-failure-detail');
                        return {
                            control: getComputedStyle(
                                document.querySelector('#report-analysis-result [data-click="sendReportAnalysisEmail"]')
                            ).height,
                            summaryHeight: rect('.report-overview-line').height,
                            overviewHeight: rect('.report-overview').height,
                            summaryItems: [...document.querySelectorAll(
                                '#report-summary .report-summary-stats > div,'
                                + '#report-summary .report-summary-context > div'
                            )].map(node => ({
                                top: node.getBoundingClientRect().top,
                                height: node.getBoundingClientRect().height,
                                width: node.getBoundingClientRect().width,
                                labelWidth: node.querySelector('.summary-label').getBoundingClientRect().width,
                                valueWidth: node.querySelector('.summary-value').getBoundingClientRect().width
                            })),
                            reportValueFits: (() => {
                                const value = document.querySelector('.report-summary-report .summary-value');
                                return value.scrollWidth <= value.clientWidth + 2;
                            })(),
                            actionTop: rect('.report-overview-actions').top,
                            summaryWidth: rect('#report-summary').width,
                            summaryContentRight: rect('#report-summary .report-summary-report').right,
                            summaryRight: rect('#report-summary').right,
                            headerRight: rect('.report-failure-heading').right,
                            headerActionsRight: rect('.report-failure-header-actions').right,
                            headerActionsTop: rect('.report-failure-header-actions').top,
                            headingTop: rect('.report-failure-heading').top,
                            summaryOverflow: document.querySelector('#report-summary').scrollWidth
                                > document.querySelector('#report-summary').clientWidth + 2,
                            uploadHeight: drop.height,
                            uploadSameRow: Math.abs(drop.top - current.top),
                            listSameRow: Math.abs(list.top - detail.top),
                            listWidth: list.width, detailWidth: detail.width,
                            resultsWithinPage: results.bottom <= page.bottom + 2,
                            rootOverflow: document.documentElement.scrollWidth - innerWidth
                        };
                    }""")
                    self.assertEqual(metrics["control"], "32px", metrics)
                    self.assertLessEqual(metrics["summaryHeight"], 46, metrics)
                    self.assertLessEqual(metrics["overviewHeight"], 50, metrics)
                    self.assertGreaterEqual(len(metrics["summaryItems"]), 8, metrics)
                    # Seven semantic fields keep stable widths; the report
                    # filename alone absorbs the remaining horizontal space.
                    expected = (150, 150, 150, 150, 150, 150, 150)
                    for item, width_px in zip(metrics["summaryItems"][:7], expected):
                        self.assertAlmostEqual(item["width"], width_px, delta=1, msg=metrics)
                    if width == 1792:
                        self.assertGreaterEqual(metrics["summaryItems"][-1]["width"], 440, metrics)
                        self.assertTrue(metrics["reportValueFits"], metrics)
                    self.assertLessEqual(
                        max(item["top"] for item in metrics["summaryItems"])
                        - min(item["top"] for item in metrics["summaryItems"]), 1, metrics
                    )
                    self.assertLessEqual(
                        abs(metrics["summaryItems"][0]["top"] - metrics["actionTop"]), 12, metrics
                    )
                    self.assertLessEqual(metrics["uploadSameRow"], 2, metrics)
                    self.assertGreater(metrics["summaryWidth"], 550, metrics)
                    self.assertLessEqual(metrics["headerRight"] - metrics["headerActionsRight"], 16, metrics)
                    self.assertLessEqual(abs(metrics["headerActionsTop"] - metrics["headingTop"]), 14, metrics)
                    if not metrics["summaryOverflow"]:
                        self.assertLessEqual(metrics["summaryRight"] - metrics["summaryContentRight"], 16, metrics)
                    self.assertLessEqual(metrics["listSameRow"], 2, metrics)
                    self.assertGreater(metrics["listWidth"], 180, metrics)
                    self.assertGreater(metrics["detailWidth"], 220, metrics)
                    self.assertLessEqual(
                        metrics["listWidth"] / (metrics["listWidth"] + metrics["detailWidth"]),
                        0.24, metrics
                    )
                    self.assertEqual(page.locator(
                        "#report-failure-detail .report-stack-text"
                    ).first.evaluate("el => getComputedStyle(el).whiteSpace"), "pre-wrap")
                    self.assertTrue(metrics["resultsWithinPage"], metrics)
                    self.assertLessEqual(metrics["rootOverflow"], 2, metrics)
                    preview_dir = os.getenv("GMS_UI_PREVIEW_DIR")
                    if preview_dir and width == 1792:
                        target = Path(preview_dir)
                        target.mkdir(parents=True, exist_ok=True)
                        page.screenshot(path=str(target / "report-workbench.png"))

            page.evaluate("resetReportAnalysis()")
            expect(page.locator("#report-analysis-result")).to_be_hidden()
            expect(page.locator("#report-failure-header-actions .report-failure-action")).to_have_count(0)
            expect(page.locator("#report-active-file")).to_be_hidden()
            expect(page.locator("#report-upload-zone")).to_be_visible()
            self.assertTrue(page.locator("#report-upload-zone").evaluate(
                "element => element.classList.contains('upload-empty')"
            ))
            self.assert_no_page_errors(page_errors)
        finally:
            page.close()

    def test_large_cts_counts_fit_fixed_single_row_summary_chips(self):
        page = self.new_page()
        try:
            self.goto_shell(page)
            page.evaluate("switchPage('report-analysis', null)")
            page.evaluate("""() => displayReportAnalysis({
                report_name: '2026.10.14_15.28.47.806_5426',
                summary: {total: 3038784, pass: 3036568, fail: 2216, pass_rate: '99.93%'},
                details: {test_type: 'CTS', suite_version: '17_r2', android_version: '17'},
                failures: [{module: 'CtsAppOps2TestCases',
                            name: 'android.app.appops2.cts.AppOpsLoggingTest#requestInstall',
                            reason: 'java.lang.IndexOutOfBoundsException'}]
            })""")
            expected = (
                (".report-summary-total", 150, "3038784"),
                (".report-summary-pass", 150, "3036568"),
                (".report-summary-fail", 150, "2216"),
                (".report-summary-pass-rate", 150, "99.93%"),
                (".report-summary-test-type", 150, "CTS"),
                (".report-summary-suite-version", 150, "17_r2"),
                (".report-summary-android-version", 150, "17"),
            )
            for selector, width_px, value in expected:
                cell = page.locator("#report-summary " + selector)
                expect(cell).to_have_count(1)
                expect(cell.locator(".summary-value")).to_have_text(value)
                geom = cell.evaluate("""el => ({
                    width: el.getBoundingClientRect().width,
                    valueFits: el.querySelector('.summary-value').scrollWidth
                        <= el.querySelector('.summary-value').clientWidth + 1
                })""")
                self.assertAlmostEqual(geom["width"], width_px, delta=1, msg=geom)
                self.assertTrue(geom["valueFits"], (selector, geom))
            self.assertEqual(page.locator("#report-summary").evaluate(
                "el => getComputedStyle(el).flexWrap"
            ), "nowrap")
        finally:
            page.close()

    def test_empty_upload_surfaces_match_apk_workbench(self):
        page = self.new_page()
        try:
            self.goto_shell(page)
            page.evaluate("switchPage('apk-analysis', null)")
            apk = page.evaluate("""() => ({
                surface: getComputedStyle(document.querySelector('#apk-upload-zone')).backgroundColor,
                frame: getComputedStyle(
                    document.querySelector('#page-apk-analysis .analysis-upload-frame')
                ).backgroundColor,
                page: getComputedStyle(document.querySelector('#page-apk-analysis')).backgroundColor
            })""")
            page.evaluate("switchPage('report-analysis', null)")
            report = page.evaluate("""() => ({
                surface: getComputedStyle(document.querySelector('#report-upload-zone')).backgroundColor,
                frame: getComputedStyle(
                    document.querySelector('#page-report-analysis .analysis-upload-frame')
                ).backgroundColor,
                page: getComputedStyle(document.querySelector('#page-report-analysis')).backgroundColor
            })""")
            self.assertEqual(report, apk, (report, apk))
        finally:
            page.close()

    def test_all_passed_report_keeps_single_column_and_no_stale_failure(self):
        page = self.new_page()
        try:
            self.goto_shell(page)
            page.evaluate("switchPage('report-analysis', null)")
            page.evaluate("""() => {
                displayReportAnalysis({
                    report_name: 'sample.xml',
                    summary: {total: 2, pass: 1, fail: 1, pass_rate: '50.00%'},
                    failures: [{module: 'ModuleA', name: 'Suite#testA', reason: 'fail'}]
                });
                displayReportAnalysis({
                    report_name: 'passing.xml',
                    summary: {total: 2, pass: 2, fail: 0, pass_rate: '100.00%'},
                    failures: []
                });
            }""")
            expect(page.locator("#report-failure-list")).to_contain_text("全部通过")
            expect(page.locator("#report-failure-detail")).to_be_hidden()
            expect(page.locator("#report-failure-header-actions .report-failure-action")).to_have_count(0)
            expect(page.locator("#report-failure-list .report-failure-card")).to_have_count(0)
            expect(page.locator("#report-active-name")).to_contain_text("passing.xml")
        finally:
            page.close()
