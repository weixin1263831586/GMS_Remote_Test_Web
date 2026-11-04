"""Force release must end the exact local replay and revoke all its aliases."""
from unittest.mock import patch

import pytest

from features.cluster.device_action_operations import fenced_local_payload

from .test_unified_device_claims import _repository


WORKER = "ats-worker-controller"
KEY = f"{WORKER}:ABC"
PORT = f"{WORKER}:@usb:1-6"


@pytest.fixture
def repository(tmp_path):
    repo = _repository(tmp_path)
    repo.heartbeat(WORKER, {"devices": [
        {"serial": "ABC", "state": "available", "properties": {"usb": "1-6"}},
        {"serial": "OTHER", "state": "available", "properties": {"usb": "1-7"}},
    ]})
    return repo


def operation(repo, *, action="aoa_sequence", release=True):
    records = repo.acquire_device_operation_claim(WORKER, [KEY], owner_id="alice",
        source_type="cluster-device-action", source_id="operation:replay", ttl_seconds=3600)
    command = repo.create_command({"worker_id": WORKER, "command_type": "device_action",
        "operation_id": "replay", "initial_status": "accepted", "payload": {
            "action": action, "devices": [KEY], "owner_id": "alice", "playback_control_id": "pb-test",
            "claim_source_id": "operation:replay", "release_claim_on_terminal": release,
            "lease_tokens": repo.claim_fencing_tokens(records, "replay", device_ids={KEY}),
        }})
    repo.start_local_command(command["id"])
    return command


def test_paused_local_replay_and_port_are_released_without_replaying(repository):
    repo = repository
    command = operation(repo)
    original = fenced_local_payload(repo, command["payload"])
    repo.request_aoa_playback_control(WORKER, "pb-test", "alice", "pause")
    repo.acquire_device_operation_claim(WORKER, [f"{WORKER}:OTHER"], owner_id="alice",
        source_type="cluster-device-action", source_id="operation:other")
    success, message = repo.force_release_local_aoa_operation(KEY, WORKER)
    assert success and "USB 端口锁" in message
    assert repo.claims.active_claim(KEY) is None and repo.claims.active_claim(PORT) is None
    assert repo.claims.active_claim(f"{WORKER}:OTHER") is not None
    current = repo.get_command(command["id"])
    assert current["status"] == "cancelled"
    assert current["payload"]["aoa_control_action"] == "cancel"
    assert current["result"]["forced_release"] is True
    assert current["result"]["reconcile_required"] is True
    with pytest.raises(RuntimeError, match="claim changed"):
        original["_validate_fencing"]()
    # New input is admitted under a new lease, while the old executor's late
    # success ACK cannot resurrect the cancelled playback.
    repo.acquire_device_operation_claim(WORKER, [KEY], owner_id="bob",
        source_type="cluster-device-action", source_id="operation:new")
    repo.ack_command(WORKER, command["id"], {"status": "completed", "result": {"success": True}})
    assert repo.get_command(command["id"])["status"] == "cancelled"
    assert repo.claims.active_claim(KEY)["owner_id"] == "bob"


def test_terminal_receipt_keeps_its_result_while_orphan_claim_is_released(repository):
    repo = repository
    command = operation(repo)
    repo.ack_command(WORKER, command["id"], {"status": "failed", "error": "USB disconnected"})
    assert repo.force_release_local_aoa_operation(KEY, WORKER)[0]
    current = repo.get_command(command["id"])
    assert current["status"] == "failed" and current["error"] == "USB disconnected"
    assert repo.claims.active_claim(PORT) is None


@pytest.mark.parametrize("kwargs", [{"action": "reboot"}, {"release": False}])
def test_active_non_aoa_or_borrowed_claim_is_not_detached(repository, kwargs):
    operation(repository, **kwargs)
    assert not repository.force_release_local_aoa_operation(KEY, WORKER)[0]
    assert repository.claims.active_claim(KEY) is not None
    assert repository.claims.active_claim(PORT) is not None


def test_remote_or_unidentified_operation_is_not_detached(repository):
    operation(repository)
    assert not repository.force_release_local_aoa_operation(KEY, "another-worker")[0]
    with patch.object(repository, "get_command_by_operation", return_value=None):
        assert not repository.force_release_local_aoa_operation(KEY, WORKER)[0]
    assert repository.claims.active_claim(KEY) is not None


def test_worker_delivered_command_cannot_be_revoked_through_shared_local_fence(repository):
    command = operation(repository)
    with repository.connect() as conn:
        conn.execute("UPDATE cluster_commands SET delivered_at=? WHERE id=?", ("2026-11-04T06:00:00Z", command["id"]))
    assert not repository.force_release_local_aoa_operation(KEY, WORKER)[0]
    assert repository.claims.active_claim(KEY) is not None


def test_changed_lease_is_never_released_even_with_reused_source_id(repository):
    repo = repository
    command = operation(repo)
    old = command["payload"]["lease_tokens"][0]
    repo.claims.release("operation:replay")
    repo.acquire_device_operation_claim(WORKER, [KEY], owner_id="alice",
        source_type="cluster-device-action", source_id="operation:replay")
    assert not repo.claims.release_operation_if_current(KEY, old["lease_id"], old["generation"], "operation:replay")
    assert not repo.force_release_local_aoa_operation(KEY, WORKER)[0]
    assert repo.claims.active_claim(KEY) is not None
    assert repo.claims.active_claim(PORT) is not None
