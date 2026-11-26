"""External knowledge sources display the actual shared GMS scan state."""

import json

from tests.runtime_ui.harness import RuntimeUiHarness, expect


class RuntimeKnowledgeSourceStatusTests(RuntimeUiHarness):
    def test_gms_requirements_status_and_sync_reuse_update_center(self):
        page = self.new_page()
        requests = []
        current = {"coverage": None, "index_consistent": False,
                   "latest_confirmed": False, "latest_scan": {"state": "failed"}}
        scan = {"running": False, "state": "failed", "source": ["gms_requirements"]}

        def reply(route, data):
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({"success": True, "data": data}))

        def start(route):
            requests.append((route.request.method, route.request.url))
            scan.update(running=True, state="running")
            reply(route, {"status": scan})

        try:
            page.route("**/api/knowledge/external/web-sources", lambda route: reply(route, {"sources": []}))
            page.route("**/api/gms-update-center/requirements/coverage", lambda route: reply(route, current))
            page.route("**/api/gms-update-center/scan/status", lambda route: reply(route, {"status": scan}))
            page.route("**/api/gms-update-center/scan?mode=full&source=gms_requirements", start)
            self.goto_shell(page)
            page.evaluate("switchPage('notes', null)")
            page.locator('#page-notes [data-kb-view="external"]').click()
            row = page.locator('[data-kb-source="gms_requirements"]')
            expect(row.locator("td").nth(3)).to_have_text("0")
            expect(row.locator("td").nth(4)).to_have_text("同步失败")
            row.get_by_role("button", name="同步", exact=True).click()
            expect(row.locator("td").nth(4)).to_have_text("同步中")
            expect(row.get_by_role("button", name="同步", exact=True)).to_be_disabled()
            self.assertEqual(len(requests), 1)
            self.assertEqual(requests[0][0], "POST")
            current.update(coverage={"clause_candidates": 8, "sections": 3, "table_rows": 4},
                           index_consistent=True, latest_confirmed=True, latest_scan={"state": "ready"})
            scan.update(running=False, state="completed")
            page.locator('#page-notes [data-kb-refresh]').click()
            expect(row.locator("td").nth(3)).to_have_text("8")
            expect(row.locator("td").nth(4)).to_have_text("已同步（待核验）")
            expect(row.get_by_role("button", name="同步", exact=True)).to_be_enabled()
        finally:
            page.close()


    def test_external_source_actions_fit_and_share_right_edge(self):
        page = self.new_page()

        def reply(route, data):
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({"success": True, "data": data}))

        source = {
            "id": "cdd-17", "name": "Android 17 Compatibility Definition",
            "url": "https://source.android.com/docs/compatibility/17/android-17-cdd",
            "android_version": 17, "kind": "cdd", "enabled": True,
            "revision": "fixture", "indexed_count": 4441,
        }
        try:
            page.route("**/api/knowledge/external/web-sources",
                       lambda route: reply(route, {"sources": [source]}))
            page.route("**/api/gms-update-center/requirements/coverage",
                       lambda route: reply(route, {"coverage": None, "index_consistent": False}))
            page.route("**/api/gms-update-center/scan/status",
                       lambda route: reply(route, {"status": {"running": False}}))
            self.goto_shell(page)
            page.evaluate("switchPage('notes', null)")
            page.locator('#page-notes [data-kb-view="external"]').click()
            table = page.locator("#page-notes .kb-source-table")
            expect(table.locator("tbody tr")).to_have_count(4)
            expect(table.locator("tbody tr").first.get_by_role("button")).to_have_count(6)
            for width in (1920, 1440, 1024):
                page.set_viewport_size({"width": width, "height": 960})
                metrics = table.evaluate("""table => {
                    const wrap = table.closest('[data-kb-sources]');
                    const rows = [...table.tBodies[0].rows].map(row => {
                        const group = row.querySelector('.kb-source-actions');
                        const bounds = group.getBoundingClientRect();
                        const buttons = [...group.children].map(b => b.getBoundingClientRect());
                        return {
                            fits: buttons.every(b => b.left >= bounds.left - 1
                                && b.right <= bounds.right + 1),
                            singleLine: buttons.every(b => Math.abs(b.top - buttons[0].top) <= 1),
                            right: buttons.at(-1).right,
                            aligned: Math.abs(buttons.at(-1).right - bounds.right) <= 1,
                        };
                    });
                    const form = document.querySelector('#page-notes [data-kb-source-form]');
                    return {rows, scrollable: wrap.scrollWidth > wrap.clientWidth,
                        inputWidth: form.elements.name.getBoundingClientRect().width,
                        urlWidth: form.elements.url.getBoundingClientRect().width,
                        nameWidth: table.tHead.rows[0].cells[0].getBoundingClientRect().width,
                        actionWidth: table.tHead.rows[0].cells[5].getBoundingClientRect().width};
                }""")
                self.assertLessEqual(metrics["inputWidth"], 301)
                if width >= 1440:
                    self.assertGreater(metrics["urlWidth"], metrics["inputWidth"])
                self.assertGreaterEqual(metrics["nameWidth"], 299)
                self.assertGreaterEqual(metrics["actionWidth"], 459)
                for row in metrics["rows"]:
                    self.assertTrue(row["fits"] and row["singleLine"] and row["aligned"], metrics)
                    self.assertAlmostEqual(row["right"], metrics["rows"][0]["right"], delta=1)
                if width == 1024:
                    self.assertTrue(metrics["scrollable"], metrics)
            page.set_viewport_size({"width": 1920, "height": 960})
            page.evaluate("""() => {
                window.__openedSourceUrls = [];
                window.open = (...args) => { window.__openedSourceUrls.push(args); return null; };
            }""")
            table.locator("tbody tr").first.get_by_role("button", name="打开网址", exact=True).click()
            table.locator('[data-kb-source="gms_requirements"]').get_by_role(
                "button", name="打开网址", exact=True).click()
            self.assertEqual(page.evaluate("window.__openedSourceUrls"), [
                [source["url"], "_blank", "noopener,noreferrer"],
                ["https://docs.partner.android.com/gms/policies/domains/reqs",
                 "_blank", "noopener,noreferrer"],
            ])
            self.assertEqual(table.get_by_role("button", name="打开网址", exact=True).count(), 2)
        finally:
            page.close()


    def test_semantic_index_builds_remaining_batches_and_stops_without_progress(self):
        page = self.new_page()
        calls = []
        state = {"status": "empty", "indexed": 0, "total": 260, "coverage_complete": False}

        def reply(route, data):
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({"success": True, "data": data}))

        def sync(route):
            calls.append(route.request.method)
            if len(calls) <= 3:
                state["indexed"] = min(260, state["indexed"] + 128)
                state["coverage_complete"] = state["indexed"] == 260
                state["status"] = "ready" if state["coverage_complete"] else "partial"
            reply(route, state)

        try:
            page.route("**/api/knowledge/external/web-sources", lambda route: reply(route, {"sources": []}))
            page.route("**/api/gms-update-center/requirements/coverage", lambda route: reply(route, {}))
            page.route("**/api/gms-update-center/scan/status", lambda route: reply(route, {}))
            page.route("**/api/knowledge/external/wiki-semantic-index", lambda route: reply(route, state))
            page.route("**/api/knowledge/external/wiki-semantic-index?batch_size=128", sync)
            self.goto_shell(page)
            page.evaluate("switchPage('notes', null)")
            page.locator('#page-notes [data-kb-view="external"]').click()
            page.evaluate("window.showConfirmDialog = async () => true")
            row = page.locator(".kb-source-table tr").filter(has_text="Android Internals Wiki")
            button = row.get_by_role("button", name="语义索引", exact=True)
            button.click()
            expect(button).to_be_enabled(timeout=10000)
            self.assertEqual(calls, ["POST", "POST", "POST"])
            self.assertTrue(state["coverage_complete"])
            state.update(status="partial", indexed=128, coverage_complete=False)
            button.click()
            expect(button).to_be_enabled(timeout=10000)
            self.assertEqual(len(calls), 4)
        finally:
            page.close()
