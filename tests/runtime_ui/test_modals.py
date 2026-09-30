"""Modal behavior, focus and supported viewport layouts."""

import json
import re

from tests.runtime_ui.harness import RuntimeUiHarness, expect


class RuntimeModalsTests(RuntimeUiHarness):
    def test_notification_panel_only_closes_on_outside_mouse_click(self):
        page = self.new_page()
        page_errors = []
        page.on("pageerror", lambda exc: page_errors.append(str(exc)))
        try:
            self.goto_shell(page)
            page.wait_for_timeout(500)
            self.close_initial_modals(page)
            toggle = page.locator("#notification-toggle")
            panel = page.locator("#notification-panel")

            toggle.click()
            expect(panel).to_have_class(re.compile(r"\bshow\b"))

            # All mouse interactions inside the panel keep it open so its
            # notification items and action buttons remain usable.
            header = page.locator("#notification-panel .notification-panel-header")
            header.click()
            expect(panel).to_have_class(re.compile(r"\bshow\b"))
            header.click(button="right")
            expect(panel).to_have_class(re.compile(r"\bshow\b"))
            page.get_by_role("button", name="全部已读").click()
            expect(panel).to_have_class(re.compile(r"\bshow\b"))

            expect(page.locator("#notification-dismiss-layer")).to_be_visible()
            page.mouse.click(800, 160)
            expect(panel).not_to_have_class(re.compile(r"\bshow\b"))

            toggle.click()
            expect(panel).to_have_class(re.compile(r"\bshow\b"))
            page.mouse.click(800, 160, button="right")
            expect(panel).not_to_have_class(re.compile(r"\bshow\b"))

            # The parent-layer also covers embedded iframe surfaces, whose
            # document events cannot bubble into the shell document.
            page.evaluate("switchPage('architecture', null)")
            frame_box = page.locator("#architecture-iframe").bounding_box()
            self.assertIsNotNone(frame_box)
            toggle.click()
            expect(panel).to_have_class(re.compile(r"\bshow\b"))
            page.mouse.click(
                frame_box["x"] + frame_box["width"] * 0.75,
                frame_box["y"] + frame_box["height"] * 0.25,
            )
            expect(panel).not_to_have_class(re.compile(r"\bshow\b"))
            self.assert_no_page_errors(page_errors)
        finally:
            page.close()

    def test_adb_proxy_modal_offers_only_remaining_source_devices(self):
        page = self.new_page()
        try:
            self.goto_shell(page)
            result = page.evaluate(
                """
                () => {
                    adbProxyStatus = {
                        cluster_enabled: true,
                        hosts: [
                            {
                                worker_id: 'worker-source',
                                name: 'Device Host',
                                address: '172.16.14.233',
                                status: 'busy',
                                adb_proxy: true,
                                devices: [
                                    {serial: 'RK-A', state: 'available', transport: 'local_usb'},
                                    {serial: 'RK-B', state: 'available', transport: 'local_usb'}
                                ]
                            },
                            {
                                worker_id: 'worker-target',
                                name: 'Test Host',
                                address: '172.16.14.246',
                                status: 'online',
                                adb_proxy: true,
                                devices: [
                                    {serial: 'RK-A', state: 'available', transport: 'adb_proxy'}
                                ]
                            }
                        ],
                        assignments: [{
                            source_worker_id: 'worker-source',
                            source_name: 'Device Host',
                            target_worker_id: 'worker-target',
                            target_name: 'Test Host',
                            devices: ['RK-A'],
                            status: 'connected'
                        }]
                    };
                    adbProxyOperationRunning = false;
                    renderAdbProxyHosts();
                    renderAdbProxyAssignments();
                    return {
                        source: document.getElementById('adb-proxy-source-host').value,
                        sourceLabel: document.getElementById(
                            'adb-proxy-source-host'
                        ).selectedOptions[0]?.textContent,
                        target: document.getElementById('adb-proxy-target-host').value,
                        targetLabel: document.getElementById(
                            'adb-proxy-target-host'
                        ).selectedOptions[0]?.textContent,
                        devices: Array.from(
                            document.getElementById('adb-proxy-source-devices').options
                        ).map(option => option.value).filter(Boolean),
                        assignment: document.getElementById(
                            'adb-proxy-assignments'
                        ).textContent,
                        message: document.getElementById('adb-proxy-message').textContent,
                        submitDisabled: document.getElementById(
                            'adb-proxy-connect-submit'
                        ).disabled
                    };
                }
                """
            )

            self.assertEqual(result["source"], "worker-source")
            self.assertEqual(result["sourceLabel"], "worker-source")
            self.assertEqual(result["target"], "worker-target")
            self.assertEqual(result["targetLabel"], "worker-target")
            self.assertEqual(result["devices"], ["RK-B"])
            self.assertIn(
                "worker-source → worker-target｜设备：RK-A",
                result["assignment"],
            )
            self.assertNotIn("172.16.14.", result["assignment"])
            self.assertNotIn("connected", result["assignment"])
            self.assertIn("还有 1 台", result["message"])
            self.assertFalse(result["submitDisabled"])
        finally:
            page.close()

    def test_adb_proxy_modal_supports_compact_source_only_ubuntu_host(self):
        page = self.new_page()
        try:
            self.goto_shell(page)
            result = page.evaluate(
                """
                () => {
                    adbProxyStatus = {
                        cluster_enabled: false,
                        hosts: [
                            {
                                worker_id: 'ats-worker-controller',
                                name: 'Controller Local Worker',
                                address: '172.16.14.233',
                                status: 'online',
                                adb_proxy: true,
                                adb_proxy_source_only: false,
                                devices: []
                            },
                            {
                                worker_id: 'adb-source-246',
                                name: 'Ubuntu ADB来源',
                                address: '172.16.14.246',
                                status: 'online',
                                adb_proxy: true,
                                adb_proxy_source_only: true,
                                devices: [{
                                    serial: 'RK3576GMS1',
                                    state: 'available',
                                    transport: 'local_usb'
                                }]
                            }
                        ],
                        assignments: []
                    };
                    adbProxyOperationRunning = false;
                    renderAdbProxyHosts();
                    ModalManager.open('adb-proxy-modal');
                    return {
                        sources: Array.from(
                            document.getElementById('adb-proxy-source-host').options
                        ).map(option => option.value).filter(Boolean),
                        targets: Array.from(
                            document.getElementById('adb-proxy-target-host').options
                        ).map(option => option.value).filter(Boolean),
                        hasUbuntuForm: Boolean(
                            document.getElementById('adb-proxy-ubuntu-host')
                            && document.getElementById('adb-proxy-ubuntu-password')
                        )
                    };
                }
                """
            )

            self.assertEqual(result["sources"], ["adb-source-246"])
            self.assertEqual(result["targets"], ["ats-worker-controller"])
            self.assertTrue(result["hasUbuntuForm"])
            source_box = page.locator("#adb-proxy-source-host").bounding_box()
            target_box = page.locator("#adb-proxy-target-host").bounding_box()
            self.assertGreater(target_box["y"], source_box["y"] + source_box["height"])
            modal_height = page.locator(
                "#adb-proxy-modal .adb-proxy-modal-content"
            ).bounding_box()["height"]
            self.assertLess(modal_height, 550)
            expect(
                page.locator("#adb-proxy-modal .modal-content")
            ).to_have_class(re.compile(r"\bdevice-routing-modal-content\b"))
        finally:
            page.close()

    def test_adb_proxy_modal_keeps_busy_targets_visible_but_disabled(self):
        page = self.new_page()
        try:
            self.goto_shell(page)
            result = page.evaluate(
                """
                () => {
                    adbProxyStatus = {
                        cluster_enabled: true,
                        local_worker_id: 'ats-worker-controller',
                        hosts: [
                            {
                                worker_id: 'ats-worker-244',
                                status: 'online',
                                adb_proxy: true,
                                devices: [{
                                    serial: 'ATS244001',
                                    state: 'available',
                                    transport: 'local_usb'
                                }]
                            },
                            {
                                worker_id: 'ats-worker-246',
                                status: 'busy',
                                adb_proxy: true,
                                devices: []
                            },
                            {
                                worker_id: 'ats-worker-controller',
                                status: 'busy',
                                adb_proxy: true,
                                devices: []
                            }
                        ],
                        assignments: []
                    };
                    adbProxyOperationRunning = false;
                    renderAdbProxyHosts();
                    return {
                        target: document.getElementById('adb-proxy-target-host').value,
                        options: Array.from(
                            document.getElementById('adb-proxy-target-host').options
                        ).map(option => ({
                            value: option.value,
                            label: option.textContent,
                            disabled: option.disabled
                        })),
                        message: document.getElementById('adb-proxy-message').textContent,
                        submitDisabled: document.getElementById(
                            'adb-proxy-connect-submit'
                        ).disabled
                    };
                }
                """
            )

            self.assertEqual(result["target"], "")
            self.assertEqual(
                result["options"],
                [
                    {
                        "value": "",
                        "label": "没有可用的ADB接入主机",
                        "disabled": True,
                    },
                    {
                        "value": "ats-worker-246",
                        "label": "ats-worker-246（测试中，不可用）",
                        "disabled": True,
                    },
                    {
                        "value": "ats-worker-controller",
                        "label": "ats-worker-controller（测试中，不可用）",
                        "disabled": True,
                    },
                ],
            )
            self.assertIn("ats-worker-246", result["message"])
            self.assertIn("正在执行测试", result["message"])
            self.assertTrue(result["submitDisabled"])
        finally:
            page.close()

    def test_sidebar_visibility_modal_closes_with_escape(self):
        page = self.new_page()
        try:
            self.goto_shell(page)
            page.locator(".sidebar-brand").click()
            modal = page.locator("#sidebar-visibility-modal")
            expect(modal).to_have_class(re.compile(r"show"))
            page.keyboard.press("Escape")
            expect(modal).not_to_have_class(re.compile(r"show"))
        finally:
            page.close()

    def test_report_copy_modal_has_stable_layout_and_actual_suite_names(self):
        page = self.new_page()

        def json_response(route, payload):
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(payload),
            )

        source_suite = {
            "available": True,
            "suite_type": "GTS",
            "test_type": "gts",
            "suite_version": "14",
            "version": "android-gts-14",
            "suite_key": "GTS:14",
            "tools_path": (
                "/home/source/GMS-Suite/android-gts-14-R1-15492250/android-gts/tools"
            ),
        }
        target_suites = [
            {
                **source_suite,
                "tools_path": "/home/target/GMS-Suite/android-gts-14/android-gts/tools",
            },
            {
                **source_suite,
                "tools_path": (
                    "/home/target/GMS-Suite/"
                    "android-gts-14-R1-15492250/android-gts/tools"
                ),
            },
        ]

        def fulfill_cluster(route):
            url = route.request.url
            path = url.split("?", 1)[0]
            if path.endswith("/api/cluster/status"):
                payload = {
                    "success": True,
                    "enabled": True,
                    "local_worker_id": "ats-worker-controller",
                }
            elif path.endswith("/api/cluster/workers"):
                payload = {
                    "success": True,
                    "workers": [
                        {
                            "id": "ats-worker-controller",
                            "address": "192.0.2.10",
                            "status": "online",
                        },
                        {
                            "id": "worker-a",
                            "address": "192.0.2.11",
                            "status": "online",
                        },
                    ],
                }
            elif path.endswith("/api/cluster/suites/files"):
                payload = {
                    "success": True,
                    "data": {"items": [
                        {
                            "name": "2026.08.07_15.56.09.558_3101",
                            "type": "directory",
                        },
                        {
                            "name": "2026.07.30_10.39.50.173_6846",
                            "type": "directory",
                        },
                    ]},
                }
            elif path.endswith("/api/cluster/suites"):
                payload = {
                    "success": True,
                    "suites": target_suites if "worker_id=worker-a" in url else [source_suite],
                }
            else:
                payload = {"success": True}
            json_response(route, payload)

        page.route("**/api/cluster/**", fulfill_cluster)
        page.add_init_script("localStorage.setItem('gms_current_page','test-suites');")
        try:
            self.goto_shell(page)
            page.wait_for_function("typeof openReportCopyModal === 'function'")
            page.evaluate("void openReportCopyModal()")

            modal = page.locator("#report-copy-modal .report-copy-modal-content")
            expect(modal).to_be_visible()
            expect(page.locator("#report-copy-target-suite option")).to_have_count(2)
            expect(page.locator("#report-copy-source-report option")).to_have_count(2)

            self.assertEqual(
                page.locator("#report-copy-source-report option").all_text_contents(),
                [
                    "2026.07.30_10.39.50.173_6846",
                    "2026.08.07_15.56.09.558_3101",
                ],
            )
            self.assertEqual(
                page.locator("#report-copy-target-suite option").all_text_contents(),
                ["android-gts-14", "android-gts-14-R1-15492250"],
            )
            expect(page.locator("#report-copy-target-suite")).to_have_value(
                "/home/target/GMS-Suite/"
                "android-gts-14-R1-15492250/android-gts/tools"
            )

            initial_box = modal.bounding_box()
            self.assertIsNotNone(initial_box)
            self.assertAlmostEqual(initial_box["width"], 760, delta=0.5)
            self.assertAlmostEqual(initial_box["height"], 500, delta=0.5)
            page.evaluate(
                "setReportCopyStatus('状态内容出现后，弹框外框尺寸仍然保持固定。', 'info')"
            )
            status_box = modal.bounding_box()
            self.assertIsNotNone(status_box)
            self.assertAlmostEqual(status_box["width"], initial_box["width"], delta=0.5)
            self.assertAlmostEqual(status_box["height"], initial_box["height"], delta=0.5)

            select_metrics = page.locator("#report-copy-target-suite").evaluate(
                """select => {
                    const style = getComputedStyle(select);
                    return {
                        height: select.getBoundingClientRect().height,
                        fontSize: parseFloat(style.fontSize),
                        paddingTop: parseFloat(style.paddingTop),
                        paddingBottom: parseFloat(style.paddingBottom),
                        clientHeight: select.clientHeight,
                    };
                }"""
            )
            self.assertAlmostEqual(select_metrics["height"], 36, delta=0.5)
            self.assertLessEqual(
                select_metrics["fontSize"]
                + select_metrics["paddingTop"]
                + select_metrics["paddingBottom"],
                select_metrics["clientHeight"],
            )
        finally:
            page.close()

    def test_main_shell_static_and_dynamic_modals_close_with_escape(self):
        page = self.new_page()
        page_errors = []
        page.on("pageerror", lambda exc: page_errors.append(str(exc)))
        try:
            self.goto_shell(page)
            page.wait_for_function("typeof ModalManager === 'object'")
            modal_ids = page.locator(".modal[id]").evaluate_all(
                "(items) => items.map(item => item.id).filter(Boolean)"
            )
            self.assertGreater(len(modal_ids), 10)

            for modal_id in modal_ids:
                with self.subTest(modal=modal_id):
                    page.evaluate("id => ModalManager.open(id)", modal_id)
                    expect(page.locator(f"#{modal_id}")).to_have_class(re.compile(r"show"))
                    page.keyboard.press("Escape")
                    expect(page.locator(f"#{modal_id}")).not_to_have_class(re.compile(r"show"))

            page.evaluate("selectReportSource()")
            expect(page.locator("#report-source-modal")).to_be_visible()
            page.keyboard.press("Escape")
            expect(page.locator("#report-source-modal")).to_have_count(0)

            page.evaluate("showRedmineAuthDialog('https://redmine.local/issues/1', null, null, null, null, {})")
            expect(page.locator("#redmine-auth-modal")).to_be_visible()
            page.keyboard.press("Escape")
            expect(page.locator("#redmine-auth-modal")).to_have_count(0)

            modal_id = page.evaluate("createAnalysisModal('runtime-smoke', '运行时弹框测试', '加载中').modalId")
            expect(page.locator(f"#{modal_id}")).to_be_visible()
            page.keyboard.press("Escape")
            expect(page.locator(f"#{modal_id}")).not_to_have_class(re.compile(r"show"))

            self.assert_no_page_errors(page_errors)
        finally:
            page.close()

    def test_main_shell_form_modals_focus_the_form_instead_of_close_button(self):
        page = self.new_page()
        page_errors = []
        page.on("pageerror", lambda exc: page_errors.append(str(exc)))
        try:
            self.goto_shell(page)
            page.wait_for_function("typeof ModalManager === 'object'")
            for modal_id, expected_focus in (
                ("wifi-modal", "wifi-ssid"),
                ("gms-assistant-config-modal", "gms-assistant-url"),
            ):
                with self.subTest(modal=modal_id):
                    page.evaluate("id => ModalManager.open(id)", modal_id)
                    page.wait_for_function(
                        "id => document.activeElement?.id === id", arg=expected_focus
                    )
                    self.assertFalse(
                        page.locator(f"#{modal_id} .modal-close").evaluate(
                            "button => button === document.activeElement"
                        )
                    )
                    page.evaluate("id => ModalManager.close(id)", modal_id)
            self.assert_no_page_errors(page_errors)
        finally:
            page.close()

    def test_main_shell_modal_focus_skips_css_hidden_and_falls_back_to_content(self):
        """CSS 隐藏控件不参与初始焦点；纯说明弹框回退聚焦内容容器。

        - 首个表单控件 display:none 时聚焦下一个可见输入框（而不是 ×）；
        - 全部控件不可聚焦时聚焦 .modal-content（此前会落到关闭按钮）。
        """
        page = self.new_page()
        page_errors = []
        page.on("pageerror", lambda exc: page_errors.append(str(exc)))
        try:
            self.goto_shell(page)
            page.wait_for_function("typeof ModalManager === 'object'")
            page.evaluate(
                """() => {
                  const host = document.createElement('div');
                  host.id = 'focus-probe-modal';
                  host.className = 'modal';
                  host.innerHTML = (
                    '<div class="modal-content">'
                    + '<div class="modal-header">'
                    + '<span class="modal-title">焦点探针</span>'
                    + '<button type="button" class="modal-close" aria-label="关闭">'
                    + '&times;</button></div>'
                    + '<div class="modal-body">'
                    + '<input id="focus-probe-hidden" type="text" style="display:none">'
                    + '<input id="focus-probe-visible" type="text">'
                    + '</div></div>'
                  );
                  document.body.appendChild(host);
                }"""
            )
            try:
                with self.subTest(case="skips_css_hidden_control"):
                    page.evaluate("ModalManager.open('focus-probe-modal')")
                    page.wait_for_function(
                        "() => document.activeElement?.id === 'focus-probe-visible'")
                with self.subTest(case="falls_back_to_content"):
                    page.evaluate(
                        "() => { document.getElementById('focus-probe-visible')"
                        ".style.display = 'none';"
                        " ModalManager.close('focus-probe-modal');"
                        " ModalManager.open('focus-probe-modal'); }")
                    page.wait_for_function(
                        "() => document.activeElement?.classList"
                        "?.contains('modal-content')")
            finally:
                page.evaluate("ModalManager.close('focus-probe-modal')")
                page.evaluate("document.getElementById('focus-probe-modal')?.remove()")
            self.assert_no_page_errors(page_errors)
        finally:
            page.close()

    def test_cluster_worker_config_modal_focuses_input_and_escapes(self):
        """Cluster 独立控制器：打开即聚焦首个输入框，Escape 关闭弹框。"""
        page = self.new_page()
        page_errors = []
        page.on("pageerror", lambda exc: page_errors.append(str(exc)))
        try:
            page.goto(f"{self.base_url}/cluster", wait_until="domcontentloaded")
            page.wait_for_function("typeof ClusterModalController === 'object'")
            page.evaluate(
                "() => { document.getElementById('worker-config-modal')"
                ".hidden = false; }")
            page.wait_for_function(
                "() => document.activeElement?.id === 'config-max-jobs'")
            self.assertFalse(page.evaluate(
                "() => document.activeElement?.id === 'close-config-modal'"))
            page.keyboard.press("Escape")
            page.wait_for_function(
                "() => document.getElementById('worker-config-modal').hidden")
            self.assert_no_page_errors(page_errors)
        finally:
            page.close()

    def test_main_shell_modals_fit_supported_viewports_and_stack_in_order(self):
        page = self.new_page()
        page_errors = []
        page.on("pageerror", lambda exc: page_errors.append(str(exc)))
        try:
            self.goto_shell(page)
            page.wait_for_function("typeof ModalManager === 'object'")
            modal_ids = page.locator(".modal[id]").evaluate_all(
                "(items) => items.map(item => item.id).filter(Boolean)"
            )
            viewports = [
                {"width": 1440, "height": 960},
                {"width": 768, "height": 720},
                {"width": 390, "height": 844},
                {"width": 844, "height": 390},
            ]

            for viewport in viewports:
                page.set_viewport_size(viewport)
                for modal_id in modal_ids:
                    with self.subTest(viewport=viewport, modal=modal_id):
                        page.evaluate("id => ModalManager.open(id)", modal_id)
                        report = page.locator(f"#{modal_id}").evaluate(
                            """modal => {
                              const content = modal.querySelector('.modal-content');
                              if (!content) return {missingContent: true};
                              const rect = content.getBoundingClientRect();
                              const controls = Array.from(content.querySelectorAll(
                                'button, [role="button"]'
                              )).filter(node => {
                                const style = getComputedStyle(node);
                                return style.display !== 'none' && style.visibility !== 'hidden';
                              });
                              return {
                                missingContent: false,
                                rect: {
                                  left: rect.left,
                                  top: rect.top,
                                  right: rect.right,
                                  bottom: rect.bottom,
                                  width: rect.width,
                                  height: rect.height
                                },
                                viewport: {width: innerWidth, height: innerHeight},
                                overflowControls: controls.filter(node =>
                                  node.clientWidth > 0 && node.scrollWidth > node.clientWidth + 2
                                ).map(node => node.id || node.className || node.tagName).slice(0, 8)
                              };
                            }"""
                        )
                        self.assertFalse(report["missingContent"], report)
                        rect = report["rect"]
                        self.assertGreater(rect["width"], 0, report)
                        self.assertGreater(rect["height"], 0, report)
                        self.assertGreaterEqual(rect["left"], -1, report)
                        self.assertGreaterEqual(rect["top"], -1, report)
                        self.assertLessEqual(rect["right"], report["viewport"]["width"] + 1, report)
                        self.assertLessEqual(rect["bottom"], report["viewport"]["height"] + 1, report)
                        self.assertEqual(report["overflowControls"], [], report)
                        if viewport["width"] >= 768 and viewport["height"] >= 560:
                            self.assertAlmostEqual(
                                (rect["left"] + rect["right"]) / 2,
                                report["viewport"]["width"] / 2,
                                delta=2,
                                msg=report,
                            )
                            self.assertAlmostEqual(
                                (rect["top"] + rect["bottom"]) / 2,
                                report["viewport"]["height"] / 2,
                                delta=2,
                                msg=report,
                            )
                        page.evaluate("id => ModalManager.close(id)", modal_id)

            first_id, second_id = modal_ids[:2]
            page.evaluate(
                "ids => { ModalManager.open(ids[0]); ModalManager.open(ids[1]); }",
                [first_id, second_id],
            )
            stack = page.evaluate(
                """ids => ({
                  active: ModalManager._activeModals.slice(),
                  firstZ: Number(getComputedStyle(document.getElementById(ids[0])).zIndex),
                  secondZ: Number(getComputedStyle(document.getElementById(ids[1])).zIndex),
                  firstInert: document.getElementById(ids[0]).inert,
                  secondInert: document.getElementById(ids[1]).inert
                })""",
                [first_id, second_id],
            )
            self.assertEqual(stack["active"][-2:], [first_id, second_id])
            self.assertGreater(stack["secondZ"], stack["firstZ"])
            self.assertTrue(stack["firstInert"])
            self.assertFalse(stack["secondInert"])
            page.keyboard.press("Escape")
            expect(page.locator(f"#{second_id}")).not_to_have_class(re.compile(r"show"))
            expect(page.locator(f"#{first_id}")).to_have_class(re.compile(r"show"))
            self.assertFalse(page.locator(f"#{first_id}").evaluate("modal => modal.inert"))
            page.keyboard.press("Escape")
            expect(page.locator(".modal.show")).to_have_count(0)
            self.assert_no_page_errors(page_errors)
        finally:
            page.close()

    def test_gms_update_monitor_notification_bridge_reaches_main_shell(self):
        page = self.new_page()
        try:
            self.goto_shell(page)
            page.evaluate(
                """
                const frame = document.createElement('iframe');
                frame.id = 'gms-update-monitor-smoke-frame';
                frame.src = '/gms-update-monitor';
                frame.style.display = 'none';
                document.body.appendChild(frame);
                """
            )
            frame = self.frame_for(page, "#gms-update-monitor-smoke-frame")
            frame.wait_for_function("typeof notifyUser === 'function'")
            frame.evaluate("notifyUser('GMS更新监控通知测试', 'ok', 'success')")
            expect(page.locator(".notification-badge")).to_be_visible()
        finally:
            page.close()

    def test_standalone_pages_keep_overlays_centered_and_inside_viewport(self):
        page = self.new_page()
        page_errors = []
        page.on("pageerror", lambda exc: page_errors.append(str(exc)))

        def assert_overlay_fits(selector, centered=False):
            report = page.locator(selector).evaluate(
                """overlay => {
                  const rect = overlay.getBoundingClientRect();
                  return {
                    left: rect.left, top: rect.top, right: rect.right, bottom: rect.bottom,
                    width: rect.width, height: rect.height,
                    viewportWidth: innerWidth, viewportHeight: innerHeight,
                    className: overlay.className,
                    transform: getComputedStyle(overlay).transform
                  };
                }"""
            )
            self.assertGreater(report["width"], 0, report)
            self.assertGreater(report["height"], 0, report)
            self.assertGreaterEqual(report["left"], -1, report)
            self.assertGreaterEqual(report["top"], -1, report)
            self.assertLessEqual(report["right"], report["viewportWidth"] + 1, report)
            self.assertLessEqual(report["bottom"], report["viewportHeight"] + 1, report)
            if centered:
                self.assertAlmostEqual(
                    (report["left"] + report["right"]) / 2,
                    report["viewportWidth"] / 2,
                    delta=2,
                    msg=report,
                )
                self.assertAlmostEqual(
                    (report["top"] + report["bottom"]) / 2,
                    report["viewportHeight"] / 2,
                    delta=2,
                    msg=report,
                )

        try:
            page.set_viewport_size({"width": 390, "height": 720})

            page.goto(f"{self.base_url}/redmine-agent", wait_until="domcontentloaded")
            page.wait_for_function("typeof showModal === 'function'")
            redmine_ids = page.locator(".modal[id]").evaluate_all(
                "(items) => items.map(item => item.id)"
            )
            for modal_id in redmine_ids:
                page.evaluate("id => showModal(id)", modal_id)
                assert_overlay_fits(f"#{modal_id} .modal-content")
                page.evaluate("id => hideModal(id)", modal_id)

            page.goto(f"{self.base_url}/gerrit-dashboard", wait_until="domcontentloaded")
            page.wait_for_function("typeof showModal === 'function'")
            gerrit_ids = page.locator(".modal[id]").evaluate_all(
                "(items) => items.map(item => item.id)"
            )
            for modal_id in gerrit_ids:
                page.evaluate("id => showModal(id)", modal_id)
                assert_overlay_fits(f"#{modal_id} .modal-content")
                page.evaluate("id => hideModal(id)", modal_id)

            page.goto(f"{self.base_url}/cluster", wait_until="domcontentloaded")
            page.wait_for_function("typeof syncClusterModalState === 'function'")
            page.evaluate(
                """() => {
                  document.getElementById('onboarding').hidden = false;
                  document.getElementById('worker-config-modal').hidden = false;
                  syncClusterModalState();
                }"""
            )
            assert_overlay_fits("#onboarding .onboarding-modal")
            assert_overlay_fits("#worker-config-modal .onboarding-modal")
            self.assertTrue(page.locator("#onboarding").evaluate("modal => modal.inert"))
            page.keyboard.press("Escape")
            expect(page.locator("#worker-config-modal")).to_be_hidden()
            expect(page.locator("#onboarding")).to_be_visible()
            page.keyboard.press("Escape")
            expect(page.locator("#onboarding")).to_be_hidden()

            page.goto(f"{self.base_url}/automation", wait_until="domcontentloaded")
            page.wait_for_function("typeof openTrace === 'function'")
            page.evaluate("openTrace()")
            page.wait_for_function(
                "document.getElementById('ats-trace-drawer').getBoundingClientRect().right <= innerWidth + 1"
            )
            assert_overlay_fits("#ats-trace-drawer")
            page.keyboard.press("Escape")
            expect(page.locator("#ats-trace-drawer")).not_to_have_class(re.compile(r"open"))
            page.evaluate("void promptBuildPassword()")
            expect(page.locator(".password-backdrop")).to_be_visible()
            assert_overlay_fits(".password-dialog")
            page.keyboard.press("Escape")
            expect(page.locator(".password-backdrop")).to_have_count(0)

            # Desktop dialogs from each standalone workspace use the same
            # viewport-centered contract as the main shell modal manager.
            page.set_viewport_size({"width": 1440, "height": 960})
            for path, modal_ids in [
                ("/redmine-agent", redmine_ids),
                ("/gerrit-dashboard", gerrit_ids),
            ]:
                page.goto(f"{self.base_url}{path}", wait_until="domcontentloaded")
                page.wait_for_function("typeof showModal === 'function'")
                for modal_id in modal_ids:
                    with self.subTest(path=path, modal=modal_id):
                        page.evaluate("id => showModal(id)", modal_id)
                        assert_overlay_fits(f"#{modal_id} .modal-content", centered=True)
                        page.evaluate("id => hideModal(id)", modal_id)

            page.goto(f"{self.base_url}/cluster", wait_until="domcontentloaded")
            page.wait_for_function("typeof syncClusterModalState === 'function'")
            for modal_id in ["onboarding", "worker-config-modal"]:
                with self.subTest(path="/cluster", modal=modal_id):
                    page.evaluate(
                        "id => { document.getElementById(id).hidden = false; syncClusterModalState(); }",
                        modal_id,
                    )
                    assert_overlay_fits(
                        f"#{modal_id} .onboarding-modal", centered=True
                    )
                    page.evaluate(
                        "id => { document.getElementById(id).hidden = true; syncClusterModalState(); }",
                        modal_id,
                    )

            self.assert_no_page_errors(page_errors)
        finally:
            page.close()

    def test_standalone_pages_have_no_uncontained_mobile_overflow(self):
        page = self.new_page()
        page_errors = []
        page.on("pageerror", lambda exc: page_errors.append(str(exc)))
        paths = [
            "/automation",
            "/cluster",
            "/redmine-agent",
            "/gerrit-dashboard",
            "/mainline-known-issues",
            "/gms-update-monitor",
            "/templates/architecture.html",
        ]
        viewports = [
            {"width": 390, "height": 720},
            {"width": 844, "height": 390},
        ]
        try:
            for viewport in viewports:
                page.set_viewport_size(viewport)
                for path in paths:
                    with self.subTest(viewport=viewport, path=path):
                        page.goto(f"{self.base_url}{path}", wait_until="domcontentloaded")
                        page.wait_for_timeout(100)
                        report = page.evaluate(
                            """() => {
                              const viewportWidth = innerWidth;
                              const isVisible = node => {
                                const style = getComputedStyle(node);
                                return style.display !== 'none'
                                  && style.visibility !== 'hidden'
                                  && !node.closest('[aria-hidden="true"]')
                                  && node.getClientRects().length > 0;
                              };
                              const hasHorizontalContainer = node => {
                                for (let parent = node.parentElement;
                                     parent && parent !== document.body;
                                     parent = parent.parentElement) {
                                  const overflow = getComputedStyle(parent).overflowX;
                                  if (overflow === 'auto' || overflow === 'scroll') return true;
                                }
                                return false;
                              };
                              const leaks = Array.from(document.querySelectorAll(
                                'button, input, select, textarea, a[href]'
                              )).filter(isVisible).filter(node => {
                                const rect = node.getBoundingClientRect();
                                return (rect.left < -1 || rect.right > viewportWidth + 1)
                                  && !hasHorizontalContainer(node);
                              }).map(node => ({
                                tag: node.tagName,
                                id: node.id,
                                className: String(node.className || ''),
                                text: String(node.textContent || node.value || '').trim().slice(0, 80),
                                rect: {
                                  left: node.getBoundingClientRect().left,
                                  right: node.getBoundingClientRect().right
                                }
                              })).slice(0, 12);
                              const clippedButtons = Array.from(document.querySelectorAll(
                                'button, [role="button"]'
                              )).filter(isVisible).filter(node =>
                                node.clientWidth > 0 && node.scrollWidth > node.clientWidth + 2
                              ).map(node => node.id || String(node.className || '') || node.textContent)
                                .slice(0, 12);
                              return {
                                viewportWidth,
                                documentWidth: document.documentElement.scrollWidth,
                                leaks,
                                clippedButtons
                              };
                            }"""
                        )
                        self.assertLessEqual(
                            report["documentWidth"], report["viewportWidth"] + 1, report
                        )
                        self.assertEqual(report["leaks"], [], report)
                        self.assertEqual(report["clippedButtons"], [], report)
            self.assert_no_page_errors(page_errors)
        finally:
            page.close()

    def test_shell_pages_keep_visible_controls_accessible_and_inside_viewport(self):
        page = self.new_page()
        page_errors = []
        page.on("pageerror", lambda exc: page_errors.append(str(exc)))
        try:
            self.goto_shell(page)
            for viewport in [
                {"width": 1440, "height": 960},
                {"width": 390, "height": 720},
            ]:
                page.set_viewport_size(viewport)
                for page_name in self.visible_sidebar_pages(page):
                    self.show_all_sidebar_pages(page)
                    page.evaluate("name => switchPage(name, null)", page_name)
                    report = page.locator(f"#page-{page_name}").evaluate(
                        """container => {
                          const bounds = container.getBoundingClientRect();
                          const isVisible = node => {
                            const style = getComputedStyle(node);
                            return style.display !== 'none'
                              && style.visibility !== 'hidden'
                              && !node.closest('[aria-hidden="true"]')
                              && node.getClientRects().length > 0;
                          };
                          const hasHorizontalContainer = node => {
                            for (let parent = node.parentElement;
                                 parent && parent !== container;
                                 parent = parent.parentElement) {
                              const overflow = getComputedStyle(parent).overflowX;
                              if (overflow === 'auto' || overflow === 'scroll') return true;
                            }
                            return false;
                          };
                          const controls = Array.from(container.querySelectorAll(
                            'button, input, select, textarea, a[href]'
                          )).filter(isVisible);
                          return {
                            container: {
                              left: bounds.left, right: bounds.right,
                              width: bounds.width, viewportWidth: innerWidth
                            },
                            leaks: controls.filter(node => {
                              const rect = node.getBoundingClientRect();
                              return (rect.left < bounds.left - 1
                                || rect.right > Math.min(bounds.right, innerWidth) + 1)
                                && !hasHorizontalContainer(node);
                            }).map(node => ({
                              tag: node.tagName,
                              id: node.id,
                              text: String(node.textContent || node.value || '').trim().slice(0, 80),
                              rect: {
                                left: node.getBoundingClientRect().left,
                                right: node.getBoundingClientRect().right
                              }
                            })).slice(0, 12),
                            clippedButtons: controls.filter(node =>
                              (node.tagName === 'BUTTON' || node.getAttribute('role') === 'button')
                              && node.clientWidth > 0
                              && node.scrollWidth > node.clientWidth + 2
                            ).map(node => node.id || node.textContent).slice(0, 12),
                            undersizedPointerTargets: Array.from(container.querySelectorAll(
                              'button, [role="button"], select, input[type="button"], '
                                + 'input[type="submit"], input[type="reset"], input[type="file"]'
                            )).filter(isVisible).filter(node => {
                              const rect = node.getBoundingClientRect();
                              return rect.width < 24 || rect.height < 24;
                            }).map(node => {
                              const rect = node.getBoundingClientRect();
                              return {
                                tag: node.tagName,
                                id: node.id,
                                className: node.className,
                                text: String(node.textContent || node.value || '').trim().slice(0, 80),
                                width: rect.width,
                                height: rect.height
                              };
                            }).slice(0, 20)
                          };
                        }"""
                    )
                    self.assertGreater(report["container"]["width"], 0, report)
                    self.assertGreaterEqual(report["container"]["left"], -1, report)
                    self.assertLessEqual(
                        report["container"]["right"],
                        report["container"]["viewportWidth"] + 1,
                        report,
                    )
                    self.assertEqual(report["leaks"], [], report)
                    self.assertEqual(report["clippedButtons"], [], report)
                    self.assertEqual(report["undersizedPointerTargets"], [], report)
            self.assert_no_page_errors(page_errors)
        finally:
            page.close()
