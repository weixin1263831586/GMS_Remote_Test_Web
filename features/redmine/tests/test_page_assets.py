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
    # 脚本 URL 允许带 ?v= 缓存戳（弹框重构后引入）；剥掉后比对清单。
    scripts = [
        name.split("?v=", 1)[0]
        for name in re.findall(r'<script src="/redmine-agent/assets/([^"]+)"', html.text)
    ]
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


def test_legacy_asset_route_is_retired(client):
    """旧版无 assets/ 前缀的资源 URL 不再提供，未知资源保持 404。"""
    for name in ("page.js", "daily-brief-session.js", "daily-brief-diagnosis.js"):
        response = client.get(f"/redmine-agent/{name}")
        assert response.status_code == 404
    # 未登记文件依旧不得经 assets/ 路由泄漏。
    assert client.get("/redmine-agent/assets/markdown-table.js").status_code == 404


def test_merged_markdown_table_helper_serves_from_core_js(client):
    core = client.get("/redmine-agent/assets/core.js")
    assert core.status_code == 200
    assert "function _splitMarkdownTableRow" in core.text


def test_shared_diagnosis_helpers_serve_from_core_js(client):
    core = client.get("/redmine-agent/assets/core.js")
    assert core.status_code == 200
    assert "function dailyBriefEvidenceTag(" in core.text
    assert "function dailyBriefEvidenceGateNotice(" in core.text
    assert "function dailyBriefSystemMetadata(" in core.text
    assert "function dailyBriefQualityChip(" in core.text
    assert "function singleIssueAnalysisMeta(" in core.text
    assert "function dailyBriefPolicyEvidence(" in core.text
    assert client.get("/redmine-agent/assets/daily-brief-diagnosis.js").status_code == 404
