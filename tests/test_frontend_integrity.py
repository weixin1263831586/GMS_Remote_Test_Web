import re
import unittest
from pathlib import Path


CALL_ATTR_RE = re.compile(r'on(?:click|change|input|submit|keydown|mouseover|mouseout)=["\']([^"\']+)["\']')
FUNCTION_RE = re.compile(r'\bfunction\s+([A-Za-z_$][\w$]*)\s*\(')
WINDOW_ASSIGN_RE = re.compile(r'\bwindow\.([A-Za-z_$][\w$]*)\s*=')
CONST_FUNCTION_RE = re.compile(
    r'\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*'
    r'(?:async\s*)?(?:function\b|\([^)]*\)\s*=>|[A-Za-z_$][\w$]*\s*=>)'
)
BUILTINS = {
    "Array",
    "Boolean",
    "Date",
    "JSON",
    "Math",
    "Number",
    "Object",
    "Promise",
    "String",
    "alert",
    "clearTimeout",
    "confirm",
    "console",
    "decodeURIComponent",
    "document",
    "encodeURIComponent",
    "event",
    "fetch",
    "if",
    "navigator",
    "setTimeout",
    "this",
    "window",
}


def read_text(path: str) -> str:
    return Path(path).read_text(encoding="utf-8", errors="ignore")


def declared_functions(text: str) -> set[str]:
    return (
        set(FUNCTION_RE.findall(text))
        | set(WINDOW_ASSIGN_RE.findall(text))
        | set(CONST_FUNCTION_RE.findall(text))
        | BUILTINS
    )


def inline_handler_calls(text: str) -> list[tuple[str, str]]:
    calls = []
    for body in CALL_ATTR_RE.findall(text):
        for name in re.findall(r'(?<![\.\w$])([A-Za-z_$][\w$]*)\s*\(', body):
            if name not in BUILTINS:
                calls.append((name, body))
    return calls


class FrontendIntegrityTests(unittest.TestCase):
    def test_server_file_browser_uses_resolved_suite_path(self):
        navigation_text = read_text("web/static/js/navigation.js")

        self.assertIn("state.config?.effective_ubuntu_user", navigation_text)
        self.assertIn("state.config?.effective_suites_path", navigation_text)
        self.assertIn("await loadFileDirectory(getDefaultSuitesPath())", navigation_text)
        self.assertEqual(
            len(re.findall(
                r"/home/\$\{(?:defaultUser|getDefaultUbuntuUser\(\))\}/GMS-Suite",
                navigation_text,
            )),
            1,  # the fallback inside getDefaultSuitesPath itself
        )

    def test_main_app_inline_handlers_resolve_to_global_functions(self):
        main_text = read_text("web/shell/shell.html")
        script_text = "\n".join(path.read_text(encoding="utf-8", errors="ignore") for path in Path("web/static/js").glob("*.js"))
        combined = main_text + "\n" + script_text
        funcs = declared_functions(combined)

        missing = sorted({f"{name}: {body}" for name, body in inline_handler_calls(main_text) if name not in funcs})

        self.assertEqual(missing, [])

    def test_embedded_dashboard_inline_handlers_resolve_locally(self):
        for label, paths in [
            (
                "redmine",
                [
                    "features/redmine/ui/page.html",
                    "features/redmine/ui/page.js",
                ],
            ),
            ("gerrit", ["features/gerrit/ui/page.html"]),
            ("update-monitor", ["features/system/update_monitor/ui/page.html"]),
            ("mainline", ["features/system/mainline_issues/ui/page.html"]),
            (
                "automation",
                [
                    "features/automation/ui/page.html",
                    "features/automation/ui/page.js",
                ],
            ),
            (
                "cluster",
                [
                    "features/cluster/ui/page.html",
                    "features/cluster/ui/page.js",
                ],
            ),
        ]:
            with self.subTest(page=label):
                text = "\n".join(read_text(path) for path in paths)
                funcs = declared_functions(text)
                missing = sorted({f"{name}: {body}" for name, body in inline_handler_calls(text) if name not in funcs})

                self.assertEqual(missing, [])

    def test_test_host_uses_one_cluster_worker_selector(self):
        main_text = read_text("web/shell/shell.html")

        self.assertEqual(len(re.findall(r'id=["\']cluster-worker["\']', main_text)), 1)
        self.assertNotRegex(main_text, r'id=["\']ubuntu-host["\']')
        self.assertRegex(
            main_text,
            r'<label>测试主机:</label>\s*<select id="cluster-worker"',
        )

    def test_public_shell_never_embeds_configured_vnc_password(self):
        main_text = read_text("web/shell/shell.html")

        self.assertNotIn("config.vnc_password", main_text)
        self.assertNotIn("DEFAULT_VNC_PASSWORD", main_text)
        self.assertNotIn("new-host-vnc-password", main_text)
        self.assertIn("/api/desktop/novnc/access", main_text)

    def test_desktop_async_mount_cannot_replace_another_worker(self):
        main_text = read_text("web/shell/shell.html")

        self.assertIn(
            "generation !== hostWorkspace.renderGeneration",
            main_text,
        )
        self.assertIn(
            "currentPane?.hostId !== expectedHostId",
            main_text,
        )
        self.assertIn(
            "mountHostWorkspaceDesktop(index, host, body, generation, pane.hostId)",
            main_text,
        )

    def test_terminal_pane_refresh_does_not_render_all_panes(self):
        main_text = read_text("web/shell/shell.html")
        refresh = re.search(
            r"function refreshTerminalWorkspacePane\(i\)\{(?P<body>.*?)\n        \}",
            main_text,
            re.DOTALL,
        )

        self.assertIsNotNone(refresh)
        body = refresh.group("body")
        self.assertIn("disposeTerminalWorkspaceInstance(i)", body)
        self.assertIn("mountTerminalWorkspacePane(i,pane", body)
        self.assertNotIn("renderTerminalWorkspace()", body)

    def test_host_workspaces_wait_for_directory_and_recover_expired_elevation(self):
        main_text = read_text("web/shell/shell.html")

        self.assertIn("await mergeClusterDesktopHosts()", main_text)
        self.assertIn("await initDesktopHosts()", main_text)
        self.assertIn("message.elevation_required", main_text)
        self.assertIn("m.elevation_required", main_text)
        self.assertIn("message.credential_required", main_text)
        self.assertIn("m.credential_required", main_text)
        self.assertIn("showDevicePasswordModal(message.device_host, 'terminal'", main_text)
        self.assertIn("recoverTerminalElevation(instance", main_text)
        self.assertIn("refreshHostWorkspacePane(index)", main_text)
        self.assertIn("refreshTerminalWorkspacePane(index)", main_text)
        self.assertIn("ensureTerminalElevation(false, '打开主机桌面', '主机桌面')", main_text)
        self.assertIn("if (state.currentUser && state.currentUser.role !== 'admin')", main_text)
        self.assertNotIn("if (state.currentUser?.role !== 'admin')", main_text)
        self.assertIn("frame.allow = 'clipboard-read; clipboard-write'", main_text)

    def test_suite_host_selector_and_assistant_url_start_in_stable_layout(self):
        main_text = read_text("web/shell/shell.html")
        navigation_text = read_text("web/static/js/navigation.js")

        self.assertRegex(
            main_text,
            r'id="suite-worker-select"[^>]+disabled[^>]*>\s*<option value="">正在加载主机',
        )
        self.assertIn("_suiteWorkerSelectorPromise", navigation_text)
        self.assertIn('id="gms-assistant-url" style="width:100%;box-sizing:border-box;"', main_text)

    def test_single_mode_hides_multi_host_controls_and_sidebar_has_descriptions(self):
        main_text = read_text("web/shell/shell.html")
        navigation_text = read_text("web/static/js/navigation.js")

        self.assertIn("SIDEBAR_PAGE_DESCRIPTIONS", main_text)
        self.assertIn("sidebar-description", main_text)
        self.assertIn("white-space: nowrap", main_text)
        self.assertIn("选择 CTS/GTS/VTS/STS 等套件", main_text)
        self.assertIn('class="sidebar-text">${text}</span>', main_text)
        self.assertIn("data-multi-host-control", main_text)
        self.assertIn("workspace-scope-single", main_text)
        self.assertIn("applyHostWorkspaceScopeMode", main_text)
        self.assertIn("reportsHostFilter.style.visibility", navigation_text)
        self.assertIn("classList.toggle('workspace-scope-single', !enabled)", navigation_text)

    def test_terminal_page_switch_avoids_hidden_or_duplicate_resize(self):
        main_text = read_text("web/shell/shell.html")

        self.assertIn("!page.classList.contains('active')", main_text)
        self.assertIn("cols === instance.lastResizeCols", main_text)
        self.assertIn("rows === instance.lastResizeRows", main_text)
        self.assertIn("applyTerminalHost(select.value, false, false)", main_text)

    def test_device_management_inventory_is_scoped_by_single_cluster_mode(self):
        main_text = read_text("web/shell/shell.html")
        navigation_text = read_text("web/static/js/navigation.js")

        self.assertIn("requestedDevicesManagementScope", main_text)
        self.assertIn("const includeCluster = requestedScope === 'cluster'", main_text)
        self.assertIn("data-cluster-mode-only", main_text)
        self.assertIn("managementGroupsForView", main_text)
        self.assertIn("currentPage === 'devices'", navigation_text)
        self.assertIn("loadDevicesManagement().catch", navigation_text)

    def test_automation_api_handles_non_json_errors_without_unhandled_promises(self):
        automation_text = read_text("features/automation/ui/page.js")

        self.assertIn("const text = await resp.text()", automation_text)
        self.assertIn("data = text ? JSON.parse(text) : {}", automation_text)
        self.assertIn("loadRuns().catch(err => toast(err.message))", automation_text)
        self.assertIn("loadBuildJobs().catch(err => toast(err.message))", automation_text)

    def test_modal_pages_support_escape_close(self):
        for label, paths in [
            ("main", ["web/shell/shell.html"]),
            (
                "redmine",
                [
                    "features/redmine/ui/page.html",
                    "features/redmine/ui/page.js",
                ],
            ),
            ("gerrit", ["features/gerrit/ui/page.html"]),
        ]:
            with self.subTest(page=label):
                text = "\n".join(read_text(path) for path in paths)
                self.assertIn('class="modal', text)
                self.assertTrue("Escape" in text or "ModalManager" in text)

    def test_user_facing_result_prompts_avoid_blocking_alerts(self):
        main_text = read_text("web/shell/shell.html")
        self.assertNotIn("alert(", main_text)
        self.assertIn("gms-dashboard-notification", main_text)
        self.assertIn("redmine-agent-notification", main_text)
        self.assertIn("gms-update-monitor-notification", main_text)

        for path in ["features/redmine/ui/page.js", "features/gerrit/ui/page.html", "features/system/update_monitor/ui/page.html"]:
            with self.subTest(path=path):
                text = read_text(path)
                self.assertIn("function notifyUser", text)
                self.assertIn("postMessage", text)
                self.assertNotIn("alert(", text)

        self.assertNotIn("_sendParentNotification", read_text("features/redmine/ui/page.js"))

    def test_modal_ids_and_function_declarations_are_not_duplicated(self):
        checked_paths = [
            "web/shell/shell.html",
            "features/redmine/ui/page.html",
            "features/redmine/ui/page.js",
            "features/gerrit/ui/page.html",
            "features/system/update_monitor/ui/page.html",
            "features/system/mainline_issues/ui/page.html",
            "features/automation/ui/page.html",
            "features/automation/ui/page.js",
            *[str(path) for path in Path("web/static/js").glob("*.js")],
        ]
        for path in checked_paths:
            with self.subTest(path=path):
                text = read_text(path)
                modal_ids = re.findall(r'<[^>]+id=["\']([^"\']+)["\'][^>]+class=["\'][^"\']*\bmodal\b', text)
                duplicate_modal_ids = sorted({item for item in modal_ids if modal_ids.count(item) > 1})
                self.assertEqual(duplicate_modal_ids, [])

                function_names = FUNCTION_RE.findall(text)
                duplicate_functions = sorted({item for item in function_names if function_names.count(item) > 1})
                self.assertEqual(duplicate_functions, [])
