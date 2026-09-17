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
                alice,
                ["SERIAL-1"],
                "reboot",
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

            _, conflicting_records, conflict = (
                support.acquire_device_operation_claim(
                    bob,
                    ["SERIAL-1"],
                    "remount",
                )
            )
            assert conflict.status_code == 409
            assert conflicting_records[0]["id"] == records[0]["id"]

            assert support.release_device_operation_claim(source_id) == 1
            _, next_records, next_conflict = (
                support.acquire_device_operation_claim(
                    bob,
                    ["SERIAL-1"],
                    "remount",
                )
            )
            assert next_conflict is None
            assert next_records[0]["generation"] == 2


def test_operation_claim_rejects_untrusted_device_ids():
    request = authenticated_request("user-alice", "alice")
    source_id, records, conflict = support.acquire_device_operation_claim(
        request,
        ["SERIAL; reboot"],
        "reboot",
    )
    assert source_id == ""
    assert records == []
    assert conflict.status_code == 400


def test_plain_user_can_operate_free_device():
    """普通用户（无 devices.lease）可直接操作空闲设备。

    常规设备运维（wifi/reboot/remount 等）不再要求"先租后用"；设备
    归属冲突仍由 claim 409 fencing 兜底，高危操作另有管理员提权门。
    """
    with tempfile.TemporaryDirectory() as directory:
        manager = DeviceLockManager(
            Path(directory) / "claims.sqlite3",
            local_worker_id="ats-worker-controller",
        )
        alice = authenticated_request("user-alice", "alice", role="user")
        with patch.object(operation_claims, "device_lock_manager", manager):
            source_id, records, conflict = support.acquire_device_operation_claim(
                alice,
                ["SERIAL-9"],
                "wifi",
            )
            assert conflict is None
            assert source_id.startswith("operation:wifi:")
            assert records[0]["owner_id"] == "user-alice"
            assert support.release_device_operation_claim(source_id) == 1


def test_agent_token_without_use_leased_scope_is_rejected():
    """Agent Service Token 没有 devices.use_leased scope 时 403。"""
    with tempfile.TemporaryDirectory() as directory:
        manager = DeviceLockManager(
            Path(directory) / "claims.sqlite3",
            local_worker_id="ats-worker-controller",
        )
        agent = authenticated_request("agent-1", "agent:agt_x", role="agent_service")
        with patch.object(operation_claims, "device_lock_manager", manager):
            source_id, records, conflict = support.acquire_device_operation_claim(
                agent,
                ["SERIAL-1"],
                "wifi",
            )
            assert source_id == ""
            assert records == []
            assert conflict.status_code == 403
            assert b"devices.use_leased" in conflict.body


def test_agent_token_with_use_leased_scope_can_operate():
    """显式授予 devices.use_leased scope 的 agent token 可正常操作。"""
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
                agent,
                ["SERIAL-1"],
                "wifi",
            )
            assert conflict is None
            assert records[0]["owner_id"] == "agent-1"
            assert support.release_device_operation_claim(source_id) == 1


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
        with patch(
            "features.users.resolve_client_display_id",
            return_value="hcq@172.16.14.66",
        ):
            assert (
                manager.get_lock_status("SERIAL-1")["locked_by"]
                == "hcq@172.16.14.66"
            )


def test_operation_claim_borrows_existing_claim_for_same_owner():
    """A device already claimed by the same owner (reservation/job/
    earlier operation) must be reused, not rejected with a spurious 409."""
    with tempfile.TemporaryDirectory() as directory:
        manager = DeviceLockManager(
            Path(directory) / "claims.sqlite3",
            local_worker_id="ats-worker-controller",
        )
        alice = authenticated_request("user-alice", "alice")
        with patch.object(operation_claims, "device_lock_manager", manager):
            # Alice holds the device via a long-lived reservation claim.
            ok, _first = manager.lock_devices(
                ["SERIAL-1"], "user-alice", "alice",
                source_id="reservation:res-1", source_type="cluster-reservation",
                ttl_seconds=3600, allow_existing_source=True,
            )
            assert ok

            source_id, records, conflict = (
                support.acquire_device_operation_claim(alice, ["SERIAL-1"], "remount")
            )
            assert conflict is None
            # The existing claim is borrowed, not re-acquired under a new
            # source_id; nothing is released by the operation source_id.
            assert records[0]["source_id"] == "reservation:res-1"
            assert support.release_device_operation_claim(source_id) == 0
            # The original reservation claim survives the operation.
            active = manager.registry.list_active(worker_id="ats-worker-controller")
            assert any(
                c["source_id"] == "reservation:res-1" for c in active
            )


def _request_with_owner(actor_id: str, owner_id: str) -> Request:
    """ATS machine principal: actor != resource owner (ADR 0010/0012)."""
    request = Request({
        "type": "http",
        "method": "POST",
        "path": "/api/devices/remount",
        "headers": [],
        "client": ("127.0.0.1", 1234),
    })
    request.state.current_user = CurrentUser(
        id=actor_id,
        username=actor_id,
        role="agent_service",
        extra_permissions=frozenset({"devices.use_leased"}),
        resource_owner_id=owner_id,
    )
    return request


def test_machine_principal_reuses_its_owner_reservation():
    """ADR 0010 回归：machine actor=automation:run-1，owner=user-alice。

    设备被 alice 的 cluster-reservation 占有时，ATS machine 执行
    remount/flash 必须按 resource_owner_id 复用同一预约；按 actor id
    fencing 会把"自己的预约"判成别人占用（409），整个 run 中途失败。
    """
    with tempfile.TemporaryDirectory() as directory:
        manager = DeviceLockManager(
            Path(directory) / "claims.sqlite3",
            local_worker_id="ats-worker-controller",
        )
        machine = _request_with_owner("automation:run-1", "user-alice")
        with patch.object(operation_claims, "device_lock_manager", manager):
            ok, _first = manager.lock_devices(
                ["SERIAL-1"], "user-alice", "alice",
                source_id="reservation:res-1", source_type="cluster-reservation",
                ttl_seconds=3600, allow_existing_source=True,
            )
            assert ok

            source_id, records, conflict = (
                support.acquire_device_operation_claim(
                    machine, ["SERIAL-1"], "remount"
                )
            )
            assert conflict is None
            assert records[0]["source_id"] == "reservation:res-1"
            assert support.release_device_operation_claim(source_id) == 0


def test_machine_principal_from_another_owner_still_conflicts():
    """别人的 machine principal（不同 owner）不得复用预约。"""
    with tempfile.TemporaryDirectory() as directory:
        manager = DeviceLockManager(
            Path(directory) / "claims.sqlite3",
            local_worker_id="ats-worker-controller",
        )
        machine = _request_with_owner("automation:run-2", "user-bob")
        with patch.object(operation_claims, "device_lock_manager", manager):
            ok, _first = manager.lock_devices(
                ["SERIAL-1"], "user-alice", "alice",
                source_id="reservation:res-1", source_type="cluster-reservation",
                ttl_seconds=3600, allow_existing_source=True,
            )
            assert ok

            _source_id, _records, conflict = (
                support.acquire_device_operation_claim(
                    machine, ["SERIAL-1"], "remount"
                )
            )
            assert conflict is not None
            assert conflict.status_code == 409


def test_operation_claim_does_not_borrow_running_cluster_job_claim():
    """同 owner 的运行中 cluster-job claim 不得被直接操作借用。

    否则用户 A 在 CTS/GTS 任务运行期间从设备页发 reboot/remount 等
    mutation 会静默借用自己的 job claim，污染正在跑的测试；必须 409
    冲突并指明持有者是集群任务（workflow 级重入，非 owner 级）。
    """
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

            source_id, records, conflict = (
                support.acquire_device_operation_claim(alice, ["SERIAL-1"], "reboot")
            )
            assert source_id == ""
            assert conflict is not None
            assert conflict.status_code == 409
            assert records[0]["source_type"] == "cluster-job"
            # The job claim is untouched by the refused operation.
            active = manager.registry.list_active(worker_id="ats-worker-controller")
            assert any(c["source_id"] == "job:job-123" for c in active)
