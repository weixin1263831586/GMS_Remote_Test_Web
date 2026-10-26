"""Export and derived-artifact routes enforce scopes and target ACLs."""

from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from features.auth import CurrentUser
from features.cluster import api
from features.cluster.repository import ClusterRepository
from features.cluster.service import ClusterService


@pytest.fixture
def export_client(tmp_path, monkeypatch):
    monkeypatch.setenv("GMS_AUTH_REQUIRED", "true")
    repo = ClusterRepository(tmp_path / "cluster.sqlite3", claim_db_path=tmp_path / "claims.sqlite3")
    repo.register_worker({"worker_id": "worker", "name": "worker", "hostname": "worker",
                          "address": "127.0.0.1", "agent_version": "1", "max_jobs": 2})
    repo.heartbeat("worker", {"devices": [{"serial": "ABC", "state": "available"}],
                              "suites": [], "running_jobs": []})
    monkeypatch.setattr(api, "cluster_service", ClusterService(repo))
    execute = AsyncMock(return_value={"raw_output": ""})
    monkeypatch.setattr(api, "_run_worker_command", execute)
    app = FastAPI()

    @app.middleware("http")
    async def identity(request, call_next):
        request.state.auth_method = "agent_token"
        request.state.current_user = CurrentUser(
            id="agent:test", username="test", role="agent_service", resource_owner_id="owner",
            extra_permissions=frozenset(filter(None, request.headers.get("X-Scopes", "").split(","))),
        )
        request.state.agent_token_record = {
            "allowed_workers": request.headers.get("X-Workers", "worker"),
            "allowed_devices": request.headers.get("X-Devices", "ABC"),
        }
        return await call_next(request)

    app.include_router(api.router)
    with TestClient(app) as client:
        yield client, repo, execute


EXPORTS = [
    ("/api/cluster/devices/export", {"worker_id": "worker", "device_id": "ABC",
                                    "path": "/system/framework/framework.jar"}, "devices.read"),
    ("/api/cluster/suites/export", {"worker_id": "worker", "suite_path": "/suites/cts",
                                   "path": "results"}, "suites.read"),
    ("/api/cluster/suites/results", {"worker_id": "worker", "suite_path": "/suites/cts"}, "suites.read"),
]


@pytest.mark.parametrize("path,params,scope", EXPORTS)
@pytest.mark.parametrize("denial", ["scope", "worker"])
def test_export_rejects_missing_scope_and_forbidden_worker(export_client, path, params, scope, denial):
    client, repo, execute = export_client
    headers = {"X-Scopes": scope if denial == "worker" else "", "X-Workers": "other" if denial == "worker" else "worker"}
    response = client.post(path, params=params, headers=headers)
    assert response.status_code == 403, response.text
    assert repo.claims.list_active() == []
    execute.assert_not_called()
    with repo.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM cluster_commands").fetchone()[0] == 0


@pytest.mark.parametrize("device", ["ABC", "worker:ABC"])
def test_device_export_checks_acl_before_claiming(export_client, device):
    client, repo, _ = export_client
    params = {**EXPORTS[0][1], "device_id": device}
    response = client.post(EXPORTS[0][0], params=params,
                           headers={"X-Scopes": "devices.read", "X-Devices": "OTHER"})
    assert response.status_code == 403
    assert repo.claims.list_active() == []


@pytest.mark.parametrize("allowed", ["ABC", "worker:ABC"])
def test_device_export_permits_canonical_and_serial_acl(export_client, allowed):
    client, repo, _ = export_client
    response = client.post(EXPORTS[0][0], params=EXPORTS[0][1],
                           headers={"X-Scopes": "devices.read", "X-Devices": allowed})
    assert response.status_code == 200, response.text
    command = repo.get_command(response.json()["command_id"])
    assert command["payload"]["devices"] == ["worker:ABC"]
    assert command["payload"]["owner_id"] == "owner"


def test_less_privileged_token_cannot_read_owners_existing_transfer(export_client):
    client, repo, _ = export_client
    transfer = repo.create_transfer("worker", owner_id="owner", transfer_type="device_export",
                                    metadata={"device_id": "worker:ABC"})
    path = f"/api/cluster/transfers/{transfer['id']}"
    assert client.get(path).status_code == 403
    assert client.get(path, headers={"X-Scopes": "devices.read", "X-Devices": "OTHER"}).status_code == 403
    assert client.get(path, headers={"X-Scopes": "devices.read"}).status_code == 200


@pytest.mark.parametrize("operation,scope", [("apk-analysis", "apk.analyze_own"), ("report-analysis", "reports.read")])
def test_derived_analysis_requires_its_own_scope(export_client, operation, scope):
    client, repo, _ = export_client
    transfer = repo.create_transfer("worker", owner_id="owner", transfer_type="suite_export")
    response = client.post(f"/api/cluster/transfers/{transfer['id']}/{operation}",
                           headers={"X-Scopes": "suites.read"})
    assert response.status_code == 403
