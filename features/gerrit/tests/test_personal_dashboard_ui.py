"""Exercise first-run Gerrit setup in a browser with isolated API fixtures."""

import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest


playwright_api = pytest.importorskip("playwright.sync_api")
ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def dashboard():
    with playwright_api.sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(headless=True)
        except playwright_api.Error as exc:
            pytest.skip(f"Chromium unavailable: {exc}")
        page = browser.new_page()
        page.set_default_timeout(5000)
        state = {
            "config": {"personal_profiles": [], "department_profiles": [], "default_owner": ""},
            "requests": [],
            "errors": [],
            "fail_statistics": False,
        }
        page.on("pageerror", lambda error: state["errors"].append(str(error)))

        def respond(route):
            url = urlsplit(route.request.url)
            assets = {
                "/gerrit-dashboard": ROOT / "features/gerrit/ui/page.html",
                "/static/js/utils.js": ROOT / "web/static/js/utils.js",
                "/static/js/embedded-workspace.js": ROOT / "web/static/js/embedded-workspace.js",
            }
            if url.path in assets:
                route.fulfill(
                    content_type="text/html" if url.path == "/gerrit-dashboard" else "text/javascript",
                    body=assets[url.path].read_text(),
                )
                return
            state["requests"].append(url)
            data = {}
            if url.path.endswith("/config"):
                data = state["config"]
            elif url.path.endswith("/statistics/personal"):
                if state["fail_statistics"]:
                    route.fulfill(
                        status=503, content_type="application/json",
                        body=json.dumps({"success": False, "error": "Temporary Gerrit failure"}),
                    )
                    return
                params = parse_qs(url.query)
                profiles = state["config"]["personal_profiles"]
                profile = next((p for p in profiles if p["id"] == params.get("profile_id", [""])[0]), None)
                owner = params.get("owner", [""])[0] or state["config"]["default_owner"].strip()
                if not profile:
                    profile = {"id": owner, "owner": owner} if owner else profiles[0]
                data = {"owner": profile["owner"], "profile": profile, "summary": {}, "trends": {}}
            route.fulfill(content_type="application/json", body=json.dumps({"success": True, "data": data}))

        page.route("**/*", respond)
        try:
            yield page, state
            assert state["errors"] == []
        finally:
            browser.close()


def open_dashboard(page):
    page.goto("https://gerrit-ui.test/gerrit-dashboard")
    page.evaluate("() => gerritInitPromise")


def statistics_requests(state):
    return [url for url in state["requests"] if url.path.endswith("/statistics/personal")]


def test_empty_setup_stays_actionable_on_refresh_and_revisit(dashboard):
    page, state = dashboard
    state["config"]["default_owner"] = "   "
    open_dashboard(page)
    for _ in range(2):
        page.evaluate("() => refreshCurrentTab()")
        page.evaluate("switchTab('query'); switchTab('personal')")
        playwright_api.expect(page.locator("#personalContent")).to_contain_text("尚未设置 Gerrit 统计邮箱")
        playwright_api.expect(page.locator("#owner")).to_be_visible()
        playwright_api.expect(page.locator("#personalContent")).to_have_attribute("aria-busy", "false")
    assert statistics_requests(state) == []
    playwright_api.expect(page.locator("#gerrit-local-toast")).to_have_count(0)
    page.locator('[title="添加成员"]').click()
    playwright_api.expect(page.locator("#addPersonalModal")).to_have_class("modal show")
    page.keyboard.press("Escape")
    page.locator("#owner").fill("  member@example.com  ")
    page.evaluate("() => refreshCurrentTab()")
    assert parse_qs(statistics_requests(state)[0].query)["owner"] == ["member@example.com"]
    playwright_api.expect(page.locator("#personalContent")).to_have_attribute("data-loaded", "true")


def test_failed_first_query_keeps_owner_and_can_retry(dashboard):
    page, state = dashboard
    state["config"]["default_owner"] = "member@example.com"
    state["fail_statistics"] = True
    open_dashboard(page)
    playwright_api.expect(page.locator("#owner")).to_have_value("member@example.com")
    playwright_api.expect(page.locator("#personalContent")).to_contain_text("Temporary Gerrit failure")
    state["fail_statistics"] = False
    page.evaluate("() => refreshCurrentTab()")
    playwright_api.expect(page.locator("#personalContent")).to_have_attribute("data-loaded", "true")
    assert len(statistics_requests(state)) == 2


@pytest.mark.parametrize("default_owner", ["", "default@example.com"])
def test_resolved_owner_matches_controls_reviews_and_manual_query(dashboard, default_owner):
    page, state = dashboard
    state["config"].update({
        "default_owner": default_owner,
        "personal_profiles": [
            {"id": "z", "name": "Z", "owner": "z@example.com"},
            {"id": "a", "name": "A", "owner": "a@example.com"},
        ],
    })
    open_dashboard(page)
    expected_owner = default_owner or "z@example.com"
    playwright_api.expect(page.locator("#owner")).to_have_value(expected_owner)
    review = next(url for url in state["requests"] if url.path.endswith("/review-queue"))
    assert parse_qs(review.query)["owner"] == [expected_owner]
    assert page.evaluate("trendOwners") == [expected_owner]
    page.locator("#owner").fill("custom@example.com")
    page.evaluate("() => refreshCurrentTab()")
    query = parse_qs(statistics_requests(state)[-1].query)
    assert query["owner"] == ["custom@example.com"]
    assert "profile_id" not in query
    playwright_api.expect(page.locator("#owner")).to_have_value("custom@example.com")
