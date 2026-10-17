from fastapi import FastAPI
from fastapi.testclient import TestClient

from features.auth import CurrentUser
from features.users import config_api


def test_shell_defaults_are_available_without_exposing_admin_configuration(monkeypatch):
    monkeypatch.setenv('GMS_AUTH_REQUIRED', 'true')
    monkeypatch.setattr(config_api, '_config_view', lambda _request: {
        'effective_ubuntu_user': 'tester', 'effective_suites_path': '/suites', 'local_server': 'me@192.0.2.1',
        'client_hosts': {'private': 'other'}, 'device_hosts': ['private-host'],
        'wifi': {'ssid': 'lab', 'password': 'private-password', 'has_password': True},
    })
    app = FastAPI()

    @app.middleware('http')
    async def authenticate(request, call_next):
        request.state.current_user = CurrentUser(id='user', username='tester', role='user')
        request.state.auth_method = 'session'
        return await call_next(request)

    app.include_router(config_api.router)
    with TestClient(app) as client:
        assert client.get('/api/config/read').status_code == 403
        response = client.get('/api/config/client')
    assert response.status_code == 200
    assert response.json()['effective_suites_path'] == '/suites'
    assert 'client_hosts' not in response.json()
    assert 'device_hosts' not in response.json()
    assert 'private-password' not in response.text
    assert response.json()['wifi']['password'] == ''


def test_opengrok_config_requires_authenticated_user(monkeypatch):
    monkeypatch.setenv('GMS_AUTH_REQUIRED', 'true')
    app = FastAPI()
    app.include_router(config_api.router)
    with TestClient(app) as client:
        assert client.get('/api/config/opengrok').status_code == 401


def test_opengrok_config_served_in_dev_mode(monkeypatch):
    monkeypatch.delenv('GMS_AUTH_REQUIRED', raising=False)

    class FakeConfigManager:
        def load_config(self):
            return {'opengrok': {'base_url': 'http://opengrok.local/source', 'project': 'gms'}}

    monkeypatch.setattr(config_api, 'config_manager', FakeConfigManager())
    app = FastAPI()
    app.include_router(config_api.router)
    with TestClient(app) as client:
        response = client.get('/api/config/opengrok')
    assert response.status_code == 200
    assert response.json()['data']['base_url'] == 'http://opengrok.local/source'
