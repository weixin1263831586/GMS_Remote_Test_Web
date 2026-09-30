"""Report and artifact upload, analysis and task recovery."""

import json
import re

from tests.runtime_ui.harness import RuntimeUiHarness, expect


class RuntimeReportsAndArtifactsTests(RuntimeUiHarness):
    def test_user_actions_and_report_identity_use_friendly_horizontal_display(self):
        page = self.new_page()
        try:
            self.goto_shell(page)
            result = page.evaluate(
                """
                async () => {
                    displayUsersList([{
                        client_id: 'hcq@172.16.14.66',
                        user_id: 'N387pLbIBhpMw5JsWUL9hg',
                        username: 'hcq',
                        ip: '172.16.14.66',
                        source_label: '内网',
                        source: 'internal',
                        running: true,
                        configured: true,
                        devices: ['RK3576GMS6'],
                        cluster_jobs: [{
                            id: 'job-1',
                            worker_id: 'ats-worker-controller',
                            attempt_id: 'attempt-1',
                            status: 'running'
                        }]
                    }, {
                        client_id: 'wlq@172.16.14.67',
                        username: 'wlq',
                        ip: '172.16.14.67',
                        source_label: '内网',
                        source: 'internal',
                        running: false,
                        configured: true,
                        devices: [],
                        cluster_jobs: []
                    }, {
                        client_id: '172.16.14.246@172.16.14.246',
                        username: '172.16.14.246',
                        ip: '172.16.14.246',
                        source_label: '内网',
                        source: 'internal',
                        status: 'online',
                        running: false,
                        configured: false,
                        devices: [],
                        cluster_jobs: [],
                        removable: false,
                        removal_reason: '临时在线会话没有持久配置，断开后会自动清理'
                    }]);
                    const actions = Array.from(document.querySelectorAll(
                        '#users-table-body tr td:last-child > div'
                    ));
                    const buttons = Array.from(
                        document.querySelectorAll('#users-table-body button')
                    );
                    displayTestReports([{
                        timestamp: 'cluster-job-941843984fd44e1b9111532981e188c9',
                        report_name: '2026.07.30_10.39.50.173_6846',
                        source_timestamp: 'Fri Jul 30 10:39:53 CST 2026',
                        display_client_id: 'hcq@172.16.14.233',
                        test_type: 'CTS',
                        suite_version: '17_r1',
                        worker_id: 'ats-worker-controller',
                        pass: 1,
                        fail: 0,
                        total: 1
                    }]);
                    const reportCells = document.querySelectorAll(
                        '#reports-table-body tr:first-child td'
                    );
                    let deletePrompt = '';
                    const originalConfirm = showConfirmDialog;
                    showConfirmDialog = async (_title, message) => {
                        deletePrompt = message;
                        return false;
                    };
                    await deleteReport(
                        'cluster-job-941843984fd44e1b9111532981e188c9',
                        'cluster:job-1:attempt-1',
                        '2026.07.30_10.39.50.173_6846'
                    );
                    showConfirmDialog = originalConfirm;
                    return {
                        actionDisplays: actions.map(
                            action => getComputedStyle(action).display
                        ),
                        actionColumns: actions.map(
                            action => getComputedStyle(action).gridTemplateColumns
                        ),
                        buttonLabels: buttons.map(button => button.textContent.trim()),
                        removeDisabled: Array.from(
                            document.querySelectorAll('.user-remove-button')
                        ).map(button => button.disabled),
                        removeTitles: Array.from(
                            document.querySelectorAll('.user-remove-button')
                        ).map(button => button.title),
                        removeLefts: Array.from(
                            document.querySelectorAll(
                                '#users-table-body button[data-remove-user]'
                            )
                        ).map(button => Math.round(
                            button.getBoundingClientRect().left
                        )),
                        reportClient: reportCells[0].textContent.trim(),
                        reportSuite: reportCells[2].textContent.trim(),
                        reportWorker: reportCells[3].textContent.trim(),
                        reportName: reportCells[4].textContent.trim(),
                        reportTimestampTitle: reportCells[4].title,
                        reportFolder: tradefedResultFolderName(
                            '/suite/results/2026.07.30_10.39.50.173_6846'
                        ),
                        legacyStartDisplayFolder: tradefedResultFolderName(
                            'Fri Jul 30 10:39:53 CST 2026'
                        ),
                        reportNameDataset: document.querySelector(
                            '#reports-table-body tr:first-child'
                        ).dataset.reportName,
                        deletePrompt
                    };
                }
                """
            )

            self.assertEqual(result["actionDisplays"], ["grid", "grid", "grid"])
            self.assertEqual(
                result["actionColumns"],
                ["44px 44px", "44px 44px", "44px 44px"],
            )
            self.assertEqual(result["buttonLabels"], ["任务", "移除", "移除", "移除"])
            self.assertEqual(result["removeDisabled"], [True, False, True])
            self.assertIn("正在测试", result["removeTitles"][0])
            self.assertIn("临时在线会话", result["removeTitles"][2])
            self.assertEqual(len(set(result["removeLefts"])), 1)
            self.assertEqual(result["reportClient"], "hcq@172.16.14.233")
            self.assertEqual(result["reportSuite"], "android-cts-17_r1")
            self.assertEqual(result["reportWorker"], "ats-worker-controller")
            self.assertEqual(
                result["reportName"],
                "2026.07.30_10.39.50.173_6846",
            )
            self.assertEqual(
                result["reportTimestampTitle"],
                "cluster-job-941843984fd44e1b9111532981e188c9",
            )
            self.assertEqual(
                result["reportFolder"],
                "2026.07.30_10.39.50.173_6846",
            )
            self.assertEqual(result["legacyStartDisplayFolder"], "")
            self.assertEqual(
                result["reportNameDataset"],
                "2026.07.30_10.39.50.173_6846",
            )
            self.assertEqual(
                result["deletePrompt"],
                "确定要删除报告 2026.07.30_10.39.50.173_6846 吗？此操作不可恢复。",
            )
        finally:
            page.close()

    def test_report_upload_keeps_latest_selection_and_renders_suite_version(self):
        page = self.new_page()
        page_errors = []
        page.on("pageerror", lambda exc: page_errors.append(str(exc)))
        try:
            self.goto_shell(page)
            page.wait_for_function("typeof handleReportFile === 'function'")
            result = page.evaluate(
                """
                async () => {
                    const OriginalXHR = window.XMLHttpRequest;
                    const requests = [];
                    class FakeXHR {
                        constructor() {
                            this.readyState = 0;
                            this.status = 200;
                            this.responseText = '';
                            this.listeners = {};
                            this.uploadListeners = {};
                            this.upload = {
                                addEventListener: (name, callback) => {
                                    this.uploadListeners[name] = callback;
                                }
                            };
                            requests.push(this);
                        }
                        addEventListener(name, callback) {
                            this.listeners[name] = callback;
                        }
                        open() { this.readyState = 1; }
                        setRequestHeader() {}
                        send() { this.sent = true; }
                        abort() { this.aborted = true; }
                        complete(payload) {
                            this.readyState = 4;
                            this.responseText = JSON.stringify(payload);
                            this.listeners.load?.();
                        }
                    }

                    window.XMLHttpRequest = FakeXHR;
                    try {
                        const older = handleReportFile(new File(['old'], 'old.zip'));
                        const newer = handleReportFile(new File(['new'], 'new.zip'));
                        requests[1].complete({
                            success: true,
                            data: {
                                report_name: 'new-report',
                                summary: {total: 2, pass: 2, fail: 0, pass_rate: '100%'},
                                details: {suite_version: 'android-cts-17_r1'},
                                failures: []
                            }
                        });
                        await newer;
                        await new Promise(resolve => setTimeout(resolve, 350));

                        requests[0].complete({
                            success: true,
                            data: {
                                report_name: 'old-report',
                                summary: {total: 1, pass: 0, fail: 1, pass_rate: '0%'},
                                details: {suite_version: 'stale-suite'},
                                failures: []
                            }
                        });
                        await older;
                        await new Promise(resolve => setTimeout(resolve, 350));
                        return {
                            requestCount: requests.length,
                            olderAborted: requests[0].aborted === true,
                            reportName: window.currentReportName,
                            summary: document.querySelector('#report-summary').textContent
                        };
                    } finally {
                        window.XMLHttpRequest = OriginalXHR;
                    }
                }
                """
            )

            self.assertEqual(result["requestCount"], 2)
            self.assertTrue(result["olderAborted"])
            self.assertEqual(result["reportName"], "new-report")
            self.assertIn("android-cts-17_r1", result["summary"])
            self.assertNotIn("stale-suite", result["summary"])
            self.assert_no_page_errors(page_errors)
        finally:
            page.close()

    def test_chunk_upload_retries_and_preserves_actionable_errors(self):
        page = self.new_page()
        page_errors = []
        page.on("pageerror", lambda exc: page_errors.append(str(exc)))
        try:
            self.goto_shell(page)
            page.wait_for_function("typeof uploadFileInChunks === 'function'")
            result = page.evaluate(
                """
                async () => {
                    const OriginalXHR = window.XMLHttpRequest;
                    const requests = [];
                    class FakeXHR {
                        constructor() {
                            this.status = 0;
                            this.responseText = '';
                            this.listeners = {};
                            this.upload = {addEventListener: () => {}};
                            requests.push(this);
                        }
                        addEventListener(name, callback) { this.listeners[name] = callback; }
                        open() {}
                        setRequestHeader() {}
                        send(form) { this.form = form; }
                        respond(status, payload) {
                            this.status = status;
                            this.responseText = JSON.stringify(payload);
                            this.listeners.load?.();
                        }
                    }
                    const waitForRequests = async count => {
                        const deadline = Date.now() + 2500;
                        while (requests.length < count && Date.now() < deadline) {
                            await new Promise(resolve => setTimeout(resolve, 20));
                        }
                        if (requests.length < count) throw new Error(`missing request ${count}`);
                    };

                    window.XMLHttpRequest = FakeXHR;
                    try {
                        const upload = uploadFileInChunks(
                            new File(['abcd'], 'firmware.img'),
                            '/fake-upload',
                            {chunkSize: 2, concurrent: 1, maxRetries: 1}
                        );
                        await waitForRequests(1);
                        requests[0].respond(503, {success: false, error: 'temporary failure'});
                        await waitForRequests(2);
                        requests[1].respond(200, {success: true, upload_complete: false});
                        await waitForRequests(3);
                        requests[2].respond(200, {
                            success: true,
                            upload_complete: true,
                            staged: true,
                            upload_id: 'upload-1'
                        });
                        const uploadResult = await upload;

                        const failedCheck = uploadFileInChunks(
                            new File(['xy'], 'other.img'),
                            '/fake-upload',
                            {chunkSize: 2, resume: true, checkExisting: true}
                        );
                        await waitForRequests(4);
                        requests[3].respond(409, {
                            success: false,
                            error: 'Chunk metadata does not match the existing upload session'
                        });
                        let checkError = null;
                        try {
                            await failedCheck;
                        } catch (error) {
                            checkError = {
                                isError: error instanceof Error,
                                message: error.message,
                                uploadId: error.upload_id
                            };
                        }
                        return {
                            requestCount: requests.length,
                            staged: uploadResult.staged,
                            checkError
                        };
                    } finally {
                        window.XMLHttpRequest = OriginalXHR;
                    }
                }
                """
            )

            self.assertEqual(result["requestCount"], 4)
            self.assertTrue(result["staged"])
            self.assertTrue(result["checkError"]["isError"])
            self.assertIn("metadata", result["checkError"]["message"])
            self.assertTrue(result["checkError"]["uploadId"])
            self.assert_no_page_errors(page_errors)
        finally:
            page.close()

    def test_report_diagnosis_labels_rule_fallback_with_ai_error(self):
        page = self.new_page()
        try:
            self.goto_shell(page)
            page.evaluate(
                """() => {
                    window.currentReportAnalysisData = {
                        report_name: 'mock-report',
                        failures: [{name: 'Example#testFailure', module: 'MockModule'}],
                        details: {test_type: 'CTS'}
                    };
                    renderReportDiagnosis({
                        test_name: 'Example#testFailure',
                        module: 'MockModule',
                        failure_index: 0,
                        ai_result: {
                            ai_enabled: false,
                            ai_attempted: true,
                            ai_error: 'glm_local quota exceeded',
                            root_cause: '待验证：规则判断',
                            root_cause_status: 'hypothesis',
                            root_cause_confidence: 'low',
                            observed_failure: 'AssertionError: expected true',
                            root_cause_note: '当前直接证据只证明断言失败。',
                            analysis: '规则分析内容',
                            suggestions: []
                        },
                        suite_target: {},
                        source_search_results: [],
                        knowledge_base_results: []
                    });
                }"""
            )
            expect(page.locator("#report-diagnostic-summary")).to_contain_text(
                "规则分析（AI 不可用）"
            )
            expect(page.locator("#report-diagnostic-result")).to_contain_text(
                "本地 AI 未完成分析"
            )
            expect(page.locator("#report-diagnostic-result")).to_contain_text(
                "glm_local quota exceeded"
            )
            expect(page.locator("#report-diagnostic-result")).to_contain_text(
                "已观察到的失败"
            )
            expect(page.locator("#report-diagnostic-result")).to_contain_text(
                "初步判断"
            )
            expect(page.locator("#report-diagnostic-result")).to_contain_text(
                "Hypothesis · 低置信度"
            )
            expect(page.locator("#report-diagnostic-result")).not_to_contain_text(
                "Root cause"
            )
            page.evaluate(
                """() => renderReportDiagnosis({
                    test_name: 'Example#testFailure',
                    module: 'MockModule',
                    failure_index: 0,
                    ai_result: {
                        ai_enabled: true,
                        ai_model: 'Backup AI',
                        ai_provider: 'zhipu',
                        ai_fallback_used: true,
                        ai_provider_errors: ['glm_local：本地模型额度已用尽。'],
                        root_cause: '待验证：备用模型结论',
                        root_cause_status: 'hypothesis',
                        root_cause_confidence: 'low',
                        observed_failure: 'AssertionError: expected true',
                        analysis: '备用模型分析',
                        suggestions: []
                    },
                    suite_target: {},
                    source_search_results: [],
                    knowledge_base_results: []
                })"""
            )
            expect(page.locator("#report-diagnostic-summary")).to_contain_text(
                "Backup AI（备用）"
            )
            expect(page.locator("#report-diagnostic-result")).to_contain_text(
                "本次已由 Backup AI 完成"
            )
        finally:
            page.close()

    def test_report_analysis_treats_uploaded_and_ai_values_as_untrusted_text(self):
        page = self.new_page()
        page_errors = []
        page.on("pageerror", lambda error: page_errors.append(str(error)))
        marker = "<img src=x onerror=window.__reportXss=(window.__reportXss||0)+1>"
        try:
            self.goto_shell(page)
            page.evaluate(
                """marker => {
                    window.__reportXss = 0;
                    displayReportAnalysis({
                        report_name: marker,
                        summary: {
                            total: marker,
                            pass: 0,
                            fail: 1,
                            pass_rate: marker,
                        },
                        details: {
                            test_type: marker,
                            suite_version: marker,
                            android_version: marker,
                            soc_platform: marker,
                        },
                        failures: [{
                            module: marker,
                            name: marker,
                            reason: marker,
                        }],
                    });
                    displayAIAnalysis({
                        ai_model: marker,
                        source_code_fetched: true,
                        source_url: 'javascript:window.__reportXss=99',
                        source_file_path: marker,
                        root_cause: marker,
                        analysis: marker,
                        suggestions: [marker],
                        related_docs: [{
                            title: marker,
                            url: 'javascript:window.__reportXss=100',
                        }],
                    }, 'test', 'failure');
                    showRedmineAuthDialog(
                        "https://example.test/report');window.__reportXss=101;//",
                        null, null, null, null
                    );
                }""",
                marker,
            )
            page.wait_for_timeout(100)

            self.assertEqual(page.evaluate("window.__reportXss"), 0)
            expect(page.locator("#report-analysis-result img")).to_have_count(0)
            expect(page.locator("[id^='ai-analysis-modal-'] img")).to_have_count(0)
            expect(page.locator("a[href^='javascript:']")).to_have_count(0)
            expect(page.locator("#report-summary")).to_contain_text(marker)
            page.locator("#redmine-auth-modal button.btn-primary").evaluate(
                "button => button.click()"
            )
            self.assertEqual(page.evaluate("window.__reportXss"), 0)
            self.assert_no_page_errors(page_errors)
        finally:
            page.close()

    def test_report_analysis_clear_cancels_requests_and_drops_stale_state(self):
        page = self.new_page()
        page_errors = []
        page.on("pageerror", lambda error: page_errors.append(str(error)))
        try:
            self.goto_shell(page)
            result = page.evaluate(
                """() => {
                    const beforeGeneration = reportUploadGeneration;
                    const aborted = {upload: 0, redmine: 0};
                    currentReportUploadRequest = {
                        readyState: 1,
                        abort() { aborted.upload += 1; },
                    };
                    currentRedmineRequest = {
                        abort() { aborted.redmine += 1; },
                    };
                    window.currentReportName = 'stale-report';
                    window.currentReportAnalysisData = {
                        report_name: 'stale-report',
                        summary: {total: 1},
                    };
                    window.reportDiagnosis = {data: {test_name: 'stale-test'}};
                    document.getElementById('report-analysis-result').style.display = 'block';
                    document.getElementById('report-upload-progress').style.opacity = '1';
                    document.getElementById('report-progress-fill').style.width = '88%';
                    document.getElementById('report-diagnosis-minimized').style.display = 'flex';
                    ModalManager.open('report-diagnosis-modal');

                    resetReportAnalysis();

                    return {
                        aborted,
                        generationAdvanced: reportUploadGeneration === beforeGeneration + 1,
                        uploadRequestCleared: currentReportUploadRequest === null,
                        redmineRequestCleared: currentRedmineRequest === null,
                        reportName: window.currentReportName,
                        reportData: window.currentReportAnalysisData,
                        diagnosis: window.reportDiagnosis,
                        resultDisplay: document.getElementById('report-analysis-result').style.display,
                        uploadEmpty: document.getElementById('report-upload-zone').classList.contains('upload-empty'),
                        progressOpacity: document.getElementById('report-upload-progress').style.opacity,
                        progressWidth: document.getElementById('report-progress-fill').style.width,
                        diagnosisOpen: ModalManager.isOpen('report-diagnosis-modal'),
                        minimizedDisplay: document.getElementById('report-diagnosis-minimized').style.display,
                    };
                }"""
            )

            self.assertEqual(result["aborted"], {"upload": 1, "redmine": 1})
            self.assertTrue(result["generationAdvanced"])
            self.assertTrue(result["uploadRequestCleared"])
            self.assertTrue(result["redmineRequestCleared"])
            self.assertEqual(result["reportName"], "")
            self.assertIsNone(result["reportData"])
            self.assertIsNone(result["diagnosis"])
            self.assertEqual(result["resultDisplay"], "none")
            self.assertTrue(result["uploadEmpty"])
            self.assertEqual(result["progressOpacity"], "0")
            self.assertEqual(result["progressWidth"], "0%")
            self.assertFalse(result["diagnosisOpen"])
            self.assertEqual(result["minimizedDisplay"], "none")
            self.assert_no_page_errors(page_errors)
        finally:
            page.close()

    def test_report_analysis_workbench_upload_diagnose_email_and_cross_page(self):
        page = self.new_page()
        page_errors = []
        email_requests = []
        page.on("pageerror", lambda error: page_errors.append(str(error)))

        def diagnose(route):
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({
                    "success": True,
                    "data": {
                        "test_name": "com.example.SampleTest#testFailure",
                        "module": "CtsSampleTestCases",
                        "failure_index": 0,
                        "error_message": "AssertionError: expected true",
                        "stack_trace": "at com.example.SampleTest.testFailure(SampleTest.java:42)",
                        "ai_result": {
                            "ai_enabled": True,
                            "ai_model": "Local GLM",
                            "root_cause": "待验证：示例断言失败",
                            "root_cause_status": "hypothesis",
                            "root_cause_confidence": "low",
                            "observed_failure": "AssertionError: expected true",
                            "analysis": "需要复现并核对前置条件",
                            "suggestions": ["执行单测复现"],
                        },
                        "suite_target": {},
                        "source_search_results": [],
                        "knowledge_base_results": [],
                        "mainline_exemptions": [],
                    },
                }),
            )

        def send_email(route):
            email_requests.append(route.request.post_data_json)
            route.fulfill(
                status=200,
                content_type="application/json",
                body='{"success":true,"data":{"to":["qa@example.test"]}}',
            )

        page.route("**/api/reports/diagnose", diagnose)
        page.route("**/api/email/send", send_email)
        xml = """<?xml version="1.0" encoding="UTF-8"?>
<Result suite_name="CTS" suite_version="17_r1" devices="DEVICE-1" start_display="2026-08-21 17:00:00">
  <Build build_version_release="17" />
  <Summary pass="1" failed="1" />
  <Module name="CtsSampleTestCases">
    <TestCase name="com.example.SampleTest">
      <Test name="testFailure" result="fail">
        <Failure message="AssertionError: expected true">
          <StackTrace>at com.example.SampleTest.testFailure(SampleTest.java:42)</StackTrace>
        </Failure>
      </Test>
    </TestCase>
  </Module>
</Result>"""

        try:
            self.goto_shell(page)
            page.evaluate("switchPage('report-analysis', null)")
            page.locator("#report-file-input").set_input_files({
                "name": "sample-test-result.xml",
                "mimeType": "application/xml",
                "buffer": xml.encode("utf-8"),
            })

            expect(page.locator("#report-analysis-result")).to_be_visible()
            expect(page.locator("#report-summary")).to_contain_text("50.00%")
            expect(page.locator("#report-failure-list")).to_contain_text(
                "com.example.SampleTest#testFailure"
            )

            page.locator(".report-failure-action.diagnose").evaluate(
                "button => button.click()"
            )
            expect(page.locator("#report-diagnosis-modal")).to_have_class(
                re.compile(r"\bshow\b")
            )
            expect(page.locator("#report-diagnostic-result")).to_contain_text(
                "示例断言失败"
            )
            page.locator("#report-diagnostic-result .dx-action-card").first.evaluate(
                "button => button.click()"
            )
            expect(page.locator("#page-test")).to_be_visible()
            expect(page.locator("#test-module")).to_have_value("CtsSampleTestCases")
            expect(page.locator("#test-case")).to_have_value(
                "com.example.SampleTest#testFailure"
            )

            page.evaluate("switchPage('report-analysis', null)")
            answers = iter(["qa@example.test", ""])
            page.on("dialog", lambda dialog: dialog.accept(next(answers)))
            page.get_by_role("button", name="📧 发送邮件").evaluate(
                "button => button.click()"
            )
            page.wait_for_function("() => window.currentReportAnalysisData !== null")
            page.wait_for_timeout(100)
            self.assertEqual(len(email_requests), 1)
            self.assertEqual(email_requests[0]["to"], "qa@example.test")
            self.assertIn("CtsSampleTestCases", email_requests[0]["body"])

            page.get_by_role("button", name="清除").evaluate(
                "button => button.click()"
            )
            expect(page.locator("#report-analysis-result")).to_be_hidden()
            self.assertIsNone(page.evaluate("window.currentReportAnalysisData"))
            self.assert_no_page_errors(page_errors)
        finally:
            page.close()

    def test_apk_analysis_tab_survives_shell_refresh(self):
        page = self.new_page()
        try:
            self.goto_shell(page)
            page.evaluate("switchPage('apk-analysis', null)")
            page.wait_for_function("typeof switchApkTab === 'function'")
            page.evaluate("switchApkTab('permissions')")
            expect(page.locator('[data-apk-tab="permissions"]')).to_have_class(
                re.compile(r"\bactive\b")
            )

            page.reload(wait_until="domcontentloaded")
            page.wait_for_function(
                """currentPage === 'apk-analysis'
                && document.querySelector('[data-apk-tab="permissions"]')
                    .classList.contains('active')"""
            )

            # 没有选择 APK 任务时结果容器整体隐藏；此处验证内部面板自身
            # 的选中状态，任务恢复场景另有运行时用例覆盖。
            expect(page.locator("#apk-tab-permissions")).to_have_css(
                "display", "block"
            )
            expect(page.locator("#apk-tab-manifest")).to_have_css(
                "display", "none"
            )
            self.assertEqual(
                page.evaluate("sessionStorage.getItem('gms_apk_analysis_tab')"),
                "permissions",
            )
        finally:
            page.close()

    def test_firmware_and_apk_actions_send_expected_requests(self):
        page = self.new_page()
        requests = []

        def handle_request(route):
            request = route.request
            requests.append((request.method, request.url.split(self.base_url, 1)[-1]))
            route.fulfill(
                status=200,
                content_type="application/json",
                body='{"success":true,"task_id":"apk-task","status":"completed"}',
            )

        try:
            self.goto_shell(page)
            page.route("**/api/burn/**", handle_request)
            page.route("**/api/apk/**", handle_request)
            page.evaluate(
                """
                state.selectedDevices = new Set(['D1']);
                state.devices = [{device_id: 'D1', status: 'online'}];
                window.apkCurrentTaskId = 'apk-task';
                executeBurnOperation = async (endpoint, data) => {
                    await apiCall(endpoint, 'POST', {
                        devices: Array.from(state.selectedDevices),
                        ...data
                    });
                };
                document.getElementById('gsi-script').value = '/tmp/burn.sh';
                document.getElementById('gsi-system').value = '/tmp/system.img';
                document.getElementById('gsi-vendor').value = '';
                document.getElementById('sn-code').value = 'SN001';
                """
            )

            page.evaluate("submitGsiBurn()")
            page.evaluate("submitSnBurn()")
            page.evaluate("startApkAnalysis()")
            page.evaluate(
                """Promise.all([
                    fetch('/api/burn/firmware', {method:'POST'}),
                    fetch('/api/apk/download/apk-task'),
                    fetch('/api/apk/task/apk-task', {method:'DELETE'})
                ])"""
            )

            for expected in (
                ("POST", "/api/burn/gsi"),
                ("POST", "/api/burn/serial"),
                ("POST", "/api/apk/analyze/apk-task"),
                ("POST", "/api/burn/firmware"),
                ("GET", "/api/apk/download/apk-task"),
                ("DELETE", "/api/apk/task/apk-task"),
            ):
                self.assertIn(expected, requests)
        finally:
            page.close()

    def test_apk_page_restores_owner_scoped_running_task_and_exposes_retry(self):
        page = self.new_page()
        active_id = "00000000-0000-0000-0000-000000000101"
        error_id = "00000000-0000-0000-0000-000000000102"
        tasks = [
            {
                "task_id": active_id,
                "filename": "running.apk",
                "status": "analyzing",
                "progress": 37,
                "timestamp": 2,
                "error": None,
            },
            {
                "task_id": error_id,
                "filename": "retry.jar",
                "status": "error",
                "progress": 10,
                "timestamp": 1,
                "error": "jadx failed",
            },
        ]
        page.route(
            "**/api/apk/tasks",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"success": True, "data": {"tasks": tasks}}),
            ),
        )

        def task_status(route):
            task_id = route.request.url.rsplit("/", 1)[-1]
            task = next(item for item in tasks if item["task_id"] == task_id)
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"success": True, "data": task}),
            )

        page.route("**/api/apk/status/*", task_status)
        try:
            self.goto_shell(page)
            page.evaluate("switchPage('apk-analysis', null)")
            page.wait_for_function(
                "taskId => window.apkCurrentTaskId === taskId",
                arg=active_id,
            )
            expect(page.locator("#apk-task-history option")).to_have_count(3)
            expect(page.locator("#apk-task-history")).to_have_value(active_id)
            expect(page.locator("#apk-analysis-state")).to_have_text(
                "正在反编译... (37%)"
            )
            expect(page.locator("#apk-btn-analyze")).to_be_visible()
            expect(page.locator("#apk-btn-analyze")).to_be_disabled()

            page.locator("#apk-task-history").select_option(error_id)
            page.wait_for_function(
                "taskId => window.apkCurrentTaskId === taskId",
                arg=error_id,
            )
            expect(page.locator("#apk-analysis-state")).to_have_text(
                "错误: jadx failed"
            )
            expect(page.locator("#apk-btn-analyze")).to_be_enabled()
            expect(page.locator("#apk-btn-analyze")).to_have_text("🔬 重新分析")
        finally:
            page.close()
