"""set-username 输入校验回归。

用户名最终进入 client_hosts 持久化并拼进展示/SSH 路由身份
（``user@ip``），必须在模型层拒绝超长与非常规字符（pydantic 422）。
"""

from __future__ import annotations

import threading
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

import features.users.users_api as users_api


def _app():
    app = FastAPI()
    app.include_router(users_api.router)
    return app


def test_overlong_username_rejected_with_422():
    with TestClient(_app()) as client:
        response = client.post('/api/users/set-username', json={'username': 'a' * 65})
    assert response.status_code == 422


def test_username_charset_rejected_with_422():
    with TestClient(_app()) as client:
        for bad in ('bad name', 'user;rm', 'user@evil', '../etc', '用户名 ok'):
            response = client.post('/api/users/set-username', json={'username': bad})
            assert response.status_code == 422, bad


def test_valid_username_still_saved(monkeypatch):
    saved = {}

    class FakeConfigManager:
        def save_client_host(self, client_ip, username):
            saved['ip'] = client_ip
            saved['username'] = username
            return True

        def get_runtime_config(self):
            return {'client_hosts': {saved.get('ip', ''): saved.get('username', '')}}

    monkeypatch.setattr(
        users_api.runtime,
        'config_manager',
        FakeConfigManager(),
    )
    monkeypatch.setattr(
        users_api.runtime,
        'global_state',
        SimpleNamespace(user_states={}, user_states_lock=threading.Lock()),
    )
    monkeypatch.setattr(
        users_api, 'get_client_id_from_request', lambda request: 'tester@192.0.2.9'
    )
    with TestClient(_app()) as client:
        response = client.post('/api/users/set-username', json={'username': 'alice-dev_01'})
    assert response.status_code == 200, response.text
    assert saved['username'] == 'alice-dev_01'
    assert users_api.client_manager.client_hosts.get(saved['ip']) == 'alice-dev_01'
