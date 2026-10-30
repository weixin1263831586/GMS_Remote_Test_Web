from __future__ import annotations

import contextlib
import io
import json
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from features.auth import AuthService, CurrentUser
from features.cluster import ClusterRepository
from features.devices import (
    debug_api,
    debug_execution,
    debug_identity,
    debug_service,
    deployment_api,
    deployment_service,
)
from features.devices.debug_store import DebugStore
from foundation.command_result import CommandResult
from foundation.error_model import ApiError, register_api_error_handler


IDENTITY = {"package": "com.example.app", "signers": ["a" * 64], "version_code": "1", "version_name": ""}


@pytest.fixture
def environment(tmp_path, monkeypatch):
    repository = MagicMock()
    repository.db_path = tmp_path / "cluster.sqlite3"
    repository.get_worker.return_value = {"id": "worker", "status": "online"}
    repository.list_devices.return_value = [{"id": "worker:SERIAL", "state": "available"}]
    real = ClusterRepository(repository.db_path)
    repository.claims = real.claims
    repository.acquire_device_operation_claim = real.acquire_device_operation_claim
    repository.claim_fencing_tokens = real.claim_fencing_tokens
    cluster = SimpleNamespace(repository=repository, config=SimpleNamespace(local_worker_id="worker"))
    monkeypatch.setattr(deployment_service, "get_cluster_service", lambda: cluster)
    monkeypatch.setattr(debug_service, "get_cluster_service", lambda: cluster)
    auth = AuthService(tmp_path / "auth.sqlite3")
    auth.initialize()
    owner = auth.create_user("debugger", "test-fixture-password", role="admin")
    token = auth.create_agent_token(name="test", owner=owner, scopes=["devices.read", "devices.use_leased"])
    agent, _ = auth.get_agent_token_principal(token["token"])
    monkeypatch.setattr(deployment_service, "auth_service", auth)
    monkeypatch.setattr(deployment_service, "ssh_config", lambda _worker: {})
    monkeypatch.setattr(deployment_service, "connection", lambda *_args: contextlib.nullcontext(object()))
    monkeypatch.setattr(deployment_service, "ensure_agent_worker_allowed", lambda *_args: None)
    monkeypatch.setattr(deployment_service, "ensure_agent_device_allowed", lambda *_args: None)
    monkeypatch.setattr(debug_service, "inspect_apk", lambda *_args: IDENTITY.copy())
    monkeypatch.setattr(deployment_service, "inspect_apk", lambda *_args: IDENTITY.copy())
    monkeypatch.setattr(deployment_service, "installed_identity", lambda *_args: None)
    monkeypatch.setattr(debug_service, "require_installed", lambda *_args, **_kwargs: IDENTITY.copy())
    app = FastAPI()
    register_api_error_handler(app)

    @app.middleware("http")
    async def principal(request, call_next):
        method = request.headers.get("x-test-principal", "agent_token")
        scopes = frozenset(request.headers.get("x-test-scopes", "devices.read,devices.use_leased").split(","))
        request.state.current_user = CurrentUser(
            id=owner.id if method == "session" else request.headers.get("x-test-actor", agent.id),
            username=owner.username,
            role=request.headers.get("x-test-role", "user") if method == "session" else "agent_service",
            extra_permissions=frozenset() if method == "session" else scopes,
            resource_owner_id=request.headers.get("x-test-owner", owner.id),
        )
        request.state.auth_method = method
        return await call_next(request)

    app.include_router(deployment_api.router)
    app.include_router(debug_api.router)
    with TestClient(app) as client:
        yield client, repository, auth, owner, token, agent


def request_session(client, **values):
    # Artifact structure is checked by deployment.prepare; signature extraction
    # is replaced by the fixture, and tested independently below.
    import zipfile
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("AndroidManifest.xml", b"fixture")
    response = client.post("/api/devices/debug-sessions", data={
        "worker_id": "worker", "device": "SERIAL", "package": "com.example.app", **values,
    }, files={"apk_file": ("app.apk", output.getvalue())})
    assert response.status_code == 200, response.text
    return response.json()["session"]


def approve(client, session):
    with patch.object(debug_service, "execute_install"):
        response = client.post("/api/devices/debug-sessions/" + session["id"] + "/approve",
                               headers={"x-test-principal": "session"})
    assert response.status_code == 200, response.text
    active = response.json()["session"]
    if active["busy"]:
        deployment_service.store().finish(active["baseline_deployment_id"], "completed", {"installed": True})
        debug_service.finish(active, active["busy"], "completed", {"installed": True})
    return debug_service.store().get(active["id"], active["owner_id"])


def test_request_is_pending_has_no_device_effect_and_agent_cannot_self_approve(environment):
    client, *_ = environment
    session = request_session(client)
    assert session["status"] == "pending" and session["duration_minutes"] == 60
    assert "_lease_tokens" not in session
    assert client.get(session["approval_url"]).status_code == 200
    assert client.post("/api/devices/debug-sessions/" + session["id"] + "/approve").status_code == 403


def request_installed(client, **values):
    return client.post("/api/devices/debug-sessions", data={
        "worker_id": "worker", "device": "worker:SERIAL", "package": "com.example.app", "installed": "true", **values,
    })


def test_installed_app_session_verifies_identity_without_upload_or_install(environment, monkeypatch):
    client, repository, *_ = environment
    observed = MagicMock(return_value={**IDENTITY, "sha256": "b" * 64})
    monkeypatch.setattr(debug_identity, "installed_identity", observed)
    response = request_installed(client)
    assert response.status_code == 200, response.text
    session = response.json()["session"]
    assert session["status"] == "pending" and session["baseline_source"] == "installed"
    assert session["serial"] == "SERIAL" and session["baseline_deployment_id"] == ""
    assert session["signers"] == IDENTITY["signers"] and session["baseline_sha256"] == "b" * 64
    assert "install" not in session["operations"]
    observed.assert_called_once()
    assert repository.claims.active_claim("worker:SERIAL") is None
    assert not list(deployment_service.store().root.glob("*/artifact.apk"))
    endpoint = "/api/devices/debug-sessions/" + session["id"]
    assert client.post(endpoint + "/approve").status_code == 403
    with patch.object(debug_service, "require_installed", return_value=IDENTITY) as verify, \
            patch.object(debug_service, "start_install") as install:
        active = approve(client, session)
    verify.assert_called_once()
    install.assert_not_called()
    assert active["status"] == "active" and not active["busy"]
    assert repository.claims.active_claim("worker:SERIAL")
    assert client.get(endpoint).json()["session"]["usable"]
    with patch.object(debug_execution, "perform", return_value={"ok": True}):
        assert client.post(endpoint + "/actions", json={"action": "launch"}).status_code == 200
    response = client.post("/api/devices/deployments", data={
        "worker_id": "worker", "device": "SERIAL", "debug_session_id": session["id"],
    }, files={"apk_file": ("new.apk", b"unused")})
    assert response.status_code == 403
    assert client.post(endpoint + "/revoke").status_code == 200
    assert repository.claims.active_claim("worker:SERIAL") is None
    assert client.post(endpoint + "/actions", json={"action": "launch"}).status_code == 403


@pytest.mark.parametrize("values, status", [
    ({"operations": "install,launch"}, 422), ({"operations": "unknown"}, 422),
    ({"device": "MISSING"}, 409), ({"worker_id": "other"}, 409),
])
def test_invalid_installed_request_does_not_read_device(environment, monkeypatch, values, status):
    client, *_ = environment
    observe = MagicMock()
    monkeypatch.setattr(debug_identity, "installed_identity", observe)
    response = request_installed(client, **values)
    assert response.status_code == status, response.text
    observe.assert_not_called()


def test_installed_request_rejects_uploaded_apk_and_missing_source(environment):
    client, *_ = environment
    values = {"worker_id": "worker", "device": "SERIAL", "package": "com.example.app"}
    assert client.post("/api/devices/debug-sessions", data=values).status_code == 422
    response = client.post("/api/devices/debug-sessions", data={**values, "installed": "true"},
                           files={"apk_file": ("app.apk", b"unused")})
    assert response.status_code == 422


@pytest.mark.parametrize("failure", ["absent", "ssh", "acl"])
def test_installed_identity_failures_are_semantic(environment, monkeypatch, failure):
    client, *_ = environment
    observe = MagicMock(return_value=None)
    if failure == "ssh":
        observe.side_effect = ApiError.upstream_failure("Worker SSH failed", service="ssh")
    elif failure == "acl":
        monkeypatch.setattr(debug_service, "check_identity", MagicMock(side_effect=ApiError.forbidden("ACL denied")))
    monkeypatch.setattr(debug_identity, "installed_identity", observe)
    response = request_installed(client)
    assert response.status_code == {"absent": 404, "ssh": 502, "acl": 403}[failure], response.text
    if failure == "acl":
        observe.assert_not_called()


def test_installed_signer_change_before_approval_releases_claim(environment, monkeypatch):
    client, repository, *_ = environment
    monkeypatch.setattr(debug_identity, "installed_identity", lambda *_args: {**IDENTITY, "sha256": "b" * 64})
    session = request_installed(client).json()["session"]
    monkeypatch.setattr(debug_service, "require_installed", MagicMock(side_effect=ApiError.forbidden("signer changed")))
    response = client.post("/api/devices/debug-sessions/" + session["id"] + "/approve",
                           headers={"x-test-principal": "session"})
    assert response.status_code == 403
    assert debug_service.store().get(session["id"], session["owner_id"])["status"] == "pending"
    assert repository.claims.active_claim("worker:SERIAL") is None


def test_debug_install_start_failure_finishes_busy_operation_and_releases_resources(environment, monkeypatch):
    _client, cluster_repository, _auth, *_ = environment
    session = request_session(_client, operations="stop")
    active = approve(_client, session)
    operation = debug_service.store().begin(
        active["id"], active["owner_id"], active["actor_id"], "stop", {"test": True},
    )
    deployment_id = uuid.uuid4().hex
    repository = deployment_service.store()
    repository.create({"id": deployment_id, "owner_id": active["owner_id"], "expires_at": time.time() + 300,
                       "worker_id": active["worker_id"], "serial": active["serial"],
                       "package": active["package"], "sha256": active["baseline_sha256"]})
    repository.start(deployment_id, active["owner_id"], active["owner_id"])

    def fail_start(*_args, **_kwargs):
        raise RuntimeError("injected begin_execution failure")

    monkeypatch.setattr(deployment_service, "store", lambda: repository)
    monkeypatch.setattr(repository, "begin_execution", fail_start)
    payload = {"id": deployment_id, "owner_id": active["owner_id"], "worker_id": active["worker_id"],
               "serial": active["serial"], "package": active["package"], "sha256": active["baseline_sha256"],
               "_debug_session": active, "_debug_operation": operation, "_ssh_config": {}}
    debug_service.execute_install(payload)

    assert repository.get(deployment_id, active["owner_id"])["status"] == "failed"
    finished = debug_service.store().get(active["id"], active["owner_id"])
    assert finished["status"] == "revoked" and finished["busy"] == ""
    history = debug_service.store().history(active["id"], active["owner_id"])
    assert history[0]["id"] == operation and history[0]["status"] == "failed"
    assert cluster_repository.claims.active_claim("worker:SERIAL") is None


@pytest.mark.parametrize("role", ["user", "device_operator"])
def test_human_operator_can_review_own_debug_session_without_agent_read_scope(environment, role):
    client, *_ = environment
    session = request_session(client)
    endpoint = "/api/devices/debug-sessions/" + session["id"]
    human = {"x-test-principal": "session", "x-test-role": role}
    assert client.get(endpoint, headers=human).status_code == 200
    assert client.get(endpoint + "/review", headers=human).status_code == 200
    assert client.get(endpoint, headers={**human, "x-test-owner": "other"}).status_code == 404
    assert client.get(endpoint, headers={**human, "x-test-role": "worker_service"}).status_code == 403
    assert client.get(endpoint, headers={"x-test-scopes": "devices.use_leased"}).status_code == 403


@pytest.mark.parametrize("role", ["user", "device_operator"])
def test_human_operator_roles_can_approve_own_debug_session(environment, role):
    client, *_ = environment
    session = request_session(client)
    with patch.object(debug_service, "execute_install") as queued:
        response = client.post("/api/devices/debug-sessions/" + session["id"] + "/approve",
                               headers={"x-test-principal": "session", "x-test-role": role})
    assert response.status_code == 200, response.text
    assert queued.call_count == 1


def test_approve_binds_finite_grant_and_first_install_once(environment):
    client, repository, _, owner, *_ = environment
    session = request_session(client)
    active = approve(client, session)
    assert active["status"] == "active"
    assert 3590 < active["expires_at"] - time.time() <= 3600
    assert active["package"] == IDENTITY["package"] and active["signers"] == IDENTITY["signers"]
    assert repository.claims.active_claim("worker:SERIAL")["owner_id"] == owner.id
    assert client.post("/api/devices/debug-sessions/" + session["id"] + "/approve",
                       headers={"x-test-principal": "session"}).status_code == 409
    assert len(debug_service.store().history(session["id"], owner.id)) == 1


def test_repeated_installs_need_no_new_human_approval(environment):
    client, _, _, owner, *_ = environment
    session = approve(client, request_session(client))
    for _ in range(2):
        with patch.object(debug_service, "execute_install") as execute:
            response = client.post("/api/devices/deployments", data={
                "worker_id": "worker", "device": "SERIAL", "debug_session_id": session["id"],
            }, files={"apk_file": ("new.apk", baseline_bytes())})
        assert response.status_code == 200, response.text
        item = response.json()["deployment"]
        assert item["status"] == "queued" and "approval_url" not in item
        assert "_ssh_config" not in item and "_debug_session" not in item
        execute.assert_called_once()
        work = execute.call_args.args[0]
        debug_service.finish(session, work["_debug_operation"], "completed", {"installed": True})
        deployment_service.store().finish(item["id"], "completed", {"installed": True})
    assert len(debug_service.store().history(session["id"], owner.id)) == 3


def baseline_bytes():
    import zipfile
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("AndroidManifest.xml", b"fixture")
    return output.getvalue()


@pytest.mark.parametrize("identity", [{"package": "com.other.app", "signers": ["a" * 64]},
                                     {"package": "com.example.app", "signers": ["b" * 64]}])
def test_other_package_or_signer_never_installs(environment, identity):
    client, *_ = environment
    session = approve(client, request_session(client))
    with patch.object(debug_service, "inspect_apk", return_value=identity), patch.object(debug_service, "execute_install") as execute:
        response = client.post("/api/devices/deployments", data={
            "worker_id": "worker", "device": "SERIAL", "debug_session_id": session["id"],
        }, files={"apk_file": ("new.apk", baseline_bytes())})
    assert response.status_code == 403
    execute.assert_not_called()
    assert debug_service.store().get(session["id"], session["owner_id"])["busy"] == ""


@pytest.mark.parametrize("change", ["expired", "revoked", "other_agent", "other_owner", "token_revoked", "lost_lease",
                                   "scope_removed", "worker_acl", "device_acl", "owner_disabled", "worker_offline"])
def test_grants_fail_closed_before_device_execution(environment, change):
    client, repository, auth, _, token, _ = environment
    session = approve(client, request_session(client))
    headers = {}
    if change == "expired":
        with debug_service.store().connect() as conn:
            conn.execute("UPDATE debug_sessions SET expires_at=? WHERE id=?", (time.time() - 1, session["id"]))
    elif change == "revoked":
        assert client.post("/api/devices/debug-sessions/" + session["id"] + "/revoke").status_code == 200
    elif change == "other_agent":
        headers["x-test-actor"] = "agent:other"
    elif change == "other_owner":
        headers["x-test-owner"] = "other"
    elif change == "token_revoked":
        auth.revoke_agent_token(token["id"])
    elif change == "lost_lease":
        repository.claims.release(session["_claim_source"])
    elif change == "worker_offline":
        repository.get_worker.return_value = {"status": "offline"}
    elif change == "owner_disabled":
        with auth._connect() as conn:
            conn.execute("UPDATE platform_users SET disabled=1 WHERE id=?", (session["owner_id"],))
    else:
        field, value = {"scope_removed": ("scopes", "devices.read"), "worker_acl": ("allowed_workers", "other"),
                        "device_acl": ("allowed_devices", "other")}[change]
        with auth._connect() as conn:
            # field is a fixed test-only mapping; no request data is interpolated.
            conn.execute("UPDATE platform_agent_tokens SET " + field + "=? WHERE id=?", (value, token["id"]))
    with patch.object(debug_execution, "perform") as perform:
        response = client.post("/api/devices/debug-sessions/" + session["id"] + "/actions", headers=headers,
                               json={"action": "stop"})
    assert response.status_code in {403, 404, 409, 503}, response.text
    perform.assert_not_called()


@pytest.mark.parametrize("source_type", ["cluster-job", "cluster-reservation"])
def test_approval_cannot_borrow_other_owner_or_running_job(environment, source_type):
    client, repository, _, owner, *_ = environment
    held_owner = owner.id if source_type == "cluster-job" else "other"
    session = request_session(client)
    repository.acquire_device_operation_claim("worker", ["worker:SERIAL"], owner_id=held_owner,
                                             source_type=source_type, source_id="held", ttl_seconds=3600)
    with patch.object(debug_service, "execute_install") as execute:
        response = client.post("/api/devices/debug-sessions/" + session["id"] + "/approve",
                               headers={"x-test-principal": "session"})
    assert response.status_code == 409
    execute.assert_not_called()
    assert repository.claims.active_claim("worker:SERIAL")["source_id"] == "held"
    assert debug_service.store().get(session["id"], owner.id)["status"] == "pending"


def test_own_explicit_reservation_is_borrowed_and_never_released(environment):
    client, repository, _, owner, *_ = environment
    repository.acquire_device_operation_claim("worker", ["worker:SERIAL"], owner_id=owner.id,
                                             source_type="cluster-reservation", source_id="reserved", ttl_seconds=3600)
    session = approve(client, request_session(client))
    assert session["_claim_source"] == ""
    assert client.post("/api/devices/debug-sessions/" + session["id"] + "/revoke").status_code == 200
    assert repository.claims.active_claim("worker:SERIAL")["source_id"] == "reserved"


@pytest.mark.parametrize("source_id,source_type", [
    ("job:next", "cluster-job"), ("reservation:next", "cluster-reservation"), ("reserved", "cluster-job"),
])
def test_transferred_reservation_blocks_debug_actions_and_installs(environment, source_id, source_type):
    client, repository, _, owner, *_ = environment
    repository.acquire_device_operation_claim("worker", ["worker:SERIAL"], owner_id=owner.id,
                                             source_type="cluster-reservation", source_id="reserved", ttl_seconds=3600)
    session = approve(client, request_session(client))
    before = repository.claims.active_claim("worker:SERIAL")
    assert repository.claims.transfer("reserved", source_id, source_type=source_type,
                                      ttl_seconds=3600, owner_id=owner.id) == 1
    after = repository.claims.active_claim("worker:SERIAL")
    assert (after["id"], after["generation"]) == (before["id"], before["generation"])
    repository.list_devices.return_value = [{"id": "worker:SERIAL", "state": "leased"}]
    endpoint = "/api/devices/debug-sessions/" + session["id"]
    with patch.object(debug_execution, "perform", return_value={"action": "stop"}) as perform, patch.object(
        debug_service, "execute_install",
    ) as install:
        assert client.post(endpoint + "/actions", json={"action": "stop"}).status_code == 409
        response = client.post("/api/devices/deployments", data={
            "worker_id": "worker", "device": "SERIAL", "debug_session_id": session["id"],
        }, files={"apk_file": ("new.apk", baseline_bytes())})
        assert response.status_code == 409
        perform.assert_not_called()
        install.assert_not_called()
    assert client.get(endpoint).json()["session"]["usable"] is False
    assert client.post(endpoint + "/revoke").status_code == 200
    assert repository.claims.active_claim("worker:SERIAL")["source_id"] == source_id


def test_legacy_grant_without_claim_source_binding_fails_closed(environment):
    client, *_ = environment
    session = approve(client, request_session(client))
    with debug_service.store().connect() as conn:
        row = conn.execute("SELECT payload FROM debug_sessions WHERE id=?", (session["id"],)).fetchone()
        payload = json.loads(row["payload"])
        for token in payload["_lease_tokens"]:
            token.pop("source_id", None)
            token.pop("source_type", None)
        conn.execute("UPDATE debug_sessions SET payload=? WHERE id=?", (json.dumps(payload), session["id"]))
    with patch.object(debug_execution, "perform", return_value={"action": "stop"}) as perform:
        response = client.post("/api/devices/debug-sessions/" + session["id"] + "/actions", json={"action": "stop"})
    assert response.status_code == 409
    perform.assert_not_called()


def test_revocation_inside_install_fails_before_later_device_mutations(environment):
    client, *_ = environment
    session = approve(client, request_session(client))
    baseline = deployment_service.store().get(session["baseline_deployment_id"], session["owner_id"])
    # Finish the fixture's suppressed baseline, then prepare a new deployment.
    deployment_service.store().finish(baseline["id"], "completed", {})
    with patch.object(debug_service, "execute_install") as queued:
        response = client.post("/api/devices/deployments", data={
            "worker_id": "worker", "device": "SERIAL", "debug_session_id": session["id"],
        }, files={"apk_file": ("new.apk", baseline_bytes())})
    assert response.status_code == 200
    work = queued.call_args.args[0]

    def interrupted(_ssh, _payload, _path, check):
        check()
        debug_service.store().revoke(session["id"], session["owner_id"])
        check()
        pytest.fail("revoked session reached a later installation step")

    with patch.object(debug_service.deployment_execution, "install", side_effect=interrupted):
        debug_service.execute_install(work)
    result = deployment_service.store().get(work["id"], session["owner_id"])
    assert result["status"] == "failed" and result["result"]["code"] == "FORBIDDEN"


def test_only_granted_actions_execute_and_text_is_not_kept_in_audit(environment):
    client, *_ = environment
    session = approve(client, request_session(client, operations="launch,text"))
    path = "/api/devices/debug-sessions/" + session["id"] + "/actions"
    with patch.object(debug_execution, "perform", return_value={"action": "text"}) as perform:
        assert client.post(path, json={"action": "stop"}).status_code == 403
        assert client.post(path, json={"action": "text", "text": "debug input"}).status_code == 200
    assert perform.call_count == 1
    history = debug_service.store().history(session["id"], session["owner_id"])
    assert "debug input" not in str(history) and "text_sha256" in history[0]["details"]


def test_busy_session_serializes_operations_across_processes(tmp_path):
    repository = DebugStore(tmp_path)
    session = {"id": "a" * 32, "owner_id": "owner", "actor_id": "agent:a", "operations": ["stop"],
               "expires_at": time.time() + 300}
    repository.create(session)
    repository.reserve_approval(session["id"], "owner")
    repository.activate(session["id"], "owner", {"expires_at": time.time() + 3600})

    def begin(_):
        try:
            return DebugStore(tmp_path).begin(session["id"], "owner", "agent:a", "stop", {})
        except ApiError:
            return ""

    with ThreadPoolExecutor(4) as pool:
        winners = [result for result in pool.map(begin, range(4)) if result]
    assert len(winners) == 1
    repository.revoke(session["id"], "owner")
    repository.finish(session["id"], winners[0], "completed", {})
    with pytest.raises(ApiError):
        repository.begin(session["id"], "owner", "agent:a", "stop", {})


def test_revoke_during_operation_stops_next_step_and_releases_after_finish(environment):
    client, repository, *_ = environment
    session = approve(client, request_session(client))
    op = debug_service.store().begin(session["id"], session["owner_id"], session["actor_id"], "stop", {})
    assert client.post("/api/devices/debug-sessions/" + session["id"] + "/revoke").status_code == 200
    assert repository.claims.active_claim("worker:SERIAL") is not None
    with pytest.raises(ApiError):
        debug_service.check_live(session)
    debug_service.finish(session, op, "failed", {})
    assert repository.claims.active_claim("worker:SERIAL") is None


@pytest.mark.parametrize("focus", ["com.other.app", "com.android.permissioncontroller"])
def test_ui_input_requires_approved_app_foreground(focus):
    body = debug_api.DebugAction(action="tap", x=10, y=20)
    with patch.object(debug_execution, "run", return_value="mCurrentFocus=Window{abc u0 " + focus + "/.Main}") as run, \
            pytest.raises(ApiError):
        debug_execution.perform(object(), {"serial": "SERIAL", **IDENTITY}, body, lambda: None)
    assert run.call_count == 1 and "input" not in run.call_args.args[1][-1]


def test_launch_and_tap_compile_typed_quoted_commands():
    session = {"serial": "SERIAL", **IDENTITY}
    with patch.object(debug_execution, "run", side_effect=["com.example.app/.Main", "Status: ok"]) as run:
        debug_execution.perform(object(), session, debug_api.DebugAction(action="launch"), lambda: None)
    assert "am start -W --user 0 -n com.example.app/.Main" in run.call_args.args[1][-1]
    with patch.object(debug_execution, "run", side_effect=["mCurrentFocus=Window{abc u0 com.example.app/.Main}", ""]) as run:
        debug_execution.perform(object(), session, debug_api.DebugAction(action="tap", x=10, y=20), lambda: None)
    assert run.call_args.args[1] == ["adb", "-s", "SERIAL", "shell", "input -d 0 tap 10 20"]


@pytest.mark.parametrize("body", [{"action": "text", "text": "a;reboot"}, {"action": "keyevent", "key": "POWER"},
                                 {"action": "stop", "x": 1}, {"action": "tap", "x": 1}])
def test_invalid_or_system_actions_never_reach_shell(environment, body):
    client, *_ = environment
    session = approve(client, request_session(client))
    with patch.object(debug_execution, "perform") as perform:
        response = client.post("/api/devices/debug-sessions/" + session["id"] + "/actions", json=body)
    assert response.status_code == 422
    perform.assert_not_called()


def test_verified_apk_identity_and_invalid_signature(tmp_path):
    artifact = tmp_path / "artifact.apk"
    artifact.write_bytes(b"fixture")
    import hashlib
    sha = hashlib.sha256(artifact.read_bytes()).hexdigest()
    outputs = [CommandResult(stdout="package: name='com.example.app' versionCode='1'"),
               CommandResult(stdout="Signer #1 certificate SHA-256 digest: " + "a" * 64)]
    with patch.object(debug_identity, "binary", side_effect=lambda name: name), \
            patch.object(debug_identity, "run_local_command", side_effect=outputs) as run:
        assert debug_identity.inspect_apk(artifact, sha) == IDENTITY
    assert run.call_args.args[0] == ["apksigner", "verify", "--print-certs", str(artifact)]
    with patch.object(debug_identity, "binary", side_effect=lambda name: name), \
            patch.object(debug_identity, "run_local_command", return_value=CommandResult(code=1)), pytest.raises(ApiError):
        debug_identity.inspect_apk(artifact, sha)


@pytest.mark.parametrize("phase", ["approval", "action"])
def test_installed_signer_mismatch_blocks_debug(environment, phase):
    client, repository, *_ = environment
    session = request_session(client, operations="stop")
    if phase == "action":
        session = approve(client, session)
    with patch.object(debug_service, "require_installed", debug_identity.require_installed), patch.object(
        debug_identity, "installed_fingerprint", return_value=("/data/app/base.apk", "boot:stat:v1:" + "d" * 64),
    ), patch.object(
        debug_identity, "installed_identity", return_value={**IDENTITY, "signers": ["b" * 64], 'sha256': 'd' * 64},
    ), patch.object(debug_execution, "perform") as perform:
        if phase == "approval":
            response = client.post("/api/devices/debug-sessions/" + session["id"] + "/approve",
                                   headers={"x-test-principal": "session"})
            assert repository.claims.active_claim("worker:SERIAL") is None
            assert debug_service.store().get(session["id"], session["owner_id"])["status"] == "pending"
        else:
            response = client.post("/api/devices/debug-sessions/" + session["id"] + "/actions", json={"action": "stop"})
        assert response.status_code == 403
        perform.assert_not_called()


def test_failed_baseline_revokes_grant_and_blocks_actions(environment):
    client, repository, *_ = environment
    session = request_session(client)
    with patch.object(debug_service, "execute_install") as queued:
        response = client.post("/api/devices/debug-sessions/" + session["id"] + "/approve",
                               headers={"x-test-principal": "session"})
    assert response.status_code == 200
    with patch.object(debug_service.deployment_execution, "install", side_effect=ApiError.upstream_failure("failed", service="adb")):
        debug_service.execute_install(queued.call_args.args[0])
    assert debug_service.store().get(session["id"], session["owner_id"])["status"] == "revoked"
    assert repository.claims.active_claim("worker:SERIAL") is None
    with patch.object(debug_execution, "perform") as perform:
        assert client.post("/api/devices/debug-sessions/" + session["id"] + "/actions", json={"action": "stop"}).status_code == 403
        perform.assert_not_called()


@pytest.mark.parametrize("failure", ["scope", "unapproved_operation", "package", "lease", "actor", "signer", "none"])
def test_database_reads_are_app_scoped_leased_and_audited(environment, failure):
    client, repository, *_ = environment
    session = approve(client, request_session(client, operations="stop" if failure == "unapproved_operation" else "database_read"))
    body = {"worker_id": "worker", "device": "SERIAL", "package": "com.example.app", "database": "app.db",
            "table": "private_rows", "debug_session_id": session["id"]}
    headers = {"x-test-scopes": "devices.read,devices.use_leased,devices.database.read"}
    if failure == "scope":
        headers["x-test-scopes"] = "devices.read,devices.use_leased"
    elif failure == "package":
        body["package"] = "com.other.app"
    elif failure == "lease":
        repository.claims.release(session["_claim_source"])
    elif failure == "actor":
        headers["x-test-actor"] = "agent:other"
    with patch.object(debug_service.deployment_execution,
                      "query_database", return_value={"rows": [["sensitive result"]]}) as query, patch.object(
        debug_service, "require_installed", side_effect=ApiError.forbidden("signer") if failure == "signer" else None,
    ):
        response = client.post("/api/devices/db/query", json=body, headers=headers)
    if failure == "none":
        assert response.status_code == 200, response.text
        query.assert_called_once()
        history = debug_service.store().history(session["id"], session["owner_id"])
        assert history[0]["details"] == {"actor_id": session["actor_id"], "package": body["package"],
                                         "database": "app.db", "table": "private_rows"}
        assert "sensitive result" not in str(history)
    else:
        assert response.status_code in {403, 409}, response.text
        query.assert_not_called()


def test_approval_does_not_allow_actions_before_baseline_completion(environment):
    client, *_ = environment
    session = request_session(client)
    with patch.object(debug_service, "execute_install"):
        assert client.post("/api/devices/debug-sessions/" + session["id"] + "/approve",
                           headers={"x-test-principal": "session"}).status_code == 200
    # Even if an operation slot becomes empty, the persisted baseline gates use.
    current = debug_service.store().get(session["id"], session["owner_id"])
    debug_service.finish(current, current["busy"], "interrupted", {})
    with patch.object(debug_execution, "perform") as perform:
        assert client.post("/api/devices/debug-sessions/" + session["id"] + "/actions",
                           json={"action": "stop"}).status_code == 409
        perform.assert_not_called()


@pytest.mark.parametrize("existing", [True, False])
def test_inaccessible_grant_renders_help_without_exposing_session(environment, existing):
    client, *_ = environment
    session = request_session(client)
    sid = session["id"] if existing else "0" * 32
    path = "/api/devices/debug-sessions/" + sid
    headers = {"x-test-owner": "other", "x-test-principal": "session"}
    page = client.get(path + "/review", headers=headers)
    assert page.status_code == 200 and page.headers["content-type"].startswith("text/html")
    assert "Agent 所属平台账号" in page.text
    assert session["baseline_sha256"] not in page.text and session["package"] not in page.text
    assert client.get(path, headers=headers).status_code == 404
    assert client.post(path + "/approve", headers=headers).status_code == 404
    assert client.get(path + "/review", headers={**headers, "x-test-role": "worker_service"}).status_code == 200
