"""临时探针：量化每日晨报顶部的空白间距（用完即删）。"""

import unittest

from tests.test_runtime_ui_smoke import RuntimeUiHarness


class TmpLayoutProbe(RuntimeUiHarness):
    def test_probe(self):
        page = self.new_page()
        fixture = {
            "run": {
                "run_id": "probe-run", "status": "cancelled", "brief_date": "2026-09-16",
                "issue_count": 9, "waiting_my_reply_count": 9, "no_reply_3_days_count": 2,
                "report_json": {"counts": {"total": 9, "needs_human_review": 0}},
            },
            "issues": [
                {"issue_id": 652135 + i, "status": "pending", "priority": "P1",
                 "subject": "RK3576 Android15 GKI GRF GMS 自动亮度调节问题"}
                for i in range(7)
            ],
        }

        def respond(route):
            route.fulfill(status=200, content_type="application/json",
                          body='{"success":true,"data":' + str(fixture).replace("'", '"') + "}")

        page.route("**/api/redmine-agent/**", respond)
        try:
            page.goto(f"{self.base_url}/redmine-agent", wait_until="domcontentloaded")
            page.wait_for_function("typeof renderDailyBriefInner === 'function'")
            page.set_viewport_size({"width": 1912, "height": 914})
            page.evaluate("switchTab('daily-brief')")
            page.wait_for_selector(".daily-brief-row")
            page.screenshot(path="/tmp/daily_brief_probe.png", clip={"x": 0, "y": 0, "width": 1912, "height": 500})
        finally:
            page.close()


if __name__ == "__main__":
    unittest.main()
