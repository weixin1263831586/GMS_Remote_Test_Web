"""Contracts between FastAPI routes, curated API metadata, and Agent tools."""

import asyncio
import json

from starlette.requests import Request
from starlette.routing import WebSocketRoute

from bootstrap.application import create_app
from features.assistant.tools import registry
from features.system.api import get_api_docs, get_api_help
from features.system.api import router as system_router
from features.system.api_catalog import build_api_catalog, curl_example, resolve_schema
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


def test_every_request_body_is_visible_and_retains_schema_and_media_type():
    document = create_app().openapi()
    entries = {(api["method"], api["path"]): api for api in build_api_catalog(document)}
    for path, path_item in document["paths"].items():
        if not path.startswith("/api/"):
            continue
        for method, operation in path_item.items():
            if method not in HTTP_METHODS or "requestBody" not in operation:
                continue
            entry = entries[method.upper(), path]
            body = resolve_schema(operation["requestBody"], document)
            assert entry["request_body"]["required"] == bool(body.get("required"))
            assert entry["params"], f"{method} {path} body is missing from UI parameters"
            assert set(entry["request_body"]["content"]) == set(body["content"])
            for media_type, content in body["content"].items():
                actual = entry["request_body"]["content"][media_type]
                assert actual["schema"] == content.get("schema", {})
            assert entry["responses"], f"{method} {path} response model is missing"


def test_docs_and_help_share_every_method_and_path():
    app = create_app()
    request = Request({"type": "http", "app": app, "headers": [], "scheme": "https", "server": ("controller", 443), "path": "/"})
    payload = json.loads(asyncio.run(get_api_docs(request)).body)
    help_text = asyncio.run(get_api_help(request)).body.decode()
    inventory = {
        tuple(line.split()) for line in help_text.splitlines()
        if line and line.split()[0].lower() in HTTP_METHODS
    }
    assert inventory == {(api["method"], api["path"]) for api in payload["apis"]}
    for path in ("api/auth/agent-enroll", "/api/cluster/commands", "/api/automation/runs"):
        response = asyncio.run(get_api_help(request, path))
        assert response.status_code == 200
        assert "Content-Type:" in response.body.decode()


def test_catalog_examples_preserve_body_path_query_and_http_method():
    schema = {
        "paths": {
            "/api/items/{item_id}": {
                "parameters": [{"name": "item_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
                **{method: {
                    "parameters": [{"name": "force", "in": "query", "schema": {"type": "boolean"}}],
                    "requestBody": {"required": True, "content": {
                        "application/json": {"schema": {"$ref": "#/components/schemas/Input"}}
                    }},
                    "responses": {"200": {"description": "OK", "content": {
                        "application/json": {"schema": {"$ref": "#/components/schemas/Input"}}
                    }}},
                }
                for method in ("post", "put", "patch", "delete")},
            }
        },
        "components": {"schemas": {"Input": {"type": "object", "required": ["name"], "properties": {
            "name": {"type": "string"}, "items": {"type": "array", "items": {"type": "integer"}}
        }}}},
    }
    for entry in build_api_catalog(schema):
        assert next(p for p in entry["params"] if p["name"] == "name")["required"]
        command = curl_example(entry, "https://controller")
        assert f'-X {entry["method"]}' in command
        assert "/api/items/1?force=True" in command
        assert "--data-raw" in command
        assert '"name": "VALUE"' in command
        assert entry["responses"]["200"]["content"]["application/json"]["schema"]["properties"]


def test_catalog_multipart_and_raw_array_bodies_are_not_lost():
    document = {"paths": {
        "/api/upload": {"post": {
            "requestBody": {"required": True, "content": {"multipart/form-data": {"schema": {
                "type": "object", "required": ["files"], "properties": {
                    "files": {"type": "array", "items": {"type": "string", "format": "binary"}},
                    "label": {"type": "string"},
                },
            }}}}, "responses": {"204": {"description": "Done"}},
        }},
        "/api/batch": {"patch": {
            "requestBody": {"required": True, "content": {"application/json": {"schema": {
                "type": "array", "items": {"type": "integer"},
            }}}}, "responses": {"200": {"description": "Done"}},
        }},
    }}
    entries = {entry["path"]: entry for entry in build_api_catalog(document)}
    upload = entries["/api/upload"]
    assert upload["params"][0]["type"] == "file"
    command = curl_example(upload, "https://controller")
    assert "files=@/path/to/file" in command
    assert "Content-Type: multipart/form-data" not in command  # curl owns boundary
    batch = entries["/api/batch"]
    assert batch["params"][0]["name"] == "body"
    assert batch["params"][0]["required"]
    assert "[1]" in curl_example(batch, "https://controller")
