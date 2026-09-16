import tempfile
from pathlib import Path
from unittest.mock import patch

from starlette.requests import Request

from features.auth import CurrentUser
from features.devices import operation_claims, support
from features.devices.locks import DeviceLockManager


def authenticated_request(
    user_id: str,
    username: str,
    role: str = "device_operator",
    extra_permissions: frozenset[str] = frozenset(),
    resource_owner_id: str = "",
) -> Request:
    request = Request({
        "type": "http",
        "method": "POST",
        "path": "/api/devices/reboot",
        "headers": [],
        "client": ("127.0.0.1", 1234),
    })
    request.state.current_user = CurrentUser(
        id=user_id,
        username=username,
        role=role,
        extra_permissions=extra_permissions,
        resource_owner_id=resource_owner_id or user_id,
    )
    return request


def test_dynamic_operation_claim_is_atomic_and_fenced():
    with tempfile.TemporaryDirectory() as directory:
        manager = DeviceLockManager(
            Path(directory) / "claims.sqlite3",
            local_worker_id="ats-worker-controller",
        )
        alice = authenticated_request("user-alice", "alice")
        bob = authenticated_request("user-bob", "bob")
        with patch.object(operation_claims, "device_lock_manager", manager):
            source_id, records, conflict = support.acquire_device_operation_claim(
                alice, ["SERIAL-1"], "reboot",
            )
            assert conflict is None
            assert source_id.startswith("operation:reboot:")
            assert records[0]["owner_id"] == "user-alice"
            assert records[0]["generation"] == 1
            assert alice.state.device_lease_tokens == [{
                "lease_id": records[0]["id"],
                "device_id": "ats-worker-controller:SERIAL-1",
                "generation": 1,
                "owner_id": "user-alice",
            }]

            _, conflicting_records, conflict = support.acquire_device_operation_claim(
                bob, ["SERIAL-1"], "remount",
            )
            assert conflict.status_code == 409
            assert conflicting_records[0]["id"] == records[0]["id"]

            assert support.release_device_operation_claim(source_id) == 1
            _, next_records, next_conflict = support.acquire_device_operation_claim(
                bob, ["SERIAL-1"], "remount",
            )
            assert next_conflict is None
            assert next_records[0]["generation"] == 2


def test_operation_claim_rejects_untrusted_device_ids():
    request = authenticated_request("user-alice", "alice")
    source_id, records, conflict = support.acquire_device_operation_claim(
        request, ["SERIAL; reboot"], "reboot",
    )
    assert source_id == ""
    assert records == []
    assert conflict.status_code == 400


def test_plain_user_can_operate_free_device():
    with tempfile.TemporaryDirectory() as directory:
        manager = DeviceLockManager(
            Path(directory) / "claims.sqlite3",
            local_worker_id="ats-worker-controller",
        )
        alice = authenticated_request("user-alice", "alice", role="user")
        with patch.object(operation_claims, "device_lock_manager", manager):
            source_id, records, conflict = support.acquire_device_operation_claim(
                alice, ["SERIAL-9"], "wifi",
            )
            assert conflict is None
            assert source_id.startswith("operation:wifi:")
            assert records[0]["owner_id"] == "user-alice"
            assert support.release_device_operation_claim(source_id) == 1


def test_agent_token_without_use_leased_scope_is_rejected():
    with tempfile.TemporaryDirectory() as directory:
        manager = DeviceLockManager(
            Path(directory) / "claims.sqlite3",
            local_worker_id="ats-worker-controller",
        )
        agent = authenticated_request("agent-1", "agent:agt_x", role="agent_service")
        with patch.object(operation_claims, "device_lock_manager", manager):
            source_id, records, conflict = support.acquire_device_operation_claim(
                agent, ["SERIAL-1"], "wifi",
            )
            assert source_id == ""
            assert records == []
            assert conflict.status_code == 403
            assert b"devices.use_leased" in conflict.body


def test_agent_token_with_use_leased_scope_can_operate():
    with tempfile.TemporaryDirectory() as directory:
        manager = DeviceLockManager(
            Path(directory) / "claims.sqlite3",
            local_worker_id="ats-worker-controller",
        )
        agent = authenticated_request(
            "agent-1",
            "agent:agt_x",
            role="agent_service",
            extra_permissions=frozenset({"devices.use_leased"}),
        )
        with patch.object(operation_claims, "device_lock_manager", manager):
            source_id, records, conflict = support.acquire_device_operation_claim(
                agent, ["SERIAL-1"], "wifi",
            )
            assert conflict is None
            assert records[0]["owner_id"] == "agent-1"
            assert support.release_device_operation_claim(source_id) == 1


def test_machine_operation_claim_uses_resource_owner_and_borrows_reservation():
    """Synthetic automation actor must share its human account's fencing."""
    with tempfile.TemporaryDirectory() as directory:
        manager = DeviceLockManager(
            Path(directory) / "claims.sqlite3",
            local_worker_id="ats-worker-controller",
        )
        machine = authenticated_request(
            "automation:run-1",
            "automation:run-1",
            role="agent_service",
            extra_permissions=frozenset({"devices.use_leased"}),
            resource_owner_id="user-alice",
        )
        with patch.object(operation_claims, "device_lock_manager", manager):
            ok, _ = manager.lock_devices(
                ["SERIAL-1"],
                "user-alice",
                "alice",
                source_id="reservation:res-1",
                source_type="cluster-reservation",
                ttl_seconds=3600,
                allow_existing_source=True,
            )
            assert ok
            source_id, records, conflict = support.acquire_device_operation_claim(
                machine, ["SERIAL-1"], "remount",
            )
            assert conflict is None
            assert source_id == ""
            assert records[0]["owner_id"] == "user-alice"
            assert machine.state.device_lease_tokens[0]["owner_id"] == "user-alice"


def test_lock_status_resolves_internal_owner_to_user_management_identity():
    with tempfile.TemporaryDirectory() as directory:
        manager = DeviceLockManager(
            Path(directory) / "claims.sqlite3",
            local_worker_id="ats-worker-controller",
        )
        success, _ = manager.lock_device(
            "SERIAL-1",
            "N387pLbIBhpMw5JsWUL9hg",
            "N387pLbIBhpMw5JsWUL9hg",
        )
        assert success
        with patch("features.users.resolve_client_display_id", return_value="hcq@172.16.14.66"):
            assert manager.get_lock_status("SERIAL-1")["locked_by"] == "hcq@172.16.14.66"


def test_operation_claim_borrows_existing_claim_for_same_owner():
    with tempfile.TemporaryDirectory() as directory:
        manager = DeviceLockManager(
            Path(directory) / "claims.sqlite3",
            local_worker_id="ats-worker-controller",
        )
        alice = authenticated_request("user-alice", "alice")
        with patch.object(operation_claims, "device_lock_manager", manager):
            ok, _first = manager.lock_devices(
                ["SERIAL-1"], "user-alice", "alice",
                source_id="reservation:res-1", source_type="cluster-reservation",
                ttl_seconds=3600, allow_existing_source=True,
            )
            assert ok

            source_id, records, conflict = support.acquire_device_operation_claim(
                alice, ["SERIAL-1"], "remount",
            )
            assert conflict is None
            assert records[0]["source_id"] == "reservation:res-1"
            assert support.release_device_operation_claim(source_id) == 0
            active = manager.registry.list_active(worker_id="ats-worker-controller")
            assert any(c["source_id"] == "reservation:res-1" for c in active)


def test_operation_claim_does_not_borrow_running_cluster_job_claim():
    with tempfile.TemporaryDirectory() as directory:
        manager = DeviceLockManager(
            Path(directory) / "claims.sqlite3",
            local_worker_id="ats-worker-controller",
        )
        alice = authenticated_request("user-alice", "alice")
        with patch.object(operation_claims, "device_lock_manager", manager):
            ok, _first = manager.lock_devices(
                ["SERIAL-1"], "user-alice", "alice",
                source_id="job:job-123", source_type="cluster-job",
                ttl_seconds=3600, allow_existing_source=True,
            )
            assert ok

            source_id, records, conflict = support.acquire_device_operation_claim(
                alice, ["SERIAL-1"], "reboot",
            )
            assert source_id == ""
            assert conflict is not None
            assert conflict.status_code == 409
            assert records[0]["source_type"] == "cluster-job"
            active = manager.registry.list_active(worker_id="ats-worker-controller")
            assert any(c["source_id"] == "job:job-123" for c in active)
