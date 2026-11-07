"""Resource routing stays allowlisted and independent of the business API schema."""

import re

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from features.redmine.page import _ASSET_TYPES, PAGE_SCRIPTS, page_router
from foundation.error_model import register_api_error_handler


@pytest.fixture
def client():
    app = FastAPI()
    register_api_error_handler(app)
    app.include_router(page_router)
    with TestClient(app) as client:
        yield client


def test_page_declares_all_scripts_once_in_dependency_order(client):
    html = client.get("/redmine-agent")
    assert html.status_code == 200
    scripts = re.findall(r'<script src="/redmine-agent/assets/([^"]+)"', html.text)
    assert tuple(scripts) == PAGE_SCRIPTS
    assert len(scripts) == len(set(scripts))
    assert scripts[-1] == "page.js"
    for name, media_type in _ASSET_TYPES.items():
        response = client.get(f"/redmine-agent/assets/{name}")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith(media_type)
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["x-content-type-options"] == "nosniff"


@pytest.mark.parametrize("name", ["page.html", "page.css", "page.py", "unknown.js", "%2e%2e%2fpage.py", "core.js.map"])
def test_assets_do_not_expose_unlisted_files_or_paths(client, name):
    assert client.get(f"/redmine-agent/assets/{name}").status_code == 404


def test_resources_are_not_business_openapi_operations(client):
    paths = client.get("/openapi.json").json()["paths"]
    assert not any(path.startswith("/redmine-agent/assets/") for path in paths)
    assert "/redmine-agent/{asset_name}" not in paths


def test_legacy_urls_work_without_loading_helpers_twice(client):
    legacy = client.get("/redmine-agent/page.js")
    assert legacy.status_code == 200
    assert "function loadStatistics" in legacy.text
    assert "function startDailyBriefRun" in legacy.text
    # markdown-table.js 已并入 core.js；其余 legacy 助手仍按原 URL 提供。
    for name in ("daily-brief-session.js", "daily-brief-diagnosis.js"):
        helper = client.get(f"/redmine-agent/{name}")
        assert helper.status_code == 200
        assert helper.text == client.get(f"/redmine-agent/assets/{name}").text
        assert helper.text not in legacy.text
    assert client.get("/redmine-agent/markdown-table.js").status_code == 404
    assert client.get("/redmine-agent/core.js").status_code == 404


def test_merged_markdown_table_helper_serves_from_core_js(client):
    core = client.get("/redmine-agent/assets/core.js")
    assert core.status_code == 200
    assert "function _splitMarkdownTableRow" in core.text
