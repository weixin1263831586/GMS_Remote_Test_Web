"""Automation workflows, permissions and workspace selection."""

import json
import re

from tests.runtime_ui.harness import RuntimeUiHarness, expect


class RuntimeAutomationTests(RuntimeUiHarness):
    def test_automation_workflow_and_status_filter_survive_refresh(self):
        page = self.new_page()
        try:
            page.goto(f"{self.base_url}/automation", wait_until="domcontentloaded")
            page.wait_for_function("document.body.dataset.automationReady === 'true'")
            page.locator('[data-workflow="overview"]').focus()
            page.locator('[data-workflow="overview"]').press("ArrowRight")
            expect(page.locator('[data-workflow="create"]')).to_have_attribute(
                "aria-selected", "true"
            )
            page.locator('[data-workflow="create"]').press("End")
            expect(page.locator('[data-workflow="reports"]')).to_have_attribute(
                "aria-selected", "true"
            )
            page.locator('[data-workflow="reports"]').press("Home")
            expect(page.locator('[data-workflow="overview"]')).to_have_attribute(
                "aria-selected", "true"
            )
            page.evaluate("switchWorkflowPane('runs'); setStatusFilter('queued')")
            expect(page.locator('[data-workflow="runs"]')).to_have_class(
                re.compile(r"\bactive\b")
            )
            expect(page.locator('[data-status="queued"]')).to_have_class(
                re.compile(r"\bactive\b")
            )

            page.reload(wait_until="domcontentloaded")
            page.wait_for_function("document.body.dataset.automationReady === 'true'")

            expect(page.locator('[data-workflow="runs"]')).to_have_attribute(
                "aria-selected", "true"
            )
            expect(page.locator("#workflow-pane-runs")).to_have_class(
                re.compile(r"\bactive\b")
            )
            expect(page.locator('[data-status="queued"]')).to_have_class(
                re.compile(r"\bactive\b")
            )
            self.assertEqual(
                page.evaluate("({pane: activeWorkflowPane, status: atsStatus})"),
                {"pane": "runs", "status": "queued"},
            )
            self.assertIn("tab=runs", page.url)
            self.assertIn("status=queued", page.url)
        finally:
            page.close()

    def test_automation_workbench_buttons_create_and_advance_stub_run(self):
        page = self.new_page()
        runs = []

        def fulfill_automation(route):
            path = route.request.url.split("?", 1)[0]
            method = route.request.method
            data = {}
            if path.endswith("/api/automation/dashboard"):
                data = {"run_total": len(runs), "run_by_status": {}, "run_by_profile": {}}
            elif path.endswith("/api/automation/profiles"):
                data = {"items": []}
            elif path.endswith("/api/automation/runs/preflight"):
                data = {
                    "ready": True,
                    "worker_id": "ats-worker-controller",
                    "test_type": "CTS",
                    "test_suite": "",
                    "devices": ["TESTSERIAL001"],
                }
            elif path.endswith("/api/automation/runs") and method == "POST":
                run = {
                    "id": "ats_ui_smoke",
                    "profile_id": "manual",
                    "status": "queued",
                    "current_stage": "queued",
                    "artifact_path": "/tmp/update.img",
                    "devices_json": '["TESTSERIAL001"]',
                    "report_timestamp": "2026-07-16T10:00:00Z",
                    "report_id": "report-ui-smoke",
                }
                runs[:] = [run]
                data = run
            elif path.endswith("/api/automation/runs"):
                data = {"items": runs}
            elif path.endswith("/api/automation/worker/tick"):
                data = runs[0] if runs else None
            elif path.endswith("/timeline"):
                data = {"items": [{
                    "created_at": "2026-07-16T09:59:00Z",
                    "stage": "testing",
                    "domain": "cluster",
                    "event_type": "command.acknowledged",
                    "level": "info",
                    "message": "Tradefed started",
                }]}
            elif path.endswith("/api/automation/worker/status"):
                data = {"running": False, "interval_seconds": 5, "last_tick_seconds_ago": None}
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"success": True, "data": data}),
            )

        page.route("**/api/automation/**", fulfill_automation)
        page.route(
            "**/api/cluster/status",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body='{"success":true,"enabled":false,"local_worker_id":"ats-worker-controller"}',
            ),
        )
        page.route(
            "**/api/devices/list*",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body='[{"id":"TESTSERIAL001","status":"device","locked":false}]',
            ),
        )
        page.route(
            "**/api/test/suites",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body='{"suites":[]}',
            ),
        )
        try:
            page.goto(f"{self.base_url}/automation", wait_until="domcontentloaded")
            page.wait_for_function("document.body.dataset.automationReady === 'true'")
            page.evaluate("document.querySelector('button[data-workflow=\"create\"]').click()")
            page.wait_for_selector("#automation-create-run")
            page.fill("#automation-artifact", "/tmp/update.img")
            expect(page.locator("#artifact-mode-hint")).to_contain_text("直接使用已有固件")
            expect(page.locator("#build-server")).to_be_disabled()
            page.check('#automation-device-list input[value="TESTSERIAL001"]', force=True)
            page.once("dialog", lambda dialog: dialog.accept())
            page.evaluate("document.getElementById('automation-create-run').click()")
            expect(page.locator("#automation-toast")).to_contain_text("流水线已启动")
            expect(page.locator("#automation-events .event-line")).to_have_count(1)
            expect(page.locator("#automation-events .event-line-message")).to_contain_text("Tradefed started")
            page.evaluate("switchWorkflowPane('reports')")
            expect(page.locator("#automation-runs-report .report-card")).to_have_count(1)
            expect(page.locator("#automation-runs-report")).to_contain_text("分析报告")
            page.evaluate("switchWorkflowPane('runs')")
            page.evaluate(
                """async () => {
                    const response = await fetch('/api/automation/worker/tick?executor=stub', {method: 'POST'});
                    if (!response.ok) throw new Error(`stub tick failed: ${response.status}`);
                    await loadRuns();
                }"""
            )
            page.evaluate("document.querySelector('button[data-status=\"queued\"]').click()")
            expect(page.locator('button[data-status="queued"]')).to_have_class(re.compile(r"active"))
        finally:
            page.close()

    def test_automation_admin_action_prompts_and_retries_after_elevation(self):
        page = self.new_page()
        tick_attempts = []

        def fulfill_automation(route):
            path = route.request.url.split("?", 1)[0]
            if path.endswith("/api/automation/worker/tick"):
                tick_attempts.append(route.request.url)
                if len(tick_attempts) == 1:
                    route.fulfill(
                        status=403,
                        content_type="application/json",
                        body=json.dumps({
                            "detail": {
                                "message": "Elevation required",
                                "elevation_required": True,
                            }
                        }),
                    )
                    return
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"success": True, "data": None}),
            )

        page.route("**/api/automation/**", fulfill_automation)
        page.route(
            "**/api/cluster/status",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({
                    "success": True,
                    "enabled": False,
                    "local_worker_id": "ats-worker-controller",
                }),
            ),
        )
        page.route(
            "**/api/devices/list*",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body="[]",
            ),
        )
        page.route(
            "**/api/test/suites",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body='{"suites":[]}',
            ),
        )
        try:
            page.goto(f"{self.base_url}/automation", wait_until="domcontentloaded")
            page.wait_for_function(
                "document.body.dataset.automationReady === 'true'"
            )
            result = page.evaluate(
                """async () => {
                    window.__elevationLabels = [];
                    window.requestElevatedAccess = async label => {
                        window.__elevationLabels.push(label);
                        return true;
                    };
                    const response = await api(
                        '/api/automation/worker/tick?executor=stub',
                        {method: 'POST'}
                    );
                    return {
                        response,
                        labels: window.__elevationLabels,
                        allocatedLabel: statusLabel('allocated'),
                        failedLabel: statusLabel('test_failed')
                    };
                }"""
            )
            self.assertEqual(len(tick_attempts), 2)
            self.assertEqual(
                result["labels"],
                ["执行 GMS ATS 管理操作"],
            )
            self.assertEqual(result["allocatedLabel"], "已分配")
            self.assertEqual(result["failedLabel"], "测试失败")
        finally:
            page.close()

    def test_automation_lunch_targets_follow_selected_workspace(self):
        page = self.new_page()

        def fulfill_automation(route):
            path = route.request.url.split("?", 1)[0]
            if path.endswith("/api/automation/dashboard"):
                data = {"run_total": 0, "run_by_status": {}, "run_by_profile": {}}
            elif path.endswith("/api/automation/profiles"):
                data = {"items": [{
                    "id": "manual",
                    "name": "Manual",
                    "build": {},
                    "test_plan": {},
                }]}
            elif path.endswith("/api/automation/runs"):
                data = {"items": []}
            elif path.endswith("/api/automation/worker/status"):
                data = {"running": False, "interval_seconds": 5, "last_tick_seconds_ago": None}
            else:
                data = {"items": []}
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"success": True, "data": data}),
            )

        def fulfill_build(route):
            path = route.request.url.split("?", 1)[0]
            if path.endswith("/api/build/servers"):
                data = {"items": [{
                    "id": "mock-build",
                    "name": "Mock Build",
                    "workspace_root": "/src",
                    "auth": {"type": "env_password"},
                }]}
            elif path.endswith("/api/build/templates"):
                data = {"items": [
                    {
                        "id": "mock-template",
                        "name": "Mock Template",
                        "server_id": "mock-build",
                        "init_commands": ["source build/envsetup.sh", "lunch {lunch_target}"],
                        "command": "{build_command}",
                        "parameters_schema": {
                            "build_command": {"default": "./build.sh -UCKApu -J 8"}
                        },
                    },
                    {
                        "id": "mock-clean-template",
                        "name": "Mock Clean Template",
                        "server_id": "mock-build",
                        "init_commands": ["source build/envsetup.sh", "lunch {lunch_target}"],
                        "command": "{build_command}",
                        "parameters_schema": {
                            "build_command": {"default": "./build.sh -UACKApu -J 8"}
                        },
                    },
                ]}
            elif path.endswith("/api/build/discover/workspaces"):
                data = {"items": ["6_Android16_0623", "other_Android16"]}
            elif path.endswith("/api/build/discover/lunch-options"):
                request = json.loads(route.request.post_data or "{}")
                workspace = request.get("workspace", "")
                data = {"items": (
                    ["rk3576_u-userdebug", "rk3576_u-user"]
                    if workspace.endswith("6_Android16_0623")
                    else ["rk3566_rgo-userdebug"]
                )}
            elif path.endswith("/api/build/jobs"):
                data = {"items": []}
            else:
                data = {}
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"success": True, "data": data}),
            )

        page.route("**/api/automation/**", fulfill_automation)
        page.route("**/api/build/**", fulfill_build)
        page.route(
            "**/api/cluster/**",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body='{"enabled":false,"local_worker_id":"ats-worker-controller","workers":[]}',
            ),
        )
        page.route(
            "**/api/test/suites",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body='{"suites":[]}',
            ),
        )
        page.route(
            "**/api/devices/list*",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body="[]",
            ),
        )
        try:
            page.goto(f"{self.base_url}/automation", wait_until="domcontentloaded")
            page.wait_for_function("document.body.dataset.automationReady === 'true'")
            page.evaluate("document.querySelector('button[data-workflow=\"create\"]').click()")
            guided_copy_layout = page.locator("#workflow-pane-create .guided-copy").first.evaluate(
                """element => ({
                    display: getComputedStyle(element).display,
                    whiteSpace: getComputedStyle(element).whiteSpace,
                })"""
            )
            self.assertEqual(guided_copy_layout, {"display": "flex", "whiteSpace": "nowrap"})
            expect(page.locator("#automation-profile")).to_have_value("manual")
            expect(page.locator("#build-server")).to_have_value("mock-build")
            expect(page.locator("#build-template-hint")).to_contain_text("source build/envsetup.sh")
            expect(page.locator(".build-panel-actions")).to_contain_text("仅编译（调试）")
            build_select_edges = page.locator(".build-source-row").evaluate(
                """element => {
                    const server = element.querySelector('#build-server').getBoundingClientRect();
                    const template = element.querySelector('#build-template').getBoundingClientRect();
                    return {serverTop: server.top, serverBottom: server.bottom,
                            templateTop: template.top, templateBottom: template.bottom};
                }"""
            )
            self.assertAlmostEqual(build_select_edges["serverTop"], build_select_edges["templateTop"], delta=0.5)
            self.assertAlmostEqual(build_select_edges["serverBottom"], build_select_edges["templateBottom"], delta=0.5)
            page.select_option("#build-template", "mock-clean-template")
            expect(page.locator("#build-command")).to_have_value("./build.sh -UACKApu -J 8")
            command_and_targets = page.locator("#automation-build-fields").evaluate(
                """element => {
                    const command = element.querySelector('#build-command').getBoundingClientRect();
                    const workspace = element.querySelector('#build-workspace').getBoundingClientRect();
                    const lunch = element.querySelector('#build-lunch-target').getBoundingClientRect();
                    return {commandTop: command.top, workspaceBottom: workspace.bottom,
                            workspaceTop: workspace.top, lunchTop: lunch.top};
                }"""
            )
            self.assertLess(command_and_targets["workspaceBottom"], command_and_targets["commandTop"])
            self.assertAlmostEqual(
                command_and_targets["workspaceTop"], command_and_targets["lunchTop"], delta=0.5
            )
            self.assertEqual(
                page.locator(".workflow-tab").all_text_contents(),
                ["概览", "创建运行", "运行监控", "构建日志", "事件诊断", "测试报告"],
            )
            page.wait_for_function("!document.querySelector('#build-workspace-refresh').disabled")
            page.evaluate("document.querySelector('#build-workspace-refresh').click()")
            expect(page.locator("#build-password-input")).to_have_count(0)

            expect(page.locator("#build-lunch-status")).to_have_class(re.compile(r"\bready\b"))
            self.assertEqual(
                page.locator("#build-lunch-target option").all_text_contents(),
                ["rk3576_u-userdebug", "rk3576_u-user"],
            )
            expect(page.locator("#build-lunch-status")).to_contain_text("当前源码树")

            page.select_option("#build-workspace", "/src/other_Android16")
            page.wait_for_function(
                "document.querySelector('#build-lunch-target').value === 'rk3566_rgo-userdebug'"
            )
            self.assertEqual(
                page.locator("#build-lunch-target option").all_text_contents(),
                ["rk3566_rgo-userdebug"],
            )
            expect(page.locator("#build-lunch-status")).to_contain_text("当前源码树")
        finally:
            page.close()

    def test_automation_shows_local_controller_and_two_column_device_picker(self):
        page = self.new_page()

        def json_response(route, payload):
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(payload),
            )

        def fulfill_automation(route):
            path = route.request.url.split("?", 1)[0]
            if path.endswith("/api/automation/dashboard"):
                data = {"run_total": 0, "run_by_status": {}, "run_by_profile": {}}
            elif path.endswith("/api/automation/profiles"):
                data = {"items": []}
            elif path.endswith("/api/automation/worker/status"):
                data = {"running": False, "interval_seconds": 5, "last_tick_seconds_ago": None}
            else:
                data = {"items": []}
            json_response(route, {"success": True, "data": data})

        def fulfill_cluster(route):
            path = route.request.url.split("?", 1)[0]
            if path.endswith("/api/cluster/status"):
                payload = {
                    "success": True,
                    "enabled": True,
                    "remote_dispatch_enabled": True,
                    "local_worker_id": "ats-worker-controller",
                }
            elif path.endswith("/api/cluster/workers"):
                payload = {
                    "success": True,
                    "workers": [
                        {
                            "id": "ats-worker-controller",
                            "name": "hcq@172.16.14.233",
                            "address": "172.16.14.233",
                            "status": "online",
                            "agent_version": "controller-0.1.0",
                        },
                        {
                            "id": "worker-1",
                            "name": "ATS Worker",
                            "address": "172.16.14.246",
                            "status": "online",
                            "agent_version": "0.3.1",
                        },
                    ],
                }
            elif path.endswith("/api/cluster/devices"):
                payload = {
                    "success": True,
                    "devices": [
                        {
                            "id": f"worker-1:DEVICE-{index}",
                            "state": "available",
                            "transport": "adb_proxy" if index == 1 else "local_usb",
                            "properties": {
                                "adb_proxy_source_worker_id": "worker-source"
                            } if index == 1 else {},
                        }
                        for index in range(1, 13)
                    ],
                }
            elif path.endswith("/api/cluster/suites"):
                payload = {"success": True, "suites": [
                    {"available": True, "suite_type": "CTS", "suite_version": "15_r1",
                     "tools_path": "/suite/android-cts-15_r1/tools"},
                    {"available": True, "suite_type": "CTS", "suite_version": "16_r2",
                     "tools_path": "/suite/android-cts-16_r2/tools"},
                    {"available": True, "suite_type": "GTS", "suite_version": "14.1-R1",
                     "tools_path": "/suite/android-gts-14.1-R1/tools"},
                ]}
            else:
                payload = {"success": True}
            json_response(route, payload)

        page.route("**/api/automation/**", fulfill_automation)
        page.route("**/api/cluster/**", fulfill_cluster)
        try:
            page.goto(f"{self.base_url}/automation", wait_until="domcontentloaded")
            page.wait_for_function("document.body.dataset.automationReady === 'true'")
            page.evaluate("document.querySelector('button[data-workflow=\"create\"]').click()")
            page.wait_for_function(
                "document.querySelectorAll('#automation-device-list input').length === 12"
            )

            local_option = page.locator('#automation-worker option[value="ats-worker-controller"]')
            expect(local_option).to_have_attribute("disabled", "")
            expect(local_option).to_contain_text("172.16.14.233")
            expect(local_option).to_contain_text("未安装 ATS Agent")
            expect(page.locator("#automation-worker")).to_have_value("worker-1")
            expect(page.locator("#automation-devices")).to_have_count(0)
            columns = page.locator("#automation-device-list").evaluate(
                "element => getComputedStyle(element).gridTemplateColumns.split(' ').length"
            )
            self.assertEqual(columns, 2)
            device_list_size = page.locator("#automation-device-list").evaluate(
                "element => ({height: element.clientHeight, scrollHeight: element.scrollHeight})"
            )
            self.assertGreaterEqual(device_list_size["height"], 112)
            self.assertLessEqual(device_list_size["height"], 124)
            self.assertGreater(device_list_size["scrollHeight"], device_list_size["height"])
            device_header_layout = page.locator(".device-field-copy").evaluate(
                """element => ({
                    display: getComputedStyle(element).display,
                    whiteSpace: getComputedStyle(element).whiteSpace,
                })"""
            )
            self.assertEqual(
                device_header_layout,
                {"display": "flex", "whiteSpace": "nowrap"},
            )
            expect(page.locator("#automation-device-list")).to_contain_text(
                "ADB Proxy · worker-source · 仅免刷机测试"
            )
            adb_proxy = page.locator('#automation-device-list input[data-transport="adb_proxy"]')
            expect(adb_proxy).to_be_disabled()
            page.select_option("#automation-flash-mode", "skip")
            expect(adb_proxy).to_be_enabled()
            expect(page.locator("#flash-mode-hint")).to_contain_text("仅测试")
            self.assertEqual(
                page.locator("#automation-test-type option").all_text_contents(),
                ["CTS", "GSI", "GTS", "GTS-ROOT", "STS", "VTS", "APTS"],
            )
            expect(page.locator("#automation-test-suite")).to_have_value(
                "/suite/android-cts-16_r2/tools"
            )
            self.assertIn(
                "/suite/android-gts-14.1-R1/tools",
                page.locator("#automation-test-suite option").all_text_contents(),
            )
            page.select_option("#automation-test-type", "GTS-ROOT")
            expect(page.locator("#automation-test-suite")).to_have_value(
                "/suite/android-gts-14.1-R1/tools"
            )
            page.select_option("#automation-test-type", "APTS")
            expect(page.locator("#automation-test-suite")).to_have_value(
                "/suite/android-gts-14.1-R1/tools"
            )
            create_layout_size = page.locator(".create-layout").evaluate(
                "element => ({height: element.clientHeight, scrollHeight: element.scrollHeight})"
            )
            self.assertLessEqual(
                create_layout_size["scrollHeight"],
                create_layout_size["height"] + 1,
            )
            outer_spacing = page.locator(".ats-shell").evaluate(
                """shell => {
                    const toolbar = shell.querySelector('.ats-toolbar').getBoundingClientRect();
                    const surface = shell.querySelector('.workflow-surface').getBoundingClientRect();
                    const layout = shell.querySelector('.create-layout').getBoundingClientRect();
                    const profile = shell.querySelector('.profile-panel').getBoundingClientRect();
                    const launch = shell.querySelector('.launch-panel').getBoundingClientRect();
                    const shellRect = shell.getBoundingClientRect();
                    return {
                        shellTop: shellRect.top,
                        toolbarTop: toolbar.top,
                        toolbarBottom: toolbar.bottom,
                        surfaceTop: surface.top,
                        layoutTop: layout.top,
                        profileTop: profile.top,
                        launchBottom: launch.bottom,
                        shellBottom: shellRect.bottom,
                        layoutBottom: layout.bottom,
                    };
                }"""
            )
            self.assertLessEqual(
                outer_spacing["toolbarTop"] - outer_spacing["shellTop"],
                8,
            )
            self.assertLessEqual(
                outer_spacing["surfaceTop"] - outer_spacing["toolbarBottom"],
                6,
            )
            self.assertLessEqual(
                outer_spacing["layoutBottom"] - outer_spacing["launchBottom"],
                1,
            )
            for workflow_name in ["overview", "create", "runs", "build", "events", "reports"]:
                page.locator(f'.workflow-tab[data-workflow="{workflow_name}"]').click()
                pane_top_gap = page.locator(f"#workflow-pane-{workflow_name}").evaluate(
                    """pane => {
                        const surface = pane.closest('.workflow-surface').getBoundingClientRect();
                        return pane.getBoundingClientRect().top - surface.top;
                    }"""
                )
                self.assertLessEqual(abs(pane_top_gap), 1)
            page.locator('.workflow-tab[data-workflow="create"]').click()
            page.set_viewport_size({"width": 1440, "height": 720})
            create_panel_edges = page.locator("#workflow-pane-create").evaluate(
                """element => {
                    const testPanel = element.querySelector('.test-panel').getBoundingClientRect();
                    const testContent = element.querySelector('.test-panel .advanced-test-options').getBoundingClientRect();
                    const launchPanel = element.querySelector('.launch-panel').getBoundingClientRect();
                    return {
                        testPanelBottom: testPanel.bottom,
                        testContentBottom: testContent.bottom,
                        launchPanelTop: launchPanel.top,
                    };
                }"""
            )
            self.assertLessEqual(
                create_panel_edges["testContentBottom"],
                create_panel_edges["testPanelBottom"],
            )
            self.assertLess(
                create_panel_edges["testPanelBottom"],
                create_panel_edges["launchPanelTop"],
            )
        finally:
            page.close()
