"""APK workbench presentation keeps existing upload, task and source handlers."""
from tests.runtime_ui.harness import RuntimeUiHarness, expect


class RuntimeApkWorkbenchTests(RuntimeUiHarness):
    def test_empty_and_active_source_workspaces_have_stable_geometry(self):
        page = self.new_page()
        try:
            self.goto_shell(page)
            page.evaluate("switchPage('apk-analysis', null)")
            page.wait_for_function("typeof switchApkTab === 'function'")
            expect(page.locator('#apk-upload-zone')).to_be_visible()
            expect(page.locator('#apk-task-history')).to_be_visible()
            self.assertEqual(page.locator('[data-apk-tab]').count(), 3)
            self.assertEqual(page.locator('#apk-file-input').get_attribute('accept'), '.apk,.jar')

            # Simulate a completed item through the same state/render methods:
            # never submit files, invoke jadx, or touch a real Controller task.
            page.evaluate("""() => {
                setApkUploadEmpty(false);
                document.querySelector('#apk-analysis-status').style.display = 'block';
                document.querySelector('#apk-analysis-result').style.display = 'block';
                document.querySelector('#apk-file-name').textContent = 'sample.apk';
                document.querySelector('#apk-analysis-state').textContent = '分析完成';
                document.querySelector('#apk-source-tree').dataset.loaded = 'true';
                switchApkTab('source');
                const item = document.createElement('div');
                item.className = 'apk-tree-item file';
                item.dataset.apkPath = 'java/com/example/Sample.java';
                item.textContent = 'Sample.java';
                document.querySelector('#apk-source-tree').appendChild(item);
                window.apkOpenFiles.set(item.dataset.apkPath, {
                    contentHtml: '<div class="apk-code-line"><span class="apk-code-line-no">1</span>'
                      + '<span class="apk-code-text">class Sample {}</span></div>'
                });
                activateApkFileTab(item.dataset.apkPath);
            }""")
            expect(page.locator('[data-apk-tab="source"]')).to_have_attribute('aria-selected', 'true')
            expect(page.locator('#apk-source-tree .apk-tree-item.selected')).to_have_count(1)
            expect(page.locator('#apk-source-placeholder')).to_be_hidden()
            expect(page.locator('#apk-file-viewer')).to_be_visible()
            expect(page.locator('#apk-file-path')).to_contain_text('Sample.java')

            # The status pane uses a 0.3s fadeIn/translate animation. Measure
            # its settled layout rather than the intermediate frame, while
            # preserving the original side-by-side geometry assertion.
            page.wait_for_timeout(380)

            for width, height in ((1792, 856), (1440, 900), (1280, 800)):
                with self.subTest(viewport=(width, height)):
                    page.set_viewport_size({'width': width, 'height': height})
                    metrics = page.evaluate("""() => {
                        // The shell also contains a hidden report-analysis upload
                        // frame. Never measure that first global match.
                        const scope = document.querySelector('#page-apk-analysis');
                        const box = name => (
                            name === '#page-apk-analysis' ? scope : scope.querySelector(name)
                        ).getBoundingClientRect();
                        const root = box('#page-apk-analysis');
                        const intake = box('.apk-intake-row');
                        const upload = box('.analysis-upload-frame');
                        const status = box('#apk-analysis-status');
                        const tabBar = box('.apk-tab-actions');
                        const workbench = box('#apk-tab-source');
                        const tree = box('.apk-source-tree-pane');
                        const viewer = box('#apk-file-viewer');
                        return {
                            outerOverflow: document.documentElement.scrollWidth - innerWidth,
                            sameTaskRow: Math.abs(upload.top - status.top),
                            uploadTop: upload.top,
                            statusTop: status.top,
                            uploadHeight: upload.height,
                            statusHeight: status.height,
                            uploadEmpty: document.getElementById('apk-upload-zone').classList.contains('upload-empty'),
                            intakeGridColumns: getComputedStyle(document.querySelector('.apk-intake-row')).gridTemplateColumns,
                            tabBelowIntake: tabBar.top >= intake.bottom - 1,
                            workbenchInside: workbench.bottom <= root.bottom + 2,
                            treeAndViewerSameTop: Math.abs(tree.top - viewer.top),
                            treeWidth: tree.width,
                            viewerWidth: viewer.width,
                            viewerHeight: viewer.height,
                        };
                    }""")
                    self.assertLessEqual(metrics['outerOverflow'], 2, metrics)
                    self.assertLessEqual(metrics['sameTaskRow'], 2, metrics)
                    self.assertTrue(metrics['tabBelowIntake'], metrics)
                    self.assertTrue(metrics['workbenchInside'], metrics)
                    self.assertLessEqual(metrics['treeAndViewerSameTop'], 2, metrics)
                    self.assertGreater(metrics['treeWidth'], 150, metrics)
                    self.assertGreater(metrics['viewerWidth'], 180, metrics)
                    self.assertGreater(metrics['viewerHeight'], 200, metrics)

            page.evaluate("closeApkFileViewer()")
            expect(page.locator('#apk-source-placeholder')).to_be_visible()
            expect(page.locator('#apk-file-viewer')).to_be_hidden()
            page.evaluate("switchApkTab('permissions')")
            expect(page.locator('[data-apk-tab="permissions"]')).to_have_attribute('aria-selected', 'true')
            expect(page.locator('[data-apk-tab="source"]')).to_have_attribute('aria-selected', 'false')
        finally:
            page.close()
