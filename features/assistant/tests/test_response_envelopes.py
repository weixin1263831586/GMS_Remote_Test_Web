"""Agent direct-route responses must preserve the registered HTTP contract."""
import sys
from types import ModuleType, SimpleNamespace

import pytest
from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse

from features.assistant.response import external_knowledge_warnings, format_payload
from features.assistant.route_invocation import call_router_function


@pytest.mark.asyncio
@pytest.mark.parametrize("response,expected_success", [
    ({"success": True, "data": {"items": [1, 2]}}, True),
    ({"success": False, "error": "rejected"}, False),
    (JSONResponse({"success": False, "error": "conflict"}, status_code=409), False),
])
async def test_route_preserves_original_envelope(monkeypatch, response, expected_success):
    module = ModuleType("_audit_response_fixture")
    router = APIRouter()

    @router.get("/audit-contract")
    async def endpoint():
        return response

    module.endpoint = endpoint
    module.router = router
    monkeypatch.setitem(sys.modules, module.__name__, module)
    app = FastAPI()
    app.include_router(router)
    tool = SimpleNamespace(
        name="probe", display_name="Probe", category="system",
        api_path="/audit-contract", method="GET",
        executor_ref=f"{module.__name__}:endpoint",
    )
    result = await call_router_function(tool, None, Request({"type": "http", "method": "GET", "path": "/audit-contract", "headers": [], "app": app}), {})
    assert result.success is expected_success
    if expected_success:
        assert result.data == {"items": [1, 2]}
    else:
        assert result.error


@pytest.mark.parametrize("tool_name", ["sdk_sources", "sdk_sources_list"])
def test_registered_sdk_list_tool_formats_its_actual_payload(tool_name):
    tool = SimpleNamespace(name=tool_name, display_name="SDK sources")
    summary = format_payload(tool, {"success": True, "data": {
        "sources": [{"source_id": "rk3576", "provider": "local_git", "default_revision": "main"}]
    }})
    assert "已登记 1 个 SDK 源" in summary
    assert "rk3576" in summary


@pytest.mark.parametrize("items", [[], [{"title": "CDD clause"}]])
def test_external_knowledge_summary_reports_hits_and_degraded_sources(items):
    tool = SimpleNamespace(name="android_internals_search", display_name="外部知识检索")
    data = {
        "results": items,
        "sources_status": [
            {"source": "web_documents", "status": "ready"},
            {"source": "android_internals", "status": "error", "detail": "/private/path"},
        ],
    }
    summary = format_payload(tool, {"success": True, "message": "Success", "data": data})
    assert ("查询到 1 条" if items else "未检索到") in summary
    assert "Android 机制 Wiki：检索或索引异常" in summary
    assert "/private/path" not in summary
    assert len(external_knowledge_warnings(data)) == 1


def test_personal_knowledge_empty_result_is_not_generic_success():
    tool = SimpleNamespace(name="knowledge_search", display_name="个人知识库检索")
    summary = format_payload(tool, {"success": True, "message": "Success", "data": {"items": []}})
    assert summary == "未检索到相关知识记录。"
