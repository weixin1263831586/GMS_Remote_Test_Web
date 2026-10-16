from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from features.auth import CurrentUser
from features.devices import adb_forward_api as api


@pytest.mark.parametrize('action', ['start', 'stop'])
@pytest.mark.parametrize('role,method,elevated,expected', [
    (None, 'session', False, 401), ('user', 'session', False, 403),
    ('admin', 'session', False, 403), ('admin', 'session', True, 200),
    ('user', 'agent_token', True, 403),
])
def test_legacy_tunnels_require_human_elevation(monkeypatch, action, role, method, elevated, expected):
    monkeypatch.setenv('GMS_AUTH_REQUIRED', 'true')
    manager = Mock()
    manager.config_manager.load_config.return_value = {'device_host': 'test@192.0.2.1'}
    manager.start_forward.return_value = manager.stop_forward.return_value = {'success': True}
    monkeypatch.setattr(api, 'adb_forward_manager', manager)
    app = FastAPI()

    @app.middleware('http')
    async def authenticate(request, call_next):
        if role:
            request.state.current_user = CurrentUser(
                id='test', username='test', role=role, extra_permissions=frozenset({'devices.lease'}),
            )
            request.state.auth_method = method
            request.state.is_elevated = elevated
        return await call_next(request)

    app.include_router(api.router)
    with TestClient(app) as client:
        assert client.post('/api/adb-forward/' + action, json={}).status_code == expected
    assert getattr(manager, action + '_forward').call_count == (1 if expected == 200 else 0)


@pytest.mark.parametrize('action', ['start', 'stop'])
@pytest.mark.parametrize('auth_method,workers,expected', [
    ('session', '*', 200), ('agent_token', 'source,target', 200), ('agent_token', 'source', 403),
])
def test_worker_tunnel_retains_resource_authorization_without_legacy_elevation(
    monkeypatch, action, auth_method, workers, expected,
):
    service = Mock(connect=AsyncMock(return_value={'success': True}),
                   disconnect=AsyncMock(return_value={'success': True}))
    monkeypatch.setattr(api, 'adb_proxy_service', service)
    from starlette.requests import Request

    request = Request({'type': 'http', 'method': 'POST', 'headers': []})
    request.state.current_user = CurrentUser(id='user', username='user', role='user')
    request.state.auth_method = auth_method
    request.state.agent_token_record = {'allowed_workers': workers}
    request.state.is_elevated = False
    body_type = api.ADBForwardStartRequest if action == 'start' else api.ADBForwardStopRequest
    body = body_type(source_worker_id='source', target_worker_id='target')
    import asyncio

    if expected == 403:
        with pytest.raises(HTTPException) as caught:
            asyncio.run(getattr(api, action + '_adb_forward')(request, body))
        assert caught.value.status_code == 403
        service.connect.assert_not_awaited()
        service.disconnect.assert_not_awaited()
    else:
        response = asyncio.run(getattr(api, action + '_adb_forward')(request, body))
        assert response.status_code == 200
