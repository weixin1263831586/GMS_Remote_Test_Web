"""Report copying supports different suites on a single online Worker."""

import json

from tests.runtime_ui.harness import RuntimeUiHarness, expect


class ReportCopyBrowserTests(RuntimeUiHarness):
    def test_single_worker_copy_between_suites_and_repeated_opening(self):
        page = self.new_page()
        worker = "ats-worker-controller"
        source = "/home/test/GMS-Suite/android-gts-14-R2/android-gts/tools"
        target = "/home/test/GMS-Suite/android-gts-14-R4/android-gts/tools"
        report = "2026.10.30_11.34.27.443_5925"
        destination = target.removesuffix("/tools") + "/results/" + report
        mutations = []
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))

        def handle(route):
            request = route.request
            path = request.url.split("?", 1)[0]
            if path.endswith("/api/cluster/status"):
                payload = {"success": True, "enabled": False, "local_worker_id": worker}
            elif path.endswith("/api/cluster/workers"):
                payload = {"success": True, "workers": [
                    {"id": worker, "address": "192.0.2.10", "status": "online"},
                ]}
            elif path.endswith("/api/cluster/suites/files"):
                payload = {"success": True, "data": {"items": [{"name": report, "type": "directory"}]}}
            elif path.endswith("/api/cluster/suites"):
                payload = {"success": True, "suites": [
                    {"tools_path": path, "suite_key": "GTS:14", "suite_version": "14", "available": True}
                    for path in (source, target)
                ]}
            elif path.endswith("/api/cluster/suites/report-copies"):
                self.assertEqual(request.method, "POST")
                mutations.append(request.post_data_json)
                payload = {"success": True, "copy_id": "transfer-copy", "transfer": {
                    "status": "completed", "size_bytes": 128,
                }}
            elif path.endswith("/api/cluster/suites/report-copies/transfer-copy/import"):
                self.assertEqual(request.method, "POST")
                mutations.append("import")
                payload = {"success": True, "result": {"destination": destination}}
            else:
                payload = {"success": True}
            route.fulfill(content_type="application/json", body=json.dumps(payload))

        page.route("**/api/cluster/**", handle)
        page.add_init_script("localStorage.setItem('gms_current_page','test-suites');")
        try:
            self.goto_shell(page)
            page.wait_for_function("typeof openReportCopyModal === 'function'")
            page.evaluate("path => { state.suiteBrowser.selectedSuitePath = path; }", source)
            expect(page.locator("#btn-copy-test-report")).to_be_visible()
            for _ in range(2):
                page.locator("#btn-copy-test-report").click()
                expect(page.locator("#report-copy-modal")).to_be_visible()
                page.wait_for_function("document.getElementById('report-copy-status').textContent === ''")
                expect(page.locator("#report-copy-target-suite")).to_have_value(target)
                expect(page.locator("#report-copy-submit")).to_be_enabled()
                expect(page.locator("#report-copy-source-worker option")).to_have_count(1)
                expect(page.locator("#report-copy-target-worker")).to_have_value(worker)
                self.assertEqual(mutations, [])
                page.locator("#report-copy-modal").get_by_role("button", name="关闭", exact=True).last.click()
                expect(page.locator("#report-copy-modal")).to_be_hidden()

            page.locator("#btn-copy-test-report").click()
            page.wait_for_function("document.getElementById('report-copy-status').textContent === ''")
            expect(page.locator("#report-copy-target-suite")).to_have_value(target)
            page.locator("#report-copy-target-suite").select_option(source)
            expect(page.locator("#report-copy-submit")).to_be_disabled()
            page.locator("#report-copy-target-suite").select_option(target)
            expect(page.locator("#report-copy-submit")).to_be_enabled()
            page.locator("#report-copy-submit").click()
            expect(page.locator("#report-copy-status")).to_have_text("拷贝完成：" + destination)
            self.assertEqual(mutations, [{
                "source_worker_id": worker, "source_suite_path": source, "report_name": report,
                "target_worker_id": worker, "target_suite_path": target,
            }, "import"])
            self.assertEqual(errors, [])
        finally:
            page.close()
