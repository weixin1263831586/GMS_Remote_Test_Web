"""Contracts between FastAPI routes, curated API metadata, and Agent tools."""

import asyncio
import json

from starlette.requests import Request
from starlette.routing import WebSocketRoute

from bootstrap.application import create_app
from features.assistant.tools import registry
from features.system.api import get_api_docs
from features.system.api import router as system_router
from features.system.api_docs_list import API_DOCS_LIST, build_openapi_api_docs


HTTP_METHODS = {"get", "post", "put", "patch", "delete", "options", "head"}


def _http_routes(schema: dict) -> set[tuple[str, str]]:
    return {
        (method.upper(), path)
        for path, path_item in schema["paths"].items()
        for method in path_item
        if method.lower() in HTTP_METHODS
    }


def test_openapi_inventory_covers_every_api_operation():
    schema = create_app().openapi()
    generated = build_openapi_api_docs(schema)
    actual = {(item["method"], item["path"]) for item in generated}
    expected = {
        key for key in _http_routes(schema) if key[1].startswith("/api/")
    }
    assert actual == expected


def test_system_docs_endpoint_returns_the_generated_inventory():
    app = create_app()
    request = Request({"type": "http", "app": app, "headers": []})
    response = asyncio.run(get_api_docs(request))
    payload = json.loads(response.body)
    expected = {
        key for key in _http_routes(app.openapi()) if key[1].startswith("/api/")
    }

    assert payload["source"] == "openapi"
    assert payload["total"] == len(expected)
    assert {(item["method"], item["path"]) for item in payload["apis"]} == expected


def test_curated_docs_reference_real_routes_and_agent_tools():
    app = create_app()
    routes = _http_routes(app.openapi())
    websocket_paths = {
        route.path
        for route in system_router.routes
        if isinstance(route, WebSocketRoute)
    }
    stale = set()
    for item in API_DOCS_LIST:
        path = str(item["path"])
        if not path.startswith("/api/"):
            continue
        method = str(item["method"]).upper()
        if method == "WEBSOCKET":
            if path not in websocket_paths:
                stale.add((method, path))
        elif (method, path) not in routes:
            stale.add((method, path))
    assert stale == set()

    tool_paths = {tool.api_path for tool in registry.get_all_tools()}
    curated_skill_paths = {
        item["path"] for item in API_DOCS_LIST if item.get("skill")
    }
    assert curated_skill_paths - tool_paths == set()


def test_config_read_description_matches_redaction_behavior():
    item = next(
        item
        for item in API_DOCS_LIST
        if item["method"] == "GET" and item["path"] == "/api/config/read"
    )
    assert "脱敏" in item["description"]
    assert "包含所有字段和敏感信息" not in item["description"]
