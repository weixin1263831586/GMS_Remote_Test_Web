"""API details and command examples consume real Controller OpenAPI metadata."""

import json
import shlex

from tests.runtime_ui.harness import RuntimeUiHarness


class ApiCatalogBrowserTests(RuntimeUiHarness):
    def test_all_request_body_operations_render_parameters_and_correct_methods(self):
        page = self.new_page()
        try:
            payload = page.request.get(f"{self.base_url}/api/system/docs")
            self.assertTrue(payload.ok, payload.text()[:500])
            catalog = payload.json()["apis"]
            body_operations = [api for api in catalog if api.get("request_body")]
            page.route(
                "**/api/system/docs",
                lambda route: route.fulfill(
                    status=200, content_type="application/json",
                    body=json.dumps({"success": True, "apis": body_operations}),
                ),
            )
            self.goto_shell(page)
            page.evaluate("() => switchPage('api-docs')")
            results = page.evaluate("""apis => apis.map(api => {
                const details = getApiDetails(api);
                return {
                    path: api.path, method: api.method,
                    title: details.title, params: generateParamsHtml(details),
                    command: generateCurlCommand(api, details).full,
                };
            })""", body_operations)
            self.assertGreater(len(results), 50)
            for api, result in zip(body_operations, results):
                with self.subTest(method=api["method"], path=api["path"]):
                    self.assertEqual(result["title"], api["title"])
                    self.assertNotIn("无参数", result["params"])
                    arguments = shlex.split(result["command"])
                    self.assertEqual(arguments[arguments.index("-X") + 1], api["method"])
                    self.assertNotIn("{", arguments[arguments.index("-X") + 2])
                    if api["content_type"] == "multipart/form-data":
                        self.assertIn("-F", arguments)
                    elif "json" in api["content_type"]:
                        self.assertIn("--data-raw", arguments)
                        json.loads(arguments[arguments.index("--data-raw") + 1])
            page.evaluate("() => changeApiDocsPageSize(100)")
            self.assertGreater(page.locator("#api-docs-table-body tr").count(), 50)
        finally:
            page.close()
    def test_table_header_aligns_with_method_distribution(self):
        page = self.new_page()
        try:
            self.goto_shell(page)
            page.evaluate("() => switchPage('api-docs')")
            page.wait_for_function("document.querySelector('#total-apis-count')?.textContent.trim() !== '-'")
            layout = page.evaluate("""() => {
                const left = document.querySelector('#page-api-docs .api-docs-table thead').getBoundingClientRect();
                const right = document.querySelector('#page-api-docs .api-side-heading').getBoundingClientRect();
                return {
                    redundantHeading: !!document.querySelector('#page-api-docs .api-panel-heading'),
                    topDelta: Math.abs(left.top - right.top),
                    heightDelta: Math.abs(left.height - right.height),
                    leftHeight: left.height,
                    columns: document.querySelectorAll('#page-api-docs .api-docs-table th').length,
                    pager: document.querySelector('#api-docs-pagination').textContent,
                };
            }""")
            self.assertFalse(layout["redundantHeading"])
            self.assertLessEqual(layout["topDelta"], 1, layout)
            self.assertLessEqual(layout["heightDelta"], 1, layout)
            # The UI geometry standard uses a 42px shared data-grid header.
            # The right method-distribution heading must match exactly.
            self.assertAlmostEqual(layout["leftHeight"], 42, delta=1, msg=str(layout))
            self.assertEqual(layout["columns"], 5, layout)
            self.assertIn("共", layout["pager"])
        finally:
            page.close()

    def test_distribution_geometry_is_shared_with_security_audit(self):
        page = self.new_page()
        try:
            self.goto_shell(page)
            for width, diameter, center in ((1440, 145, 105), (1280, 124, 88)):
                page.set_viewport_size({"width": width, "height": 900})
                for route in ("api-docs", "security-audit"):
                    page.evaluate("(name) => switchPage(name)", route)
                    chart = page.locator(f"#page-{route} .ui-distribution-chart:visible").first
                    chart.wait_for(state="visible")
                    geometry = chart.evaluate("""element => {
                        const ring = element.querySelector('.ui-distribution-ring').getBoundingClientRect();
                        const center = element.querySelector('.ui-distribution-center').getBoundingClientRect();
                        const heading = element.parentElement.querySelector('.ui-panel-heading').getBoundingClientRect();
                        return [ring.width, ring.height, center.width, center.height, heading.height];
                    }""")
                    with self.subTest(width=width, route=route):
                        for actual, expected in zip(geometry, (diameter, diameter, center, center, 42)):
                            self.assertAlmostEqual(actual, expected, delta=1, msg=str(geometry))
        finally:
            page.close()

    def test_pagination_footer_is_conditional(self):
        page = self.new_page()
        try:
            self.goto_shell(page)
            page.evaluate("switchPage('api-docs')")
            page.wait_for_function("allApiDocs.length > 21")
            footer = page.locator('#api-docs-pagination')
            for total in (0, 14, 20, 21):
                page.evaluate("(total) => { apiDocsPageSize = 20; apiDocsPage = 1; displayApiDocs(allApiDocs.slice(0, total)); }", total)
                self.assertEqual(footer.is_visible(), total > 20)
                if total <= 20:
                    self.assertEqual(footer.evaluate('el => el.getBoundingClientRect().height'), 0)
            page.evaluate("() => { apiDocsPageSize = 50; apiDocsPage = 1; displayApiDocs(allApiDocs.slice(0, 21)); }")
            self.assertTrue(footer.is_visible())
            self.assertEqual(footer.locator('nav').count(), 0)
            self.assertEqual(footer.locator('select').input_value(), '50')
        finally:
            page.close()

    def test_dashboard_filters_and_page_controls(self):
        page = self.new_page()
        try:
            self.goto_shell(page)
            page.evaluate("() => switchPage('api-docs')")
            page.wait_for_function("document.querySelector('#total-apis-count')?.textContent.trim() !== '-'")
            result = page.evaluate("""() => ({
                total: allApiDocs.length,
                visible: document.querySelectorAll('#api-docs-table-body tr').length,
                totalCard: Number(document.querySelector('#total-apis-count').textContent),
                methodLegend: document.querySelector('#api-docs-legend-get').textContent,
            })""")
            self.assertGreater(result["total"], 100)
            self.assertEqual(result["visible"], 20)
            self.assertEqual(result["totalCard"], result["total"])
            self.assertIn("%)", result["methodLegend"])
            page.evaluate("() => goApiDocsPage(2)")
            self.assertEqual(page.locator("#api-docs-table-body tr").count(), 20)
            page.evaluate("() => filterApiDocsMethod('POST')")
            self.assertEqual(page.locator("#api-docs-table-body tr .api-method-post").count(), 20)
            self.assertEqual(page.locator("#api-docs-method-actions [data-method='POST']").get_attribute("aria-pressed"), "true")
            page.evaluate("() => filterApiDocsMethod('all')")
            page.locator("#api-search-input").fill("/api/agent")
            page.wait_for_timeout(400)
            self.assertGreater(page.locator("#api-docs-table-body tr").count(), 0)
            self.assertLessEqual(page.locator("#api-docs-table-body tr").count(), 20)
            for query in ("__no_api_matches_fixture__", "", "/api/agent"):
                page.locator("#api-search-input").fill(query)
                page.wait_for_timeout(400)
                rows = page.locator("#api-docs-table-body tr")
                self.assertGreater(rows.count(), 0)
                self.assertLessEqual(rows.count(), 20)
                self.assertEqual(rows.evaluate_all("""rows => rows.filter(row =>
                    !row.textContent.trim() && !row.querySelector('button,input,svg,a')).length"""), 0)
                if query == "__no_api_matches_fixture__":
                    self.assertEqual(rows.count(), 1)
                    self.assertEqual(rows.first.locator("td").get_attribute("colspan"), "5")
            page.evaluate("switchPage('users', null)")
            page.evaluate("switchPage('api-docs', null)")
            self.assertEqual(page.locator("#api-docs-table-body tr").evaluate_all(
                """rows => rows.filter(row => !row.textContent.trim()
                    && !row.querySelector('button,input,svg,a')).length"""
            ), 0)
        finally:
            page.close()
