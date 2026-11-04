"""The force-release HTTP contract retains elevation and reports conflicts."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from features.auth import CurrentUser
from features.devices import operations_api
from features.devices.claim_release import force_release_device_lock
from foundation.device_locks import DeviceLockManager


@pytest.fixture
def client(monkeypatch):
    app = FastAPI()

    @app.middleware("http")
    async def identity(request: Request, call_next):
        request.state.current_user = CurrentUser(id="alice", username="alice", role="admin")
        request.state.is_elevated = request.headers.get("X-Test-Elevated") == "1"
        if request.headers.get("Authorization"):
            request.state.auth_method = "agent_token"
        return await call_next(request)

    app.include_router(operations_api.router)
    monkeypatch.setattr(operations_api, "broadcast_device_lock_update", AsyncMock())
    monkeypatch.setattr(operations_api.security_audit_logger, "log_event", Mock())
    with TestClient(app) as test_client:
        yield test_client


def test_force_release_preserves_elevation_gate(client, monkeypatch):
    release = Mock(return_value=(True, "已释放"))
    monkeypatch.setattr(operations_api, "force_release_device_lock", release)
    response = client.post("/api/devices/force-release", json={"device_id": "ABC"})
    assert response.status_code == 403
    release.assert_not_called()


def test_failed_release_returns_409_and_actionable_message(client, monkeypatch):
    monkeypatch.setattr(operations_api, "force_release_device_lock", Mock(return_value=(False, "请先取消对应任务或预约")))
    response = client.post("/api/devices/force-release", json={"device_id": "ABC"}, headers={"X-Test-Elevated": "1"})
    assert response.status_code == 409
    assert response.json()["code"] == "STATE_CONFLICT"
    assert "请先取消" in response.json()["error"]
    assert response.json()["results"][0]["success"] is False


def test_bearer_cannot_inherit_human_elevation(client, monkeypatch):
    release = Mock(return_value=(True, "已释放"))
    monkeypatch.setattr(operations_api, "force_release_device_lock", release)
    response = client.post("/api/devices/force-release", json={"device_id": "ABC"},
                           headers={"X-Test-Elevated": "1", "Authorization": "Bearer test"})
    assert response.status_code == 403
    release.assert_not_called()


def test_success_returns_existing_result_envelope(client, monkeypatch):
    monkeypatch.setattr(operations_api, "force_release_device_lock", Mock(return_value=(True, "原回放及端口锁已释放")))
    response = client.post("/api/devices/force-release", json={"device_id": "ABC"}, headers={"X-Test-Elevated": "1"})
    assert response.status_code == 200 and response.json()["success"] is True
    assert "端口锁" in response.json()["results"][0]["message"]


def test_wrapper_routes_only_local_device_action_to_cluster(monkeypatch):
    from features.devices import claim_release

    locks = DeviceLockManager(local_worker_id="local")
    locks.lock_device("ABC", "alice", source_type="cluster-device-action", source_id="operation:aoa")
    release = Mock(return_value=(True, "已释放"))
    service = SimpleNamespace(config=SimpleNamespace(local_worker_id="local"),
                              repository=SimpleNamespace(force_release_local_aoa_operation=release))
    monkeypatch.setattr(claim_release, "device_lock_manager", locks)
    monkeypatch.setattr(claim_release, "get_cluster_service", lambda: service)
    assert force_release_device_lock("local:ABC")[0]
    release.assert_called_once_with("local:ABC", "local")


def test_wrapper_never_falls_back_to_force_unlock_when_cluster_is_unavailable(monkeypatch):
    from features.devices import claim_release

    locks = DeviceLockManager(local_worker_id="local")
    locks.lock_device("ABC", "alice", source_type="cluster-device-action", source_id="operation:aoa")
    monkeypatch.setattr(claim_release, "device_lock_manager", locks)
    monkeypatch.setattr(claim_release, "get_cluster_service", Mock(side_effect=RuntimeError("not configured")))
    assert not force_release_device_lock("ABC")[0]
    assert locks.get_lock_status("ABC") is not None
