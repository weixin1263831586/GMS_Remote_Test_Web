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


def read_all_frontend_js() -> str:
    """Read navigation.js + shell modules + all page modules as one string."""
    parts = [read_text("web/static/js/navigation.js")]
    shell_dir = Path("web/static/js/shell")
    if shell_dir.is_dir():
        for js_file in sorted(shell_dir.glob("*.js")):
            parts.append(read_text(str(js_file)))
    pages_dir = Path("web/static/js/pages")
    if pages_dir.is_dir():
        for js_file in sorted(pages_dir.glob("*.js")):
            parts.append(read_text(str(js_file)))
    return "\n".join(parts)


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
    def test_admin_elevation_survives_page_and_tab_initialization_until_ttl(self):
        api = read_text("web/static/js/api.js")

        auth_start = api.index("async function ensureAuthenticatedBeforeAppStart()")
        auth_end = api.index("function applyRoleBasedUiAccess()", auth_start)
        auth_source = api[auth_start:auth_end]
        self.assertIn("const status = await fetchAuthStatus();", auth_source)
        self.assertNotIn("/api/auth/elevation/reset", api)
        self.assertNotIn("resetElevationForNewBrowserTab", api)

    def test_cluster_dashboard_excludes_offline_devices_from_distribution(self):
        cluster_page = read_text("features/cluster/ui/page.js")
        names_start = cluster_page.index("const dashStateNames=")
        names_end = cluster_page.index(";", names_start)

        self.assertIn(
            "const dashboardDevices=state.devices.filter(d=>d.state!=='offline')",
            cluster_page,
        )
        self.assertNotIn("offline:'离线'", cluster_page[names_start:names_end])
        self.assertIn(
            "设备状态分布 ('+dashboardDevices.length+')",
            cluster_page,
        )
        self.assertIn(
            "String(device.state||'').toLowerCase()!=='offline'&&matches",
            cluster_page,
        )
        self.assertIn("暂无匹配的在线设备", cluster_page)

    def test_host_pages_share_short_lived_cluster_directory(self):
        shell = read_text("web/shell/shell.html")
        workspace_devices = read_text("web/static/js/shell/workspace-devices.js")
        terminal_start = shell.index("async function loadTerminalClusterHosts()")
        terminal_end = shell.index("function applyTerminalHost", terminal_start)

        self.assertIn("async function loadClusterHostDirectory(force = false)", shell)
        self.assertIn("const directoryHosts = await loadClusterHostDirectory()", shell)
        self.assertNotIn("fetch('/api/cluster/hosts'", shell[terminal_start:terminal_end])
        self.assertIn("hosts = await window.loadClusterHostDirectory()", workspace_devices)

    def test_page_initialization_is_deduplicated_and_rejections_are_handled(self):
        shell = read_text("web/shell/shell.html")

        self.assertIn("const pendingAuthPageInitializers = new Set()", shell)
        self.assertIn("if (pendingAuthPageInitializers.has(pageName)) return", shell)
        self.assertIn("runPageInitializers(pageName).catch(error =>", shell)
        self.assertIn("initializePageSafely(pageName)", shell)

    def test_shell_stops_boot_when_navigation_bundle_is_unavailable(self):
        shell = read_text("web/shell/shell.html")
        navigation = read_text("web/static/js/navigation.js")

        self.assertIn("window.GmsNavigationReady !== true", shell)
        self.assertTrue(
            navigation.rstrip().endswith("window.GmsNavigationReady = true;")
        )

    def test_system_status_pages_use_consistent_chinese_loading_copy(self):
        for path in (
            "features/system/mainline_issues/ui/page.html",
            "features/system/update_monitor/ui/page.html",
        ):
            with self.subTest(path=path):
                text = read_text(path)
                self.assertNotIn("Loading...", text)
                self.assertIn("加载中…", text)

    def test_report_page_reuses_recent_data_and_parallelizes_initial_requests(self):
        reports = read_text("web/static/js/pages/test-reports.js")
        navigation = read_text("web/static/js/navigation.js")
        shell = read_text("web/shell/shell.html")

        self.assertIn("const REPORTS_REENTRY_CACHE_MS = 10000", reports)
        self.assertIn("const [data] = await Promise.all([", reports)
        self.assertIn("reportsLastQueryKey === queryKey", reports)
        self.assertIn("window.clusterWorkersSnapshot", reports)
        self.assertIn("if (reportsRequests.has(url))", reports)
        self.assertIn("window.preloadTestReports = preloadTestReports", reports)
        self.assertIn("scheduleDeferredPagePreload(initialPage)", navigation)
        self.assertIn("const clusterModeReady = initializeClusterMode()", navigation)
        self.assertIn("await Promise.all([clusterModeReady, configReady])", navigation)
        self.assertIn("test-reports.js?v=20260812-stable-surfaces", shell)
        self.assertIn("navigation.js?v=20260827-apts-gts-suite", shell)
        self.assertIn("testTypeLower === 'apts'", navigation)
        self.assertIn("APTS使用GTS测试套件", navigation)

    def test_server_file_browser_uses_resolved_suite_path(self):
        navigation_text = read_all_frontend_js()

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
        script_paths = list(Path("web/static/js").glob("*.js"))
        script_paths.extend(Path("web/static/js/shell").glob("*.js"))
        script_paths.extend(Path("web/static/js/pages").glob("*.js"))
        script_text = "\n".join(path.read_text(encoding="utf-8", errors="ignore") for path in script_paths)
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

    def test_cluster_dashboard_has_stable_refresh_and_safe_dynamic_actions(self):
        html = read_text("features/cluster/ui/page.html")
        script = read_text("features/cluster/ui/page.js")

        for control_id in ("dash-refresh-charts", "refresh", "reload-library"):
            self.assertRegex(
                html,
                rf'id="{control_id}" class="[^"]*refresh-action[^"]*"',
            )
        self.assertIn('class="section-head-copy"', html)
        self.assertIn('id="cluster-search"', html)
        self.assertIn('id="job-status-filter"', html)
        self.assertIn('<th>任务</th><th>用户</th><th>Worker</th>', html)
        self.assertIn("if(refreshPromise)return await refreshPromise", script)
        self.assertIn("Promise.allSettled", script)
        self.assertIn("'/api/cluster/jobs?include_active=true'", script)
        self.assertIn("Boolean(job.monitor_only)", script)
        self.assertIn('data-action="redeploy-worker"', script)
        self.assertNotIn('onclick="redeployWorker(', script)
        self.assertIn("function setClusterRefreshBusy(busy)", script)
        self.assertIn("button.textContent=busy?'刷新中…':button.dataset.idleLabel", script)
        self.assertIn("document.querySelector('#refresh').onclick=()=>refresh(true)", script)
        self.assertIn("scheduleDashboardChartsResize", script)
        self.assertIn("state.status.enabled&&clusterWorkspace.scope_mode==='cluster'", script)
        self.assertIn("renderModeStatus();renderJobForm()", script)
        self.assertNotIn('集群模式已启用 · 本机', script)
        self.assertNotIn('集群能力已启用', script)
        self.assertIn('>测试终端日志 (${Math.max(0,Number(stdout.size_bytes)||0)} bytes)</a>', script)
        self.assertIn('data-action="open-job-report-file"', script)
        self.assertIn('data-action="download-job-report"', script)
        self.assertIn("window.parent.downloadReport", script)
        self.assertIn("window.parent.openReportSuiteDirectory", script)
        self.assertIn("test_result_failures_suite.html", script)
        self.assertNotIn("job-artifact-card", script)
        self.assertIn("origin_page:terminalJob(job.status)?'reports':'cluster'", script)

        navigation = read_all_frontend_js()
        self.assertIn("const filePath = targetFile ? `${targetPath}/${targetFile}` : '';", navigation)
        self.assertIn("setSuiteBrowserHighlightedPath(filePath)", navigation)
        self.assertIn("const reportProvenanceOnly = ['reports', 'report-analysis', 'report-download', 'test-suites', 'automation']", navigation)
        self.assertIn("contextJobId !== state.clusterJobId && !reportProvenanceOnly", navigation)

    def test_opengrok_tool_icon_does_not_probe_the_external_service(self):
        shell = read_text("web/shell/shell.html")

        self.assertIn(
            "'OpenGrok': '/static/icons/favicons/rockchip-opengrok.svg'",
            shell,
        )
        self.assertNotIn("/default/img/apple-touch-icon.png", shell)

    def test_login_explains_client_ssh_account_and_finishes_identity_prefill(self):
        shell = read_text("web/shell/shell.html")
        api_script = read_text("web/static/js/api.js")

        self.assertIn("SSH用户名@客户端IP，例如 ", shell)
        self.assertIn("可使用平台管理员账号", shell)
        self.assertIn("通常与系统登录/锁屏密码相同", shell)
        self.assertIn("identity.includes('@')", api_script)
        self.assertIn("但尚不知道 SSH 用户名", api_script)
        self.assertIn("usernameInput.placeholder.includes('正在读取客户端身份')", api_script)
        self.assertIn("此处不使用客户端 SSH 账号", api_script)
        self.assertIn("loginHost !== String(result.client_ip || '')", api_script)

    def test_firmware_share_copy_explains_public_download(self):
        navigation = read_all_frontend_js()

        self.assertIn("分享链接已复制，无需登录即可打开下载", navigation)
        self.assertNotIn("分享链接已复制，已登录客户端可直接打开下载", navigation)

    def test_firmware_upload_stages_before_burn_and_keeps_errors_retryable(self):
        firmware = read_text("web/static/js/pages/firmware-burn.js")
        chunks = read_text("web/static/js/chunk-upload.js")
        shell = read_text("web/shell/shell.html")

        self.assertIn("stage_only: '1'", firmware)
        self.assertIn("finalizeForm.append('finalize_upload', '1')", firmware)
        self.assertIn("if (uploadResult.staged)", firmware)
        self.assertIn("return await new Promise((resolve, reject)", chunks)
        self.assertIn("const uploadError = error instanceof Error", chunks)
        self.assertIn("formData.append('chunk_size', chunkSize)", chunks)
        self.assertIn("chunk-upload.js?v=20260813-firmware-content-id", shell)
        self.assertNotIn("普通固件烧写需要 ADB 设备", firmware)
        self.assertIn("firmware-burn.js?v=20260902-notify-dedup", shell)

    def test_gsi_burn_starts_and_stops_fastboot_transition_refresh(self):
        navigation = read_all_frontend_js()

        self.assertIn(
            "stopDeviceProtocolRefresh = startBurnDeviceProtocolRefresh(devices)",
            navigation,
        )
        self.assertIn("stopDeviceProtocolRefresh();", navigation)
        self.assertIn("loadDevices(true, {silent: true})", navigation)

    def test_cluster_mode_switch_stays_on_page_and_desktop_elevates_first(self):
        navigation = read_all_frontend_js()
        shell = read_text("web/shell/shell.html")

        toggle_start = navigation.index("async function toggleClusterMode()")
        toggle_end = navigation.index("window.toggleClusterMode", toggle_start)
        self.assertNotIn("switchPage('test')", navigation[toggle_start:toggle_end])

        desktop_initializer = """if (pageName === 'desktop') {
                if (!await ensureTerminalElevation(false, '打开主机桌面', '主机桌面')) return;
                await ensureDesktopInitialized();"""
        self.assertIn(desktop_initializer, shell)

    def test_test_host_uses_one_cluster_worker_selector(self):
        main_text = read_text("web/shell/shell.html")
        navigation = read_all_frontend_js()

        self.assertEqual(len(re.findall(r'id=["\']cluster-worker["\']', main_text)), 1)
        self.assertNotRegex(main_text, r'id=["\']ubuntu-host["\']')
        self.assertRegex(
            main_text,
            r'<label>测试主机:</label>\s*<select id="cluster-worker"',
        )
        load_start = navigation.index("async function loadClusterWorkers(forceRefresh = false)")
        load_end = navigation.index("async function resolveClusterHost", load_start)
        load_source = navigation[load_start:load_end]
        self.assertNotIn("select.style.visibility", load_source)
        self.assertIn("await initializeClusterMode()", load_source)
        self.assertIn("if (!optionsUnchanged)", load_source)
        self.assertIn("select.value !== selectedWorkerId", load_source)
        self.assertIn("select.dataset.workersLoaded = 'true'", load_source)
        self.assertIn("testHostSelect.disabled = !enabled || !workersLoaded", navigation)
        self.assertIn(
            "currentPage === 'test' && typeof loadClusterWorkers === 'function'",
            navigation,
        )

    def test_skill_toolbar_prefers_installer_and_labels_zip_as_offline_only(self):
        shell = read_text("web/shell/shell.html")
        navigation = read_all_frontend_js()
        api_constants = read_text("web/static/js/api-constants.js")

        self.assertIn('onclick="copySkillInstallCommand()"', shell)
        self.assertIn("📋 安装/更新命令", shell)
        self.assertIn('onclick="downloadSkillsZip()"', shell)
        self.assertIn("📦 离线包", shell)
        self.assertIn("function buildSkillInstallCommand()", navigation)
        self.assertIn("function copySkillInstallCommand()", navigation)
        self.assertIn(
            "window.location.protocol === 'https:' ? '-kfsSL' : '-fsSL'",
            navigation,
        )
        self.assertIn(
            'curl -kfsSL "https://server:5001/api/system/skills/install.sh" | bash',
            api_constants,
        )
        self.assertIn(
            "{% if request.url.scheme == 'https' %}-kfsSL{% else %}-fsSL{% endif %}",
            shell,
        )
        self.assertIn("apiPath === '/api/system/skills/install.sh'", navigation)
        self.assertIn("apiPath === '/api/system/skills'", navigation)
        self.assertIn("全部独立gms-rt-*命令", api_constants)

    def test_suite_share_links_keep_slashes_readable(self):
        navigation = read_all_frontend_js()
        share_start = navigation.index("function buildSuiteBrowserLink(")
        share_end = navigation.index("async function initTestSuiteBrowserPage", share_start)
        share_source = navigation[share_start:share_end]

        self.assertIn("buildReadablePathQuery(params)", share_source)
        self.assertIn("#test-suites?${readableQuery}", share_source)
        self.assertIn("params.set('worker_id', workerId)", share_source)
        self.assertNotIn("workerId && !isLocalWorkspaceWorker(workerId)", share_source)
        self.assertIn("params.get('host') || workspaceLocalWorkerId()", navigation)

    def test_suite_download_and_inline_urls_keep_path_slashes_readable(self):
        navigation = read_all_frontend_js()

        self.assertIn(
            "return params.toString().replace(/%2F/gi, '/')",
            navigation,
        )
        self.assertIn(
            "window.open(`${endpoint}?${buildReadablePathQuery(params)}`, '_blank')",
            navigation,
        )
        self.assertIn(
            "link.href = `${endpoint}?${buildReadablePathQuery(params)}`",
            navigation,
        )
        self.assertIn(
            "/api/test/suites/download-dir?${buildReadablePathQuery(params)}",
            navigation,
        )

    def test_public_shell_never_embeds_configured_vnc_password(self):
        main_text = read_text("web/shell/shell.html")

        self.assertNotIn("config.vnc_password", main_text)
        self.assertNotIn("DEFAULT_VNC_PASSWORD", main_text)
        self.assertNotIn("new-host-vnc-password", main_text)
        self.assertIn("/api/desktop/novnc/access", main_text)

    def test_desktop_async_mount_cannot_replace_another_worker(self):
        main_text = read_text("web/shell/shell.html")

        self.assertIn("function hostWorkspaceMountIsCurrent", main_text)
        self.assertIn("generation === hostWorkspace.renderGeneration", main_text)
        self.assertIn(
            "hostWorkspace.panes[index]?.hostId === expectedHostId",
            main_text,
        )
        self.assertIn(
            "mountHostWorkspaceDesktop(index, host, body, generation, pane.hostId, paneGeneration)",
            main_text,
        )
        self.assertIn(
            "paneGeneration !== (hostWorkspace.paneGenerations.get(index) || 0)",
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

    def test_device_shell_uses_visible_adb_workspace_without_timer_injection(self):
        main_text = read_text("web/shell/shell.html")

        self.assertIn("mode: 'adb'", main_text)
        self.assertIn("serialNo: rawSerial", main_text)
        self.assertIn("workerId", main_text)
        self.assertIn("mode:terminalMode", main_text)
        self.assertIn("serial_no:serialNo", main_text)
        self.assertIn("shellReady:false", main_text)
        self.assertIn("if(instance.mode==='adb')", main_text)
        self.assertIn("const adbCommand=", main_text)
        self.assertNotIn("setTimeout(sendShell", main_text)
        self.assertNotIn("input: `adb -s ${rawSerial} shell", main_text)

    def test_host_workspaces_wait_for_directory_and_recover_expired_elevation(self):
        main_text = read_text("web/shell/shell.html")

        self.assertIn("await mergeClusterDesktopHosts()", main_text)
        self.assertIn("const hostsReady = initDesktopHosts()", main_text)
        self.assertIn("canUseLocalBootstrap", main_text)
        self.assertIn("message.elevation_required", main_text)
        self.assertIn("m.elevation_required", main_text)
        self.assertIn("message.credential_required", main_text)
        self.assertIn("m.credential_required", main_text)
        self.assertIn("showDevicePasswordModal(message.device_host, 'terminal'", main_text)
        self.assertIn("recoverTerminalElevation(instance", main_text)
        self.assertIn("refreshHostWorkspacePane(index)", main_text)
        self.assertIn("refreshTerminalWorkspacePane(index)", main_text)
        self.assertIn("ensureTerminalElevation(false, '打开主机桌面', '主机桌面')", main_text)
        self.assertIn("frame.allow = 'clipboard-read; clipboard-write'", main_text)

    def test_suite_host_selector_and_assistant_url_start_in_stable_layout(self):
        main_text = read_text("web/shell/shell.html")
        navigation_text = read_all_frontend_js()

        self.assertRegex(
            main_text,
            r'<label data-multi-host-control[^>]*>主机\s*<select id="suite-worker-select"[^>]+disabled[^>]*>\s*<option value="">正在加载主机',
        )
        self.assertIn("_suiteWorkerSelectorPromise", navigation_text)
        self.assertIn('id="gms-assistant-url" style="width:100%;box-sizing:border-box;"', main_text)

    def test_suite_report_copy_modal_uses_worker_and_suite_choices(self):
        main_text = read_text("web/shell/shell.html")
        common_text = read_text("web/static/css/common.css")
        navigation_text = read_all_frontend_js()

        self.assertIn('id="btn-copy-test-report"', main_text)
        self.assertIn('id="report-copy-modal"', main_text)
        self.assertIn('class="modal-content report-copy-modal-content"', main_text)
        self.assertIn('id="report-copy-source-worker"', main_text)
        self.assertIn('id="report-copy-source-report"', main_text)
        self.assertIn('id="report-copy-target-worker"', main_text)
        self.assertIn('id="report-copy-target-suite"', main_text)
        self.assertIn("#report-copy-modal .report-copy-modal-content", common_text)
        self.assertIn("height: 500px", common_text)
        self.assertIn("#report-copy-modal select.combo-box", common_text)
        self.assertIn("function reportCopySuiteLabel(suite)", navigation_text)
        self.assertIn(
            ".sort((left, right) => left.name.localeCompare(right.name))",
            navigation_text,
        )
        self.assertIn("/api/cluster/suites/report-copies", navigation_text)
        self.assertIn("source_worker_id: sourceWorkerId", navigation_text)
        self.assertIn("target_worker_id: targetWorkerId", navigation_text)

    def test_manual_suite_refresh_forces_reload_and_has_busy_feedback(self):
        main_text = read_text("web/shell/shell.html")
        navigation_text = read_all_frontend_js()

        self.assertIn('id="refresh-suites-btn" class="btn-xxs ui-refresh-action"', main_text)
        self.assertIn('onclick="refreshTestSuites()"', main_text)
        self.assertIn("await loadTestSuites(true)", navigation_text)
        self.assertIn("button.textContent = '刷新中…'", navigation_text)
        self.assertIn("if (forceRefresh) return loadTestSuites(true)", navigation_text)

    def test_single_mode_hides_multi_host_controls_and_sidebar_has_descriptions(self):
        main_text = read_text("web/shell/shell.html")
        navigation_text = read_all_frontend_js()

        self.assertIn("SIDEBAR_PAGE_DESCRIPTIONS", main_text)
        self.assertIn("sidebar-description", main_text)
        self.assertIn("white-space: nowrap", main_text)
        self.assertIn("选择 CTS/GTS/VTS/STS 等套件", main_text)
        self.assertIn('class="sidebar-text">${text}</span>', main_text)
        self.assertIn("data-multi-host-control", main_text)
        self.assertIn("workspace-scope-single", main_text)
        self.assertIn("body.workspace-scope-pending [data-multi-host-control]", main_text)
        self.assertIn(
            "includeCluster\n                        ? fetch('/api/cluster/status'",
            main_text,
        )
        self.assertIn(
            "await mgmtLoadGroups();\n                // Group loading is another asynchronous boundary.",
            main_text,
        )
        self.assertIn("workspace-scope-pending", main_text)
        self.assertIn("host-workspace-ready", main_text)
        self.assertIn("terminal-workspace-status-single", main_text)
        self.assertIn("applyHostWorkspaceScopeMode", main_text)
        self.assertIn("reportsHostFilter.style.visibility", navigation_text)
        self.assertIn("body.classList.add(enabled ? 'workspace-scope-cluster' : 'workspace-scope-single')", navigation_text)

    def test_terminal_page_switch_avoids_hidden_or_duplicate_resize(self):
        main_text = read_text("web/shell/shell.html")

        self.assertIn("!page.classList.contains('active')", main_text)
        self.assertIn("cols === instance.lastResizeCols", main_text)
        self.assertIn("rows === instance.lastResizeRows", main_text)
        self.assertIn("applyTerminalHost(select.value, false, false)", main_text)

    def test_device_management_inventory_is_scoped_by_single_cluster_mode(self):
        main_text = read_text("web/shell/shell.html")
        navigation_text = read_all_frontend_js()

        self.assertIn("requestedDevicesManagementScope", main_text)
        self.assertIn("const includeCluster = requestedScope === 'cluster'", main_text)
        self.assertIn("data-cluster-mode-only", main_text)
        self.assertIn("managementGroupsForView", main_text)
        self.assertIn("currentPage === 'devices'", navigation_text)
        self.assertIn("loadDevicesManagement().catch", navigation_text)

    def test_device_management_renders_fastboot_and_plural_usbip_ids(self):
        main_text = read_text("web/shell/shell.html")
        controls = read_text("web/static/js/pages/test-control.js")

        self.assertIn("device.protocol === 'fastboot'", main_text)
        self.assertIn("● Fastboot", main_text)
        self.assertIn("config.usbip_vid_pids", controls)
        self.assertIn("usbip_vid_pids: [...new Set(usbipVidPids)]", controls)
        self.assertIn("test-control.js?v=20260827-usbip-vid-pids", main_text)

    def test_unselectable_devices_stay_selected_but_render_unchecked(self):
        navigation = read_text("web/static/js/navigation.js")
        workspace_devices = read_text("web/static/js/shell/workspace-devices.js")
        browser = read_text("web/static/js/pages/test-suite-browser.js")

        # loadDevices：只从选中集合删除已消失的设备，回填仅按存在性判断；
        # 不可选（占用/状态异常）设备保留勾选状态，恢复可选后自动回选。
        self.assertIn("if (!currentIds.has(id)) {", workspace_devices)
        self.assertIn("if (currentIds.has(id)) state.selectedDevices.add(id);", workspace_devices)
        # device_lock_update：被占用不取消勾选，仅由渲染层隐藏勾选。
        self.assertNotIn("state.selectedDevices.delete(deviceId);", navigation)
        self.assertNotIn("syncWorkspaceDeviceSelection", navigation)
        # 渲染兜底：不可选设备的复选框不显示勾选。
        self.assertIn(
            "const isSelected = selectable && state.selectedDevices.has(deviceId);",
            browser,
        )

    def test_automation_api_handles_non_json_errors_without_unhandled_promises(self):
        automation_text = read_text("features/automation/ui/page.js")

        self.assertIn("const text = await resp.text()", automation_text)
        self.assertIn("data = text ? JSON.parse(text) : {}", automation_text)
        self.assertIn("loadRuns().catch(err => toast(err.message))", automation_text)
        self.assertIn("loadBuildJobs().catch(err => toast(err.message))", automation_text)

    def test_local_software_reconfigure_elevates_before_posting(self):
        cluster_text = read_text("features/cluster/ui/page.js")
        function_text = cluster_text.split(
            "async function reconfigureLocalSoftware(button)",
            1,
        )[1].split("async function redeployWorker", 1)[0]
        elevation = function_text.index("requestElevatedAccess")
        submit = function_text.index(
            "api('/api/cluster/workers/local/software/reconfigure'"
        )

        self.assertLess(elevation, submit)
        self.assertIn("if (!state.authRequired)", read_all_frontend_js())

    def test_user_actions_use_stable_grid_and_safe_event_binding(self):
        main_text = read_text("web/shell/shell.html")
        navigation_text = read_all_frontend_js()
        css_text = read_text("web/static/css/common.css")

        self.assertIn('class="user-actions-grid"', main_text)
        self.assertIn('data-remove-user=', navigation_text)
        self.assertIn('renderUserRemoveCell(user, normalizedStatus)', main_text)
        self.assertIn('disabled aria-disabled="true"', navigation_text)
        self.assertNotIn('onclick="removeUser(', main_text)
        self.assertIn("grid-template-columns: 44px 44px", css_text)

    def test_config_override_device_mutations_prompt_before_request(self):
        main_text = read_text("web/shell/shell.html")

        self.assertIn("dcfgRequireElevation('应用设备 RRO 配置覆盖')", main_text)
        self.assertIn("dcfgRequireElevation('撤销设备 RRO 配置覆盖')", main_text)
        self.assertIn("requestElevatedAccess(actionLabel, {allowAnonymousDev: true})", main_text)
        self.assertIn("return apiCall(path + sep + qs", main_text)
        self.assertIn("`已配置 ${entryCount} 项；Overlay ${", main_text)
        self.assertNotIn("`已应用 ${entryCount", main_text)

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
            *[str(path) for path in Path("web/static/js/pages").glob("*.js")],
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
