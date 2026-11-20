"""Disk audit refresh must visibly rescan and never queue duplicate reads."""

from tests.runtime_ui.harness import RuntimeUiHarness


class DiskAuditRefreshTests(RuntimeUiHarness):
    def test_disk_sidebar_cards_scroll_instead_of_clipping_content(self):
        """Disk details and scan scope retain complete text at constrained heights."""
        page = self.new_page()
        try:
            self.goto_shell(page)
            page.evaluate("() => switchPage('security-audit', null)")
            page.wait_for_function(
                "() => document.getElementById('page-security-audit').classList.contains('active')"
            )
            page.evaluate(
                """() => {
                    document.getElementById('audit-main-view').style.display = 'none';
                    document.getElementById('audit-outbox-panel').style.display = 'none';
                    document.getElementById('audit-disk-panel').style.display = 'flex';
                    const detail = document.getElementById('audit-disk-detail');
                    // Populate long details without making a network or device request.
                    for (let i = 0; i < 12; i++) {
                        const row = document.createElement('div');
                        const label = document.createElement('span');
                        const value = document.createElement('strong');
                        label.textContent = '追加分类属性 ' + i;
                        value.textContent = '较长的只读分类审计说明与完整路径 ' + i;
                        row.append(label, value);
                        detail.appendChild(row);
                    }
                }"""
            )
            for width, height in ((1912, 914), (1440, 720)):
                with self.subTest(viewport=(width, height)):
                    page.set_viewport_size({"width": width, "height": height})
                    geometry = page.evaluate(
                        """() => {
                            const root = document.getElementById('audit-disk-panel');
                            const side = root.querySelector('.audit-side-panels');
                            const cards = Array.from(side.querySelectorAll(':scope > .audit-side-card'));
                            const table = root.querySelector('.audit-disk-table-panel .audit-table-wrap');
                            const scrolled = side.scrollHeight > side.clientHeight + 1;
                            const styles = cards.map(card => ({
                                shrink: getComputedStyle(card).flexShrink,
                                client: card.clientHeight,
                                scroll: card.scrollHeight,
                            }));
                            side.scrollTop = side.scrollHeight;
                            const sideRect = side.getBoundingClientRect();
                            const notesRect = cards[cards.length - 1].getBoundingClientRect();
                            return {
                                sideOverflow: getComputedStyle(side).overflowY,
                                tableOverflow: getComputedStyle(table).overflowY,
                                scrollable: scrolled,
                                scrollTop: side.scrollTop,
                                notesBottom: notesRect.bottom,
                                sideBottom: sideRect.bottom,
                                cardSizes: styles,
                                cardCount: cards.length,
                            };
                        }"""
                    )
                    self.assertEqual(geometry["sideOverflow"], "auto", geometry)
                    self.assertEqual(geometry["tableOverflow"], "auto", geometry)
                    self.assertEqual(geometry["cardCount"], 3, geometry)
                    self.assertTrue(geometry["scrollable"], geometry)
                    self.assertGreater(geometry["scrollTop"], 0, geometry)
                    self.assertLessEqual(
                        geometry["notesBottom"], geometry["sideBottom"] + 2, geometry
                    )
                    for card in geometry["cardSizes"]:
                        self.assertEqual(card["shrink"], "0", geometry)
                        self.assertLessEqual(card["scroll"] - card["client"], 1, geometry)
        finally:
            page.close()

    def test_refresh_bypasses_cache_and_has_visible_loading_feedback(self):
        page = self.new_page()
        try:
            self.goto_shell(page)
            page.wait_for_function("typeof loadDiskArtifactAudit === 'function'")
            result = page.evaluate(
                """async () => {
                    const previous = window.apiCall;
                    const previousAuth = state.authRequired;
                    const calls = [];
                    state.authRequired = false;
                    window.apiCall = (url, ...rest) =>
                        url.startsWith('/api/security-audit/disk-artifacts?')
                            ? new Promise(resolve => calls.push({url, resolve}))
                            : previous(url, ...rest);
                    const payload = bytes => ({
                        data: {
                            generated_at: new Date().toISOString(),
                            readonly: true, cache_hit: false,
                            summary: {
                                measured_bytes: bytes, controller_data_bytes: bytes,
                                local_worker_bytes: 0, aged_bytes: 0,
                                attention_categories: 1, scanned_categories: 1,
                                incomplete_categories: 0, scan_seconds: 4.3,
                                visited_entries: 8,
                                disk: {free_bytes: 1024, used_bytes: 2048}
                            },
                            items: [{
                                id: 'apk', name: 'APK 原包与反编译',
                                location: 'data/apk_uploads', group: 'APK',
                                scope: 'Controller', cleanup: '任务配额',
                                priority: '高', age_days: 30, description: '测试样本',
                                bytes, files: 2, directories: 1,
                                aged_bytes: 0, aged_files: 0,
                                older_7d_bytes: 0, older_30d_bytes: 0,
                                older_90d_bytes: 0, present: true,
                                status: 'scanned', complete: true,
                                breakdown: [{type: '.java', bytes, files: 2}],
                            }]
                        }
                    });
                    try {
                        const button = document.getElementById('audit-disk-refresh-btn');
                        const label = document.getElementById('audit-disk-refresh-label');
                        const first = loadDiskArtifactAudit(true);
                        const firstBusy = button.disabled
                            && label.textContent.includes('扫描中')
                            && document.getElementById('audit-disk-scanned-at')
                                .textContent.includes('正在');
                        const rejected = await loadDiskArtifactAudit(true);
                        const noDuplicate = rejected === false && calls.length === 1;
                        calls[0].resolve(payload(2048));
                        await first;
                        const firstValue = document.getElementById('audit-disk-bytes')
                            .textContent;
                        const firstDone = !button.disabled
                            && label.textContent.includes('刷新');
                        const second = loadDiskArtifactAudit(true);
                        const secondBusy = button.disabled;
                        calls[1].resolve(payload(4096));
                        await second;
                        const secondValue = document.getElementById('audit-disk-bytes')
                            .textContent;
                        const rowExists = document.querySelectorAll(
                            '#audit-disk-table-body tr[data-artifact-id=apk]').length === 1;
                        const details = document.getElementById('audit-disk-detail')
                            .textContent;
                        return {
                            firstBusy, noDuplicate, firstDone, secondBusy,
                            firstValue, secondValue,
                            requests: calls.map(item => item.url),
                            rowExists, typeBreakdown: details.includes('.java'),
                            footer: document.getElementById('audit-disk-scan-cost')
                                .textContent,
                            complete: document.getElementById('audit-disk-covered')
                                .textContent,
                            refreshed: document.getElementById('audit-disk-scanned-at')
                                .textContent.includes('实盘扫描'),
                        };
                    } finally {
                        window.apiCall = previous;
                        state.authRequired = previousAuth;
                    }
                }"""
            )
            self.assertTrue(result["firstBusy"], result)
            self.assertTrue(result["noDuplicate"], result)
            self.assertTrue(result["firstDone"], result)
            self.assertTrue(result["secondBusy"], result)
            self.assertEqual(result["firstValue"], "2.0 KiB")
            self.assertEqual(result["secondValue"], "4.0 KiB")
            self.assertEqual(len(result["requests"]), 2)
            self.assertTrue(all("refresh=true" in url for url in result["requests"]))
            self.assertTrue(result["rowExists"], result)
            self.assertTrue(result["typeBreakdown"], result)
            self.assertEqual(result["footer"], "4.3 秒")
            self.assertEqual(result["complete"], "1 / 1 类")
            self.assertTrue(result["refreshed"], result)
        finally:
            page.close()

    def test_size_sort_and_attention_card_filter_follow_count_semantics(self):
        """Sorted bytes stay numeric; KPI shows only high-priority nonzero rows."""
        page = self.new_page()
        try:
            self.goto_shell(page)
            page.wait_for_function("typeof toggleDiskAttentionFilter === 'function'")
            result = page.evaluate(
                """async () => {
                    const previous = window.apiCall;
                    const previousAuth = state.authRequired;
                    state.authRequired = false;
                    const specimen = (id, bytes, priority) => ({
                        id, name: id, location: 'data/' + id, group: '测试',
                        scope: 'Controller', cleanup: '按策略',
                        priority, bytes, files: bytes ? 1 : 0,
                        directories: 1, description: '', complete: true,
                        status: 'scanned', present: true,
                    });
                    const items = [
                        specimen('high9', 9, '高'),
                        specimen('high40', 40, '高'),
                        specimen('medium100', 100, '中'),
                        specimen('high20', 20, '高'),
                        specimen('high0', 0, '高'),
                        specimen('high15', 15, '高'),
                    ];
                    const payload = {
                        data: {
                            generated_at: new Date().toISOString(),
                            readonly: true, cache_hit: false,
                            items, summary: {
                                measured_bytes: 184, attention_categories: 4,
                                controller_data_bytes: 184, local_worker_bytes: 0,
                                aged_bytes: 0, scanned_categories: 6,
                                incomplete_categories: 0, scan_seconds: 1.2,
                                visited_entries: 11, disk: {free_bytes: 0, used_bytes: 184},
                            },
                        },
                    };
                    window.apiCall = (url, ...args) =>
                        url.startsWith('/api/security-audit/disk-artifacts?')
                            ? Promise.resolve(payload)
                            : previous(url, ...args);
                    const ids = () => Array.from(document.querySelectorAll(
                        '#audit-disk-table-body tr[data-artifact-id]'),
                        row => row.dataset.artifactId);
                    try {
                        await loadDiskArtifactAudit(true);
                        const original = ids();
                        const sortButton = document.getElementById('audit-disk-size-sort');
                        sortButton.click();
                        const descending = ids();
                        const descendingState = document.getElementById(
                            'audit-disk-size-heading').getAttribute('aria-sort');
                        sortButton.click();
                        const ascending = ids();
                        const ascendingState = document.getElementById(
                            'audit-disk-size-heading').getAttribute('aria-sort');
                        const attention = document.getElementById(
                            'audit-disk-attention-card');
                        attention.click();
                        const focusRows = ids();
                        const focusCount = document.getElementById('audit-disk-count')
                            .textContent;
                        const pressed = attention.getAttribute('aria-pressed');
                        attention.click();
                        const restored = ids();
                        const unpressed = attention.getAttribute('aria-pressed');
                        attention.click();
                        const priority = document.getElementById('audit-disk-priority');
                        priority.value = '中';
                        priority.dispatchEvent(new Event('change', {bubbles: true}));
                        return {
                            original, descending, ascending, descendingState,
                            ascendingState, focusRows, focusCount, pressed,
                            restored, unpressed, manualRows: ids(),
                            manualUnpressed: attention.getAttribute('aria-pressed'),
                            columnWidth: document.querySelector(
                                '.audit-disk-table thead th:first-child').style.width,
                        };
                    } finally {
                        window.apiCall = previous;
                        state.authRequired = previousAuth;
                    }
                }"""
            )
            self.assertEqual(result["columnWidth"], "220px")
            self.assertEqual(result["original"], [
                "high9", "high40", "medium100", "high20", "high0", "high15",
            ])
            self.assertEqual(result["descending"], [
                "medium100", "high40", "high20", "high15", "high9", "high0",
            ])
            self.assertEqual(result["ascending"], [
                "high0", "high9", "high15", "high20", "high40", "medium100",
            ])
            self.assertEqual(result["descendingState"], "descending")
            self.assertEqual(result["ascendingState"], "ascending")
            self.assertEqual(result["focusRows"], [
                "high9", "high15", "high20", "high40",
            ])
            self.assertEqual(result["focusCount"], "4 / 6 类")
            self.assertEqual(result["pressed"], "true")
            self.assertEqual(len(result["restored"]), 6)
            self.assertEqual(result["unpressed"], "false")
            self.assertEqual(result["manualRows"], ["medium100"])
            self.assertEqual(result["manualUnpressed"], "false")
        finally:
            page.close()
