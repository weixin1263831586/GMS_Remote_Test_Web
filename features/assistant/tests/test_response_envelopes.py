"""Agent direct-route responses must preserve the registered HTTP contract."""
import sys
from types import ModuleType, SimpleNamespace

import pytest
from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse

from features.assistant.executor_formatting import format_payload
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
