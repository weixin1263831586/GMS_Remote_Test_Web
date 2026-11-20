"""Host Terminal compact-pane regression using the real browser CSS.

Only renders DOM with stubbed terminal mounts: no SSH, ADB, WebSocket, or
production host session is opened by these geometry checks.
"""

from tests.runtime_ui.harness import RuntimeUiHarness


class TerminalWorkspaceVisualTests(RuntimeUiHarness):
    def test_terminal_panes_match_desktop_toolbar_and_frame_geometry(self):
        page = self.new_page()
        try:
            self.goto_shell(page)
            page.wait_for_function(
                "typeof renderTerminalWorkspace === 'function' "
                "&& typeof hostTerminalEmptyView === 'function'"
            )
            result = page.evaluate(
                """() => {
                    const previousPage = currentPage;
                    const previousMount = mountTerminalWorkspacePane;
                    const previousHosts = desktopHosts;
                    const previousCluster = hostWorkspaceClusterEnabled;
                    const grid = document.getElementById('terminal-workspace-grid');
                    // Do not create xterm, WebSockets, or any real terminal session.
                    mountTerminalWorkspacePane = () => {};
                    currentPage = 'reports';
                    desktopHosts = [
                        {id:'default', name:'ats-worker-controller', offline:false},
                        {id:'worker-a', name:'ats-worker-118', offline:false},
                        {id:'worker-b', name:'ats-worker-244', offline:false},
                    ];
                    hostWorkspaceClusterEnabled = true;
                    terminalWorkspace.instances.clear();
                    terminalWorkspace.mountingPanes.clear();
                    terminalWorkspace.maximized = null;
                    terminalWorkspace.layout = 'quad';
                    terminalWorkspace.panes = [
                        {hostId:'default'}, {hostId:'worker-a'},
                        {hostId:'worker-b'}, {hostId:''},
                    ];
                    terminalWorkspace.renderedSignature = null;
                    document.body.classList.remove('workspace-scope-single', 'workspace-scope-pending');
                    document.body.classList.add('workspace-scope-cluster');
                    try {
                        renderTerminalWorkspace();
                        const panes = [...grid.querySelectorAll('.host-workspace-pane')];
                        const header = panes[0].querySelector('.host-workspace-pane-header');
                        const quad = {
                            paneCount: panes.length,
                            labelCount: grid.querySelectorAll('[data-terminal-pane-mode-label]').length,
                            labelText: [...grid.querySelectorAll('[data-terminal-pane-mode-label]')]
                                .map(x => x.textContent.trim()),
                            selectors: grid.querySelectorAll('select[aria-label="主机"]').length,
                            refreshButtons: grid.querySelectorAll(
                                '.host-terminal-pane-actions [data-click="refreshTerminalWorkspacePane"]').length,
                            focusButtons: grid.querySelectorAll(
                                '.host-terminal-pane-actions [data-click="maximizeTerminalWorkspacePane"]').length,
                            topPadding: getComputedStyle(grid).paddingTop,
                            leftPadding: getComputedStyle(grid).paddingLeft,
                            rightPadding: getComputedStyle(grid).paddingRight,
                            headerHeight: getComputedStyle(header).height,
                            helpRows: grid.querySelectorAll('.host-desktop-meta').length,
                            lazyButtons: grid.querySelectorAll(
                                '.host-terminal-connect-button[data-click="refreshTerminalWorkspacePane"]').length,
                        };
                        terminalWorkspaceStatus(0, 'hcq@ats-041055-64g', true);
                        const stateNode = grid.querySelector('#terminal-workspace-status-0');
                        const status = {
                            label: stateNode.textContent, connected: stateNode.classList.contains('is-connected'),
                            tooltip: stateNode.title,
                        };
                        terminalWorkspace.layout = 'horizontal';
                        terminalWorkspace.renderedSignature = null;
                        renderTerminalWorkspace();
                        const horizontal = {
                            count: grid.querySelectorAll('.host-workspace-pane').length,
                            top: getComputedStyle(grid).paddingTop,
                            left: getComputedStyle(grid).paddingLeft,
                        };
                        terminalWorkspace.layout = 'vertical';
                        terminalWorkspace.renderedSignature = null;
                        renderTerminalWorkspace();
                        const vertical = grid.querySelectorAll('.host-workspace-pane').length;
                        document.body.classList.remove('workspace-scope-cluster');
                        document.body.classList.add('workspace-scope-single');
                        hostWorkspaceClusterEnabled = false;
                        terminalWorkspace.layout = 'single';
                        terminalWorkspace.panes = [{hostId:'default',mode:'adb',
                            serialNo:'FAKE-DEVICE',workerId:'worker-a'}];
                        terminalWorkspace.renderedSignature = null;
                        renderTerminalWorkspace();
                        const first = grid.querySelector('.host-workspace-pane');
                        const single = {
                            count: grid.querySelectorAll('.host-workspace-pane').length,
                            headerDisplay: getComputedStyle(first.querySelector(
                                '.host-workspace-pane-header')).display,
                            titleBarDisplay: getComputedStyle(document.querySelector(
                                '#page-terminal .host-workspace-single-bar')).display,
                            label: first.querySelector('[data-terminal-pane-mode-label]').textContent.trim(),
                            hostName: first.querySelector('.host-terminal-single-name').textContent.trim(),
                            hostNameDisplay: getComputedStyle(first.querySelector(
                                '.host-terminal-single-name')).display,
                            selectDisplay: getComputedStyle(first.querySelector(
                                '.host-terminal-select')).display,
                            returnVisible: getComputedStyle(first.querySelector(
                                '[data-terminal-pane-host-mode]')).display,
                            bodyHasTerminal: !!first.querySelector('.host-workspace-terminal'),
                        };
                        terminalWorkspaceStatus(0, '连接失败');
                        const error = first.querySelector('#terminal-workspace-status-0')
                            .classList.contains('is-error');
                        return {quad, horizontal, vertical, status, single, error};
                    } finally {
                        currentPage = previousPage;
                        mountTerminalWorkspacePane = previousMount;
                        desktopHosts = previousHosts;
                        hostWorkspaceClusterEnabled = previousCluster;
                    }
                }"""
            )
            self.assertEqual(result["quad"]["paneCount"], 4)
            self.assertEqual(result["quad"]["labelCount"], 4)
            self.assertEqual(result["quad"]["labelText"], ["🐧 终端"] * 4)
            self.assertEqual(result["quad"]["selectors"], 4)
            self.assertEqual(result["quad"]["refreshButtons"], 4)
            self.assertEqual(result["quad"]["focusButtons"], 4)
            self.assertEqual(result["quad"]["topPadding"], "0px")
            self.assertEqual(result["quad"]["leftPadding"], "0px")
            self.assertEqual(result["quad"]["rightPadding"], "0px")
            self.assertEqual(result["quad"]["headerHeight"], "34px")
            self.assertEqual(result["quad"]["helpRows"], 0)
            self.assertEqual(result["quad"]["lazyButtons"], 2)
            self.assertEqual(
                result["status"], {"label": "hcq@ats-041055-64g",
                                   "connected": True, "tooltip": "hcq@ats-041055-64g"}
            )
            self.assertEqual(result["horizontal"], {"count": 2, "top": "0px", "left": "0px"})
            self.assertEqual(result["vertical"], 2)
            self.assertEqual(result["single"]["count"], 1)
            self.assertEqual(result["single"]["headerDisplay"], "flex")
            self.assertEqual(result["single"]["titleBarDisplay"], "none")
            self.assertEqual(result["single"]["label"], "🐧 终端")
            self.assertEqual(result["single"]["hostName"], "ats-worker-controller")
            self.assertEqual(result["single"]["hostNameDisplay"], "block")
            self.assertEqual(result["single"]["selectDisplay"], "none")
            self.assertIn(result["single"]["returnVisible"], ("flex", "inline-flex"))
            self.assertFalse(result["single"]["bodyHasTerminal"])
            self.assertTrue(result["error"])
        finally:
            page.close()
