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
            self.assertGreater(page.locator("#api-docs-table-body tr").count(), 50)
        finally:
            page.close()
