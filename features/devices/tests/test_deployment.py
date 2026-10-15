from __future__ import annotations

import contextlib
import hashlib
import io
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from features.auth import AuthService, CurrentUser
from features.cluster import ClusterRepository
from features.devices import deployment_api as api
from features.devices import deployment_execution as execution
from features.devices import deployment_service as service
from features.devices.deployment_store import DeploymentStore
from foundation.command_result import CommandResult
from foundation.error_model import ApiError, register_api_error_handler


def apk():
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("AndroidManifest.xml", b"fixture-manifest")
    return output.getvalue()


@pytest.fixture
def environment(tmp_path, monkeypatch):
    repository = MagicMock()
    repository.db_path = tmp_path / "cluster.sqlite3"
    repository.get_worker.return_value = {"id": "worker", "status": "online"}
    repository.list_devices.return_value = [{"id": "worker:SERIAL", "state": "available"}]
    repository.claims.list_active.return_value = []
    repository.claims.active_claim.return_value = None
    cluster = SimpleNamespace(repository=repository, config=SimpleNamespace(local_worker_id="worker"))
    monkeypatch.setattr(service, "get_cluster_service", lambda: cluster)
    monkeypatch.setattr(api, "get_cluster_service", lambda: cluster)
    auth = AuthService(tmp_path / "auth.sqlite3")
    auth.initialize()
    monkeypatch.setattr(service, "auth_service", auth)
    monkeypatch.setattr(service, "ssh_config", lambda _worker: {})
    monkeypatch.setattr(service, "ensure_agent_worker_allowed", lambda *_args: None)
    monkeypatch.setattr(service, "ensure_agent_device_allowed", lambda *_args: None)
    app = FastAPI()
    register_api_error_handler(app)

    @app.middleware("http")
    async def principal(request, call_next):
        method = request.headers.get("x-test-principal", "agent_token")
        owner = request.headers.get("x-test-owner", "owner")
        scopes = frozenset(request.headers.get("x-test-scopes", "devices.read,devices.use_leased").split(","))
        request.state.current_user = CurrentUser(
            id="agent:one" if method != "session" else owner, username=owner,
            role=request.headers.get("x-test-role", "user") if method == "session" else "agent_service",
            extra_permissions=frozenset() if method == "session" else scopes, resource_owner_id=owner,
        )
        request.state.auth_method = method
        return await call_next(request)

    app.include_router(api.router)
    with TestClient(app) as client:
        yield client, repository, auth


def upload(client):
    response = client.post("/api/devices/deployments", data={"worker_id": "worker", "device": "SERIAL"},
                           files={"apk_file": ('unsafe<script>.apk', apk(), "application/octet-stream")})
    assert response.status_code == 200, response.text
    return response.json()["deployment"]


def test_upload_is_pending_and_has_no_device_side_effect(environment):
    client, repository, _auth = environment
    item = upload(client)
    assert item["status"] == "pending"
    assert item["approval_url"].endswith(item["id"] + "/review")
    assert "token" not in item
    repository.acquire_device_operation_claim.assert_not_called()
    assert client.get(item["approval_url"]).status_code == 200


def test_agent_cannot_self_approve_and_other_owner_cannot_read(environment):
    client, repository, _auth = environment
    item = upload(client)
    endpoint = "/api/devices/deployments/" + item["id"]
    assert client.post(endpoint + "/approve").status_code == 403
    assert client.get(endpoint, headers={"x-test-owner": "other"}).status_code == 404
    repository.acquire_device_operation_claim.assert_not_called()


def test_human_operator_can_read_own_review_without_agent_read_scope(environment):
    client, repository, _auth = environment
    item = upload(client)
    endpoint = "/api/devices/deployments/" + item["id"]
    human = {"x-test-principal": "session"}
    assert client.get(endpoint, headers=human).status_code == 200
    assert client.get(endpoint + "/review", headers=human).status_code == 200
    assert client.get(endpoint, headers={**human, "x-test-owner": "other"}).status_code == 404
    assert client.get(endpoint, headers={**human, "x-test-role": "worker_service"}).status_code == 403
    assert client.get(endpoint, headers={"x-test-scopes": "devices.use_leased"}).status_code == 403
    repository.acquire_device_operation_claim.assert_not_called()


def test_human_approval_runs_exact_artifact_once_and_returns_no_token(environment):
    client, repository, _auth = environment
    item = upload(client)
    endpoint = "/api/devices/deployments/" + item["id"]
    with patch.object(service, "execute") as execute:
        result = client.post(endpoint + "/approve", headers={"x-test-principal": "session"})
        assert result.status_code == 200, result.text
        assert "token" not in result.text
        payload, source = execute.call_args.args
        assert payload["sha256"] == item["sha256"]
        assert (payload["worker_id"], payload["serial"], payload["replace_existing"]) == ("worker", "SERIAL", True)
        assert source == "operation:apk-deploy:" + item["id"]
        assert client.post(endpoint + "/approve", headers={"x-test-principal": "session"}).status_code == 409
        assert execute.call_count == 1
    assert repository.acquire_device_operation_claim.call_args.kwargs["owner_id"] == "owner"


def test_background_ssh_config_stays_private_and_uses_approving_owner(environment):
    client, _repository, _auth = environment
    with patch.object(service, "ssh_config", return_value={"password": "example-ssh-password"}):
        item = upload(client)
        endpoint = "/api/devices/deployments/" + item["id"]
        with patch.object(service, "execute") as execute:
            response = client.post(endpoint + "/approve", headers={"x-test-principal": "session"})
            assert response.status_code == 200
            assert execute.call_args.args[0]["_ssh_config"] == {"password": "example-ssh-password"}
            public = client.get(endpoint)
            assert "example-ssh-password" not in public.text
            assert "_ssh_config" not in public.json()["deployment"]


def test_tampered_artifact_rejected_before_approval_consumption(environment):
    client, repository, _auth = environment
    item = upload(client)
    path = service.store().root / item["id"] / "artifact.apk"
    path.chmod(0o600)
    path.write_bytes(b"different artifact")
    response = client.post("/api/devices/deployments/" + item["id"] + "/approve",
                           headers={"x-test-principal": "session"})
    assert response.status_code == 409
    repository.acquire_device_operation_claim.assert_not_called()


def test_scope_and_worker_inventory_fail_closed(environment):
    client, repository, _auth = environment
    response = client.post("/api/devices/deployments", headers={"x-test-scopes": "devices.read"},
                           data={"worker_id": "worker", "device": "SERIAL"}, files={"apk_file": ("app.apk", apk())})
    assert response.status_code == 403
    repository.list_devices.return_value = []
    response = client.post("/api/devices/deployments", data={"worker_id": "worker", "device": "SERIAL"},
                           files={"apk_file": ("app.apk", apk())})
    assert response.status_code == 409


def test_approval_store_is_process_safe_and_single_use(tmp_path):
    store = DeploymentStore(tmp_path)
    store.create({"id": "a" * 32, "owner_id": "owner", "expires_at": time.time() + 300})

    def start(_index):
        try:
            DeploymentStore(tmp_path).start("a" * 32, "owner", "owner")
            return True
        except ApiError:
            return False

    with ThreadPoolExecutor(4) as pool:
        assert sum(pool.map(start, range(4))) == 1


def test_expired_request_cannot_be_started(tmp_path):
    store = DeploymentStore(tmp_path)
    store.create({"id": "a" * 32, "owner_id": "owner", "expires_at": time.time() - 1})
    assert store.get("a" * 32, "owner")["status"] == "expired"
    with pytest.raises(ApiError):
        store.start("a" * 32, "owner", "owner")


@pytest.mark.parametrize("field,value", [("table", 'favorites;DELETE'), ("columns", ['x);DROP TABLE a']),
                                        ("database", "../a.db"), ("limit", 501), ("package", "com.app;id")])
def test_database_query_rejects_injection_and_unbounded_input(environment, field, value):
    client, _repository, _auth = environment
    body = {"worker_id": "worker", "device": "SERIAL", "package": "com.example.app", "database": "app.db", **{field: value}}
    with patch.object(api, "query_database") as query:
        result = client.post("/api/devices/db/query", json=body)
        assert result.status_code in (400, 422)
        query.assert_not_called()


def test_database_select_uses_readonly_and_reports_pagination():
    body = api.DatabaseQuery(worker_id="worker", device="SERIAL", package="com.example.app",
                             database="app.db", table="favorites", columns=["cellX", "cellY"], limit=1)
    with patch.object(execution.ssh_executor, "run", return_value=CommandResult(stdout="cellX,cellY\n1,2\n3,4\n", stderr="", code=0)) as run:
        result = execution.query_database(object(), "SERIAL", body)
    assert result["rows"] == [{"cellX": "1", "cellY": "2"}]
    assert result["has_more"] is True
    command = run.call_args.args[1]
    assert "-readonly" in command and "run-as" in command and "LIMIT 2" in command


def test_missing_sqlite_is_dependency_failure():
    with patch.object(execution.ssh_executor, "run", return_value=CommandResult(
        stdout="", stderr="sqlite3: not found", code=127,
    )), pytest.raises(ApiError) as result:
        execution.run(object(), ["adb", "shell", "sqlite3"])
    assert result.value.status_code == 503


def test_system_app_query_reuses_existing_access_without_enabling_root():
    body = api.DatabaseQuery(worker_id="worker", device="SERIAL", package="com.android.launcher3",
                             database="launcher.db", table="favorites", columns=["cellX", "cellY"])
    with patch.object(execution, "run", side_effect=[ApiError.forbidden("not debuggable"), "cellX,cellY\n1,2\n"]) as run:
        result = execution.query_database(object(), "SERIAL", body)
    assert result["access_mode"] == "shell"
    assert result["read_only"] and result["user_id"] == 0
    command = run.call_args.args[1][-1]
    assert "sqlite3 -readonly" in command
    assert "/data/user/0/com.android.launcher3/databases/launcher.db" in command
    assert "su " not in command and "root" not in command and "chmod" not in command


def test_database_output_cannot_be_silently_truncated_by_mcp():
    body = api.DatabaseQuery(worker_id="worker", device="SERIAL", package="com.example.app", database="app.db")
    with patch.object(execution, "run", return_value="x" * (512 * 1024 + 1)), pytest.raises(ApiError) as caught:
        execution.query_database(object(), "SERIAL", body)
    assert caught.value.status_code == 422


@pytest.mark.parametrize("owner,source_type", [("owner", "cluster-job"), ("other", "cluster-reservation")])
def test_deployment_never_borrows_a_test_or_another_owner_claim(environment, tmp_path, owner, source_type):
    client, repository, _auth = environment
    real = ClusterRepository(tmp_path / "real-cluster.sqlite3")
    repository.claims = real.claims
    repository.acquire_device_operation_claim = real.acquire_device_operation_claim
    real.acquire_device_operation_claim("worker", ["worker:SERIAL"], owner_id=owner,
                                        source_type=source_type, source_id="held", ttl_seconds=3600)
    item = upload(client)
    with patch.object(service, "execute") as execute:
        response = client.post("/api/devices/deployments/" + item["id"] + "/approve",
                               headers={"x-test-principal": "session"})
        assert response.status_code == 409
        execute.assert_not_called()
    assert real.claims.active_claim("worker:SERIAL")["source_id"] == "held"
    assert service.store().get(item["id"], "owner")["status"] == "pending"


@pytest.mark.parametrize("transfer_at", ["unchanged", "before_execute", "after_push"])
def test_borrowed_reservation_is_fenced_until_install(environment, tmp_path, transfer_at):
    client, repository, _auth = environment
    real = ClusterRepository(tmp_path / "real-cluster.sqlite3")
    repository.claims = real.claims
    repository.claim_fencing_tokens = real.claim_fencing_tokens
    real.acquire_device_operation_claim("worker", ["worker:SERIAL"], owner_id="owner",
                                        source_type="cluster-reservation", source_id="reserved", ttl_seconds=3600)
    item = upload(client)
    with patch.object(service, "execute") as queued:
        response = client.post("/api/devices/deployments/" + item["id"] + "/approve",
                               headers={"x-test-principal": "session"})
        assert response.status_code == 200
        payload, source = queued.call_args.args
    assert source == ""

    def transfer():
        before = real.claims.active_claim("worker:SERIAL")
        assert real.claims.transfer("reserved", "job:next", source_type="cluster-job", ttl_seconds=3600) == 1
        after = real.claims.active_claim("worker:SERIAL")
        assert (after["id"], after["generation"]) == (before["id"], before["generation"])

    def command(_ssh, args, *_rest):
        if args[:4] == ["adb", "-s", "SERIAL", "push"] and transfer_at == "after_push":
            transfer()
        if "sha256sum" in " ".join(args):
            return item["sha256"]
        return "Success\n" if "pm install" in " ".join(args) else ""

    if transfer_at == "before_execute":
        transfer()
    with patch.object(service, "connection", return_value=contextlib.nullcontext(MagicMock())), patch.object(
        execution, "run", side_effect=command,
    ) as run:
        service.execute(payload, source)
    result = service.store().get(item["id"], "owner")
    installed = any("pm install" in " ".join(call.args[1]) for call in run.call_args_list)
    assert installed is (transfer_at == "unchanged")
    assert result["status"] == ("completed" if installed else "failed")
    if not installed:
        assert result["result"]["code"] == "STATE_CONFLICT"
    assert real.claims.active_claim("worker:SERIAL")["source_id"] == ("reserved" if installed else "job:next")


def test_transmission_checksum_failure_prevents_install(tmp_path):
    artifact = tmp_path / "artifact.apk"
    artifact.write_bytes(apk())
    payload = {"id": "a" * 32, "serial": "SERIAL", "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
               "replace_existing": True}
    ssh = MagicMock()
    with patch.object(execution, "run", side_effect=lambda _ssh, args, *rest: "0" * 64 if args[0] == "sha256sum" else "") as run, pytest.raises(ApiError):
        execution.install(ssh, payload, artifact)
    commands = [call.args[1] for call in run.call_args_list]
    assert not any("push" in command for command in commands)
    assert not any("pm install" in " ".join(command) for command in commands)


def test_install_failure_even_with_exit_zero_is_not_success(tmp_path):
    artifact = tmp_path / "artifact.apk"
    artifact.write_bytes(apk())
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    payload = {"id": "a" * 32, "serial": "SERIAL", "sha256": digest, "replace_existing": True}

    def command(_ssh, args, *rest):
        if args[0] == "sha256sum" or "sha256sum" in " ".join(args):
            return digest
        if "pm install" in " ".join(args):
            return "Failure [INSTALL_FAILED_UPDATE_INCOMPATIBLE]"
        return ""

    with patch.object(execution, "run", side_effect=command), pytest.raises(ApiError) as caught:
        execution.install(MagicMock(), payload, artifact)
    assert caught.value.status_code == 502


def test_interrupted_deployment_eventually_has_actionable_terminal_status(tmp_path):
    store = DeploymentStore(tmp_path)
    store.create({"id": "a" * 32, "owner_id": "owner", "expires_at": time.time() + 300})
    store.start("a" * 32, "owner", "owner")
    with store.connect() as conn:
        conn.execute("UPDATE deployments SET expires_at=?", (time.time() - 1,))
    result = store.get("a" * 32, "owner")
    assert result["status"] == "failed"
    assert result["result"]["code"] == "DEPENDENCY_TIMEOUT"
