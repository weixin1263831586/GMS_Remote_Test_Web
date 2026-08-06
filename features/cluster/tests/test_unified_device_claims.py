from __future__ import annotations

from pathlib import Path

import pytest

from features.cluster import ClusterRepository
from features.devices import DeviceLockManager


def _repository(root: Path) -> ClusterRepository:
    claims = root / "device_claims.sqlite3"
    repository = ClusterRepository(
        root / "cluster.sqlite3",
        claim_db_path=claims,
    )
    repository.register_worker(
        {
            "worker_id": "ats-worker-controller",
            "name": "local",
            "hostname": "localhost",
            "address": "127.0.0.1",
            "agent_version": "1",
            "max_jobs": 1,
            "capabilities": {},
        }
    )
    repository.heartbeat(
        "ats-worker-controller",
        {
            "running_jobs": [],
            "devices": [{"serial": "ABC", "state": "available"}],
            "suites": [],
        },
    )
    return repository


def test_local_lock_blocks_cluster_lease_and_different_source_for_same_owner(tmp_path):
    repository = _repository(tmp_path)
    locks = DeviceLockManager(
        tmp_path / "device_claims.sqlite3",
        local_worker_id="ats-worker-controller",
    )
    acquired, _message = locks.lock_device(
        "ABC",
        "alice",
        "Alice",
        source_id="test:alice",
        source_type="test",
    )

    assert acquired
    same_owner_other_source, _message = locks.lock_device(
        "ABC",
        "alice",
        "Alice",
        source_id="firmware:alice",
        source_type="firmware",
    )
    assert not same_owner_other_source
    with pytest.raises(ValueError, match="already claimed"):
        repository.create_job_with_leases(
            {
                "worker_id": "ats-worker-controller",
                "owner_id": "bob",
                "devices": ["ABC"],
            }
        )


def test_cluster_lease_is_visible_to_local_ui_and_release_allows_reuse(tmp_path):
    repository = _repository(tmp_path)
    locks = DeviceLockManager(
        tmp_path / "device_claims.sqlite3",
        local_worker_id="ats-worker-controller",
    )
    job = repository.create_job_with_leases(
        {
            "worker_id": "ats-worker-controller",
            "owner_id": "alice",
            "devices": ["ABC"],
        }
    )

    status = locks.get_lock_status("ABC")
    assert status is not None
    assert status["client_id"] == "alice"
    assert status["source_type"] == "cluster-job"
    blocked, _message = locks.lock_device("ABC", "bob", "Bob")
    assert not blocked

    command = repository.create_command(
        {
            "worker_id": "ats-worker-controller",
            "command_type": "start_test",
            "job_id": job["id"],
            "attempt_id": job["current_attempt_id"],
            "payload": {},
        }
    )
    completed = repository.ack_command(
        "ats-worker-controller",
        command["id"],
        {"status": "completed", "result": {}, "error": ""},
    )
    repository.sync_job_from_command(completed)

    assert locks.get_lock_status("ABC") is None
    reused, _message = locks.lock_device("ABC", "bob", "Bob")
    assert reused


def test_cluster_reservation_blocks_local_lock_until_released(tmp_path):
    repository = _repository(tmp_path)
    locks = DeviceLockManager(
        tmp_path / "device_claims.sqlite3",
        local_worker_id="ats-worker-controller",
    )
    reservation = repository.reserve_devices(
        "ats-worker-controller",
        ["ABC"],
        owner_id="ats-user",
        source_id="ats-run-1",
    )

    blocked, _message = locks.lock_device("ABC", "manual-user", "Manual")
    assert not blocked
    assert repository.release_reservation(reservation["id"])
    acquired, _message = locks.lock_device("ABC", "manual-user", "Manual")
    assert acquired


def test_force_release_refuses_to_detach_active_cluster_job(tmp_path):
    repository = _repository(tmp_path)
    locks = DeviceLockManager(
        tmp_path / "device_claims.sqlite3",
        local_worker_id="ats-worker-controller",
    )
    job = repository.create_job_with_leases(
        {"worker_id": "ats-worker-controller", "owner_id": "alice", "devices": ["ABC"]}
    )

    released, message = locks.force_unlock_device("ABC")

    assert not released
    assert "集群任务或预约" in message
    assert repository.get_job(job["id"])["leases"][0]["status"] == "active"
    assert locks.get_lock_status("ABC") is not None


def test_reservation_renew_reacquires_missing_unified_claim(tmp_path):
    repository = _repository(tmp_path)
    reservation = repository.reserve_devices(
        "ats-worker-controller", ["ABC"], owner_id="alice", source_id="ats-run-1"
    )
    repository.claims.release(
        f"reservation:{reservation['id']}", status="expired"
    )

    assert repository.renew_reservation(reservation["id"], ttl_seconds=300)
    claim = repository.claims.active_claim("ats-worker-controller:ABC")
    assert claim is not None
    assert claim["source_id"] == f"reservation:{reservation['id']}"


def test_reservation_renew_expires_metadata_when_device_was_reclaimed(tmp_path):
    repository = _repository(tmp_path)
    locks = DeviceLockManager(
        tmp_path / "device_claims.sqlite3",
        local_worker_id="ats-worker-controller",
    )
    reservation = repository.reserve_devices(
        "ats-worker-controller", ["ABC"], owner_id="alice", source_id="ats-run-1"
    )
    repository.claims.release(
        f"reservation:{reservation['id']}", status="expired"
    )
    acquired, _message = locks.lock_device(
        "ABC", "bob", "Bob", source_id="test:bob", source_type="test"
    )
    assert acquired

    assert not repository.renew_reservation(reservation["id"], ttl_seconds=300)
    assert repository.get_reservation(reservation["id"])["status"] == "expired"


def test_cluster_job_fencing_generation_follows_all_unified_claims(tmp_path):
    repository = _repository(tmp_path)
    records = repository.acquire_device_operation_claim(
        "ats-worker-controller",
        ["ABC"],
        owner_id="admin",
        source_type="cluster-device-action",
        source_id="operation:inspect-1",
    )
    repository.claims.release("operation:inspect-1")

    job = repository.create_job_with_leases(
        {"worker_id": "ats-worker-controller", "owner_id": "alice", "devices": ["ABC"]}
    )

    lease = job["leases"][0]
    assert lease["id"].startswith("claim-")
    assert lease["generation"] == records[0]["generation"] + 1


def test_firmware_multi_device_claim_is_atomic_and_not_reentrant(tmp_path):
    locks = DeviceLockManager(
        tmp_path / "device_claims.sqlite3", local_worker_id="ats-worker-controller"
    )
    acquired, _records = locks.lock_devices(
        ["A", "B"],
        "alice",
        "Alice",
        source_id="firmware:alice",
        source_type="firmware",
        allow_existing_source=False,
    )
    duplicate, conflicts = locks.lock_devices(
        ["A", "B"],
        "alice",
        "Alice",
        source_id="firmware:alice",
        source_type="firmware",
        allow_existing_source=False,
    )

    assert acquired
    assert not duplicate
    assert {item["serial"] for item in conflicts} == {"A", "B"}

    busy, _message = locks.lock_device(
        "C", "bob", "Bob", source_id="test:bob", source_type="test"
    )
    assert busy
    atomic, _conflicts = locks.lock_devices(
        ["D", "C"],
        "alice",
        "Alice",
        source_id="firmware:alice-2",
        source_type="firmware",
        allow_existing_source=False,
    )
    assert not atomic
    assert locks.get_lock_status("D") is None


def test_firmware_claim_survives_worker_adb_fastboot_transition(tmp_path):
    repository = _repository(tmp_path)
    locks = DeviceLockManager(
        tmp_path / "device_claims.sqlite3",
        local_worker_id="ats-worker-controller",
    )
    acquired, _records = locks.lock_devices(
        ["ABC"],
        "alice",
        "Alice",
        source_id="firmware:alice",
        source_type="firmware",
        allow_existing_source=False,
    )
    assert acquired
    lease_id = locks.get_lock_status("ABC")["lease_id"]

    repository.heartbeat(
        "ats-worker-controller",
        {
            "running_jobs": [],
            "devices": [{"serial": "ABC", "state": "fastboot"}],
            "suites": [],
        },
    )
    fastboot = repository.list_devices("ats-worker-controller")[0]
    assert fastboot["state"] == "fastboot"
    assert fastboot["claimed"] is True
    assert fastboot["claim_source_type"] == "firmware"
    assert locks.get_lock_status("ABC")["lease_id"] == lease_id

    repository.heartbeat(
        "ats-worker-controller",
        {
            "running_jobs": [],
            "devices": [{"serial": "ABC", "state": "available"}],
            "suites": [],
        },
    )
    adb = repository.list_devices("ats-worker-controller")[0]
    assert adb["state"] == "available"
    assert adb["claimed"] is True
    assert locks.get_lock_status("ABC")["lease_id"] == lease_id
