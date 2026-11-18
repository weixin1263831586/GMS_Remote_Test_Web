"""Dynamic calls use the active app's registered authorization context."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import APIRouter, Depends, FastAPI, HTTPException

from features.assistant.route_invocation import enforce_route_dependencies
from foundation.routing_introspection import flatten_app_routes


@pytest.mark.asyncio
@pytest.mark.parametrize('source', ['request', 'assembled'])
async def test_unmounted_module_router_cannot_authorize_an_app_call(monkeypatch, source):
    from features.assistant import route_invocation

    router = APIRouter()

    @router.get('/unmounted')
    def endpoint():
        return {}

    app = FastAPI()
    request = SimpleNamespace(app=app) if source == 'request' else SimpleNamespace()
    monkeypatch.setattr(route_invocation, '_get_global_app_routes', lambda: flatten_app_routes(app))
    with pytest.raises(HTTPException) as rejected:
        await enforce_route_dependencies(
            SimpleNamespace(router=router), endpoint, request, api_path='/unmounted', method='GET',
        )
    assert rejected.value.status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize('source', ['request', 'assembled'])
@pytest.mark.parametrize('executor_ref', ['wrapper', 'endpoint'])
async def test_included_router_keeps_parent_authorization(monkeypatch, source, executor_ref):
    from features.assistant import route_invocation

    guard = Mock(side_effect=HTTPException(403, 'authorization required'))

    def require_guard(request):
        guard(request)

    router = APIRouter()

    @router.get('/resource')
    def implementation():
        return {}

    def public_wrapper():
        return implementation()

    app = FastAPI()
    app.include_router(router, prefix='/api', dependencies=[Depends(require_guard)])
    request = SimpleNamespace(app=app) if source == 'request' else SimpleNamespace()
    monkeypatch.setattr(route_invocation, '_get_global_app_routes', lambda: flatten_app_routes(app))
    with pytest.raises(HTTPException) as rejected:
        await enforce_route_dependencies(
            SimpleNamespace(), public_wrapper if executor_ref == 'wrapper' else implementation, request,
            api_path='/api/resource' if executor_ref == 'wrapper' else '/api/stale-metadata', method='GET',
        )
    assert rejected.value.status_code == 403
    guard.assert_called_once_with(request)


@pytest.mark.asyncio
@pytest.mark.parametrize('source', ['request', 'assembled', 'module'])
async def test_endpoint_identity_cannot_replace_the_requested_http_method(monkeypatch, source):
    from features.assistant import route_invocation

    router = APIRouter()

    @router.get('/resource')
    def endpoint():
        return {}

    app = FastAPI()
    app.include_router(router)
    request = SimpleNamespace(app=app) if source == 'request' else SimpleNamespace()
    monkeypatch.setattr(route_invocation, '_get_global_app_routes', lambda: flatten_app_routes(app))
    with pytest.raises(HTTPException) as rejected:
        await enforce_route_dependencies(
            SimpleNamespace(router=router), endpoint, request,
            api_path='/resource' if source != 'module' else '', method='POST',
        )
    assert rejected.value.status_code == 404


@pytest.mark.asyncio
async def test_unregistered_function_fails_closed_without_importing_global_app(monkeypatch):
    from features.assistant import route_invocation

    global_routes = Mock(side_effect=AssertionError('no app discovery without a route path'))
    monkeypatch.setattr(route_invocation, '_get_global_app_routes', global_routes)
    with pytest.raises(HTTPException) as rejected:
        await enforce_route_dependencies(SimpleNamespace(), lambda: None, None)
    assert rejected.value.status_code == 404
    global_routes.assert_not_called()

@pytest.mark.asyncio
async def test_vpn_status_handler_forwards_request_to_human_only_route(monkeypatch):
    from features.assistant.executor import ActionExecutor

    calls = []

    async def fetch(*args, **kwargs):
        calls.append((args, kwargs))
        return None, {"success": True, "connected": True, "source": "local"}

    monkeypatch.setattr(ActionExecutor, "_fetch_router_json", staticmethod(fetch))
    request = SimpleNamespace(actor="browser-user")
    result = await ActionExecutor()._query_vpn_status({}, request, {})
    assert result.success
    assert "已连接" in result.formatted_text
    assert len(calls) == 1
    assert calls[0][0] == ("features.system.integrations", "get_vpn_status")
    assert calls[0][1]["request"] is request
    assert calls[0][1]["api_path"] == "/api/vpn/status"
    assert calls[0][1]["method"] == "GET"
