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
