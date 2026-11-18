"""Notification actions should navigate to the exact owned business result."""
import json

from tests.runtime_ui.harness import RuntimeUiHarness, expect


class NotificationNavigationTests(RuntimeUiHarness):
    def test_completed_cts_notice_opens_matching_report_and_can_return(self):
        page = self.new_page()
        try:
            self.goto_shell(page)
            page.wait_for_function(
                "typeof openNotification === 'function' "
                "&& typeof focusReportFromNotification === 'function'"
            )
            wanted = {
                "report_id": "cluster:job-cts:attempt-1",
                "timestamp": "cluster-job-cts",
                "cluster_job_id": "job-cts",
                "attempt_id": "attempt-1",
                "worker_id": "ats-worker-controller",
                "test_type": "CTS",
                "suite_path": "/suite/tools",
                "report_name": "2026.11.18_14.55.10.142_9114",
                "owner_id": "hidden-from-browser",
                "pass": 10, "fail": 1, "total": 11,
            }
            requests = []

            def report_response(route):
                requests.append(route.request.url)
                if "report_id=cluster" in route.request.url:
                    route.fulfill(
                        status=200, content_type="application/json",
                        body=json.dumps({"reports": [wanted], "next_cursor": ""}),
                    )
                else:
                    route.fulfill(
                        status=200, content_type="application/json",
                        body=json.dumps({"reports": [], "next_cursor": ""}),
                    )

            page.route("**/api/reports/list*", report_response)
            page.evaluate("""() => {
                state.notifications = [{
                    id: 'cts-notice', category: 'cluster', level: 'success',
                    title: 'CTS:17_r2 测试完成', message: 'Worker: ats-worker-controller',
                    read: false,
                    data: {
                        job_id: 'job-cts', cluster_job_id: 'job-cts',
                        attempt_id: 'attempt-1',
                        report_id: 'cluster:job-cts:attempt-1',
                        report_timestamp: 'cluster-job-cts',
                        command_type: 'start_test', status: 'completed',
                    }
                }];
                state.unreadNotifications = 1;
                renderNotificationList();
                document.getElementById('notification-panel').classList.add('show');
            }""")
            page.locator('#notification-list [data-notification-id="cts-notice"]').click()
            page.wait_for_function(
                "() => document.getElementById('page-reports').classList.contains('active')"
            )
            row = page.locator(
                '#reports-table-body tr[data-report-id="cluster:job-cts:attempt-1"]'
            )
            expect(row).to_have_count(1)
            expect(row).to_have_class("notification-report-target")
            expect(row).to_contain_text("2026.11.18_14.55.10.142_9114")
            expect(page.get_by_role('button', name='返回全部报告')).to_be_visible()
            self.assertTrue(any("report_id=cluster" in url for url in requests))
            self.assertEqual(page.evaluate(
                "state.notifications.find(n=>n.id==='cts-notice').read"
            ), True)
            page.get_by_role('button', name='返回全部报告').click()
            expect(page.locator('.notification-report-target')).to_have_count(0)
        finally:
            page.close()

    def test_destination_uses_structured_metadata_not_message_text(self):
        page = self.new_page()
        try:
            self.goto_shell(page)
            destinations = page.evaluate("""() => {
                const make = (category, data) =>
                    notificationDestination({title: '无关标题', category, data});
                return [
                    make('cluster', {command_type: 'start_test', status: 'completed',
                                     job_id: 'job-1'}),
                    make('cluster', {command_type: 'start_test', status: 'failed',
                                     job_id: 'job-2'}),
                    make('apk', {task_id: 'apk-3'}),
                    make('automation', {run_id: 'run-4'}),
                    make('redmine', {redmine_issue_id: '12345'}),
                    make('suite-download', {worker_id: 'w-1', archive_path: 'cts.zip'}),
                    make('system', {}),
                ];
            }""")
            self.assertEqual([d["kind"] if d else None for d in destinations], [
                "report", "workspace", "apk", "workspace", "workspace",
                "workspace", None,
            ])
            self.assertEqual(destinations[0]["anchor"]["cluster_job_id"], "job-1")
            self.assertEqual(destinations[1]["page"], "cluster")
            self.assertEqual(destinations[2]["taskId"], "apk-3")
            self.assertEqual(destinations[3]["context"]["automation_run_id"], "run-4")
            self.assertEqual(destinations[4]["context"]["redmine_issue_id"], "12345")
            self.assertEqual(destinations[5]["context"]["worker_id"], "w-1")
        finally:
            page.close()
