"""Rendered desktop-pane skin regression without contacting VNC or a remote host."""

from tests.runtime_ui.harness import RuntimeUiHarness


class DesktopWorkspaceVisualTests(RuntimeUiHarness):
    def test_internal_toolbar_and_lazy_panes_in_all_four_layouts(self):
        page = self.new_page()
        try:
            self.goto_shell(page)
            page.wait_for_function(
                "typeof renderHostWorkspace === 'function' "
                "&& typeof hostWorkspaceEmptyView === 'function'"
            )
            result = page.evaluate(
                """() => {
                    const previousPage = currentPage;
                    currentPage = 'reports'; // never mount noVNC, only inspect shell DOM
                    desktopHosts = [
                        {id:'default', worker_id:'ats-worker-controller', name:'ats-worker-controller',
                         connection:'hcq@127.0.0.1'},
                        {id:'cluster:a', worker_id:'a', name:'ats-worker-118',
                         connection:'hcq@192.0.2.10'},
                        {id:'cluster:b', worker_id:'b', name:'ats-worker-244',
                         connection:'hcq@192.0.2.11'},
                    ];
                    currentHost = desktopHosts[0];
                    hostWorkspaceClusterEnabled = true;
                    hostWorkspace.instances.clear();
                    hostWorkspace.layout = 'quad';
                    hostWorkspace.maximized = null;
                    hostWorkspace.panes = [
                        {type:'desktop', hostId:'default'},
                        {type:'desktop', hostId:'cluster:a'},
                        {type:'desktop', hostId:'cluster:b'},
                        {type:'empty', hostId:''},
                    ];
                    hostWorkspace.renderedSignature = null;
                    document.body.classList.remove('workspace-scope-single', 'workspace-scope-pending');
                    document.body.classList.add('workspace-scope-cluster');
                    renderHostWorkspace();
                    const grid = document.getElementById('host-workspace-grid');
                    const panelHeaders = [...grid.querySelectorAll('.host-workspace-pane-header')];
                    const quad = {
                        count: panelHeaders.length,
                        toolbarRows: panelHeaders.every(header => header.querySelector(
                            '.host-desktop-pane-actions .host-workspace-refresh-btn')),
                        selectCount: grid.querySelectorAll('select[aria-label="主机"]').length,
                        pendingButtons: grid.querySelectorAll(
                            '.host-desktop-placeholder-on_demand [data-click="refreshHostWorkspacePane"]'
                        ).length,
                        emptyButtons: grid.querySelectorAll(
                            '.host-desktop-placeholder-closed [data-click="reopenHostWorkspacePane"]'
                        ).length,
                        connectingPlaceholders: grid.querySelectorAll(
                            '.host-desktop-placeholder-connecting'
                        ).length,
                        metaCount: grid.querySelectorAll('.host-desktop-meta').length,
                        numberedLabels: grid.querySelectorAll('.host-desktop-pane-number').length,
                        emojiIcons: [...grid.querySelectorAll('.host-desktop-screen-label')]
                            .map(node => node.textContent.trim()),
                        typeLabels: [...grid.querySelectorAll('.host-desktop-type-label')]
                            .map(node => node.textContent.trim()),
                        headerHeight: getComputedStyle(panelHeaders[0]).height,
                            labelGeometry: (() => {
                                const label = grid.querySelector('.host-pane-label');
                                const icon = getComputedStyle(label.querySelector('.host-pane-label-icon'));
                                const css = getComputedStyle(label);
                                return {width: icon.width, height: icon.height, glyph: icon.fontSize,
                                    label: css.fontSize, weight: css.fontWeight, gap: css.columnGap};
                            })(),
                        outerInset: getComputedStyle(grid).padding,
                    };
                    setHostWorkspaceStatus(0, '桌面已连接', true);
                    const badge = grid.querySelector('#host-workspace-status-0');
                    const status = {text: badge.textContent, connected: badge.classList.contains('is-connected')};
                    hostWorkspace.layout = 'horizontal';
                    hostWorkspace.renderedSignature = null;
                    renderHostWorkspace();
                    const horizontal = {
                        count: grid.querySelectorAll('.host-workspace-pane').length,
                        metaCount: grid.querySelectorAll('.host-desktop-meta').length,
                        outerInset: getComputedStyle(grid).padding,
                    };
                    hostWorkspace.layout = 'vertical';
                    hostWorkspace.renderedSignature = null;
                    renderHostWorkspace();
                    const vertical = {
                        count: grid.querySelectorAll('.host-workspace-pane').length,
                        metaCount: grid.querySelectorAll('.host-desktop-meta').length,
                        outerInset: getComputedStyle(grid).padding,
                    };
                    document.body.classList.remove('workspace-scope-cluster');
                    document.body.classList.add('workspace-scope-single');
                    hostWorkspaceClusterEnabled = false;
                    hostWorkspace.layout = 'single';
                    hostWorkspace.panes = [{type: 'desktop', hostId: 'default'}];
                    hostWorkspace.renderedSignature = null;
                    renderHostWorkspace();
                    const single = {
                        count: grid.querySelectorAll('.host-workspace-pane').length,
                        header: getComputedStyle(grid.querySelector(
                            '.host-workspace-pane-header')).display,
                        hostName: grid.querySelector('.host-desktop-single-name').textContent,
                        hostNameDisplay: getComputedStyle(grid.querySelector(
                            '.host-desktop-single-name')).display,
                        selectDisplay: getComputedStyle(grid.querySelector(
                            'select[aria-label="主机"]')).display,
                        bodyHasFrame: !!grid.querySelector('iframe'),
                        metaCount: grid.querySelectorAll('.host-desktop-meta').length,
                        emojiIcon: grid.querySelector('.host-desktop-screen-label').textContent.trim(),
                        typeLabel: grid.querySelector('.host-desktop-type-label').textContent.trim(),
                    };
                    const html = hostWorkspaceEmptyView(0, 'error', '<unsafe>');
                    currentPage = previousPage;
                    return {quad, status, horizontal, vertical, single,
                        escapedError: html.includes('&lt;unsafe&gt;') &&
                            !html.includes('<unsafe>')};
                }"""
            )
            self.assertEqual(result["quad"]["count"], 4)
            self.assertTrue(result["quad"]["toolbarRows"])
            self.assertEqual(result["quad"]["selectCount"], 4)
            self.assertEqual(result["quad"]["pendingButtons"], 0)
            # Multiple distinct hosts connect automatically in the visible
            # desktop page rather than requiring extra per-pane clicks.
            self.assertEqual(result["quad"]["connectingPlaceholders"], 3)
            self.assertEqual(result["quad"]["emptyButtons"], 1)
            self.assertEqual(result["quad"]["metaCount"], 0)
            self.assertEqual(result["quad"]["numberedLabels"], 0)
            self.assertEqual(result["quad"]["emojiIcons"], ["🖥️"] * 4)
            self.assertEqual(result["quad"]["typeLabels"], ["桌面"] * 4)
            self.assertEqual(result["quad"]["headerHeight"], "34px")
            self.assertEqual(result["quad"]["labelGeometry"], {
                "width": "22px", "height": "22px", "glyph": "17px",
                "label": "12px", "weight": "500", "gap": "6px",
            })
            self.assertEqual(result["quad"]["outerInset"], "0px")
            self.assertEqual(result["status"], {"text": "桌面已连接", "connected": True})
            self.assertEqual(result["horizontal"]["count"], 2)
            self.assertEqual(result["horizontal"]["metaCount"], 0)
            self.assertEqual(result["horizontal"]["outerInset"], "0px")
            self.assertEqual(result["vertical"]["count"], 2)
            self.assertEqual(result["vertical"]["metaCount"], 0)
            self.assertEqual(result["vertical"]["outerInset"], "0px")
            self.assertEqual(result["single"]["count"], 1)
            self.assertEqual(result["single"]["header"], "flex")
            self.assertEqual(result["single"]["hostName"], "ats-worker-controller")
            self.assertEqual(result["single"]["hostNameDisplay"], "block")
            self.assertEqual(result["single"]["selectDisplay"], "none")
            self.assertFalse(result["single"]["bodyHasFrame"])
            self.assertEqual(result["single"]["metaCount"], 0)
            self.assertEqual(result["single"]["emojiIcon"], "🖥️")
            self.assertEqual(result["single"]["typeLabel"], "桌面")
            self.assertTrue(result["escapedError"])
        finally:
            page.close()
