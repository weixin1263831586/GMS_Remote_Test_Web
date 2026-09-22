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


def test_adb_proxy_reservation_converts_to_job_and_fences_source_device(tmp_path):
    """proxy 预约 → 从预约建任务必须成功，且物理源设备同时被 fencing。

    回归：create_job_with_leases 曾把 _claim_devices 展开后的 fencing 集合
    （proxy + source_worker:serial）与 reservation.requested（只含 proxy id）
    严格比较，导致 ADB Proxy + 预约路径永远报
    "test devices do not match the active reservation"。
    预约一致性只应比较请求集合；物理 fencing 由展开后的 claim 集合保证。
    """
    repository = ClusterRepository(
        tmp_path / "cluster.sqlite3",
        claim_db_path=tmp_path / "device_claims.sqlite3",
    )
    for worker_id in ("ats-worker-controller", "worker-source"):
        repository.register_worker(
            {
                "worker_id": worker_id,
                "name": worker_id,
                "hostname": worker_id,
                "address": "127.0.0.1",
                "agent_version": "1",
                "max_jobs": 2,
                "capabilities": {},
            }
        )
    repository.heartbeat(
        "worker-source",
        {
            "running_jobs": [],
            "devices": [{"serial": "PHYSICAL_ABC", "state": "available"}],
            "suites": [],
        },
    )
    repository.heartbeat(
        "ats-worker-controller",
        {
            "running_jobs": [],
            "devices": [
                {
                    "serial": "PROXY_ABC",
                    "state": "available",
                    "transport": "adb_proxy",
                    "properties": {
                        "adb_proxy_source_worker_id": "worker-source",
                        "adb_proxy_source_serial": "PHYSICAL_ABC",
                    },
                }
            ],
            "suites": [],
        },
    )

    reservation = repository.reserve_devices(
        "ats-worker-controller",
        ["PROXY_ABC"],
        owner_id="alice",
        source_id="ats-run-1",
    )
    assert reservation["status"] == "active"

    job = repository.create_job_with_leases(
        {
            "worker_id": "ats-worker-controller",
            "owner_id": "alice",
            "devices": ["PROXY_ABC"],
            "device_reservation_id": reservation["id"],
            "automation_run_id": "ats-run-1",
        }
    )

    # Job created and the reservation was converted.
    assert repository.get_reservation(reservation["id"])["status"] == "converted"
    # Physical fencing survived the transfer: the source worker's local-USB
    # key is claimed by the job, not left free for a concurrent operation.
    source_claim = repository.claims.active_claim("worker-source:PHYSICAL_ABC")
    assert source_claim is not None
    assert source_claim["source_id"] == f"job:{job['id']}"
    assert source_claim["source_type"] == "cluster-job"
    proxy_claim = repository.claims.active_claim("ats-worker-controller:PROXY_ABC")
    assert proxy_claim is not None
    assert proxy_claim["source_id"] == f"job:{job['id']}"


def test_reservation_rejects_job_with_extra_requested_device(tmp_path):
    """请求集合与预约不一致仍必须拒绝（不能借集合比较放松越权设备）。"""
    repository = _repository(tmp_path)
    repository.heartbeat(
        "ats-worker-controller",
        {
            "running_jobs": [],
            "devices": [
                {"serial": "ABC", "state": "available"},
                {"serial": "XYZ", "state": "available"},
            ],
            "suites": [],
        },
    )
    reservation = repository.reserve_devices(
        "ats-worker-controller", ["ABC"], owner_id="alice", source_id="ats-run-1"
    )

    with pytest.raises(ValueError, match="do not match the active reservation"):
        repository.create_job_with_leases(
            {
                "worker_id": "ats-worker-controller",
                "owner_id": "alice",
                "devices": ["ABC", "XYZ"],
                "device_reservation_id": reservation["id"],
                "automation_run_id": "ats-run-1",
            }
        )


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


def test_startup_reconciliation_repairs_cross_db_drift(tmp_path):
    """崩溃 split-brain 修复：

    - active reservation 丢了物理 claim（claim 过期/半提交）→ 重启后重取；
    - 已结束 reservation 的幽灵 claim → 重启后释放；
    - 终态 job 的残留 claim → 重启后释放。
    """
    repository = _repository(tmp_path)
    # 1) active reservation 丢 claim
    reservation = repository.reserve_devices(
        "ats-worker-controller", ["ABC"], owner_id="alice", source_id="ats-run-1"
    )
    repository.claims.release(
        f"reservation:{reservation['id']}", status="expired"
    )
    assert repository.claims.active_claim("ats-worker-controller:ABC") is None

    stats = repository.reconcile_claims()

    assert stats["reservation_claims_reacquired"] == 1
    claim = repository.claims.active_claim("ats-worker-controller:ABC")
    assert claim is not None
    assert claim["source_id"] == f"reservation:{reservation['id']}"

    # 2) 已释放 reservation 的幽灵 claim
    repository.release_reservation(reservation["id"])
    # 人为制造幽灵：直接以 reservation source 重新占上
    repository.acquire_device_operation_claim(
        "ats-worker-controller", ["ABC"],
        owner_id="alice", source_type="cluster-reservation",
        source_id=f"reservation:{reservation['id']}", ttl_seconds=3600,
    )
    assert repository.claims.active_claim("ats-worker-controller:ABC") is not None

    stats = repository.reconcile_claims()

    assert stats["reservation_claims_released"] >= 1
    assert repository.claims.active_claim("ats-worker-controller:ABC") is None

    # 3) 终态 job 的残留 claim
    job = repository.create_job_with_leases(
        {"worker_id": "ats-worker-controller", "owner_id": "alice", "devices": ["ABC"]}
    )
    # 模拟 job 已终态但 claim 未释放（进程崩溃在状态提交后）
    with repository.connect() as conn:
        conn.execute(
            "UPDATE cluster_jobs SET status='completed' WHERE id=?", (job["id"],)
        )
        conn.commit()
    assert repository.claims.active_claim("ats-worker-controller:ABC") is not None

    stats = repository.reconcile_claims()

    assert stats["job_claims_released"] == 1
    assert repository.claims.active_claim("ats-worker-controller:ABC") is None


def test_startup_reconciliation_refences_running_job_after_claim_loss(tmp_path):
    """运行中 job 丢 claim（崩溃在状态提交后、claim 未写/被误释放）→
    reconcile 按 device_leases 重取 claim 恢复 fencing；重取失败则把
    job 置为 failed，绝不让无 fencing 的任务继续跑。"""
    repository = _repository(tmp_path)
    job = repository.create_job_with_leases(
        {"worker_id": "ats-worker-controller", "owner_id": "alice", "devices": ["ABC"]}
    )
    repository.claims.release(f"job:{job['id']}", status="expired")
    assert repository.claims.active_claim("ats-worker-controller:ABC") is None

    stats = repository.reconcile_claims()

    assert stats["job_claims_reacquired"] == 1
    claim = repository.claims.active_claim("ats-worker-controller:ABC")
    assert claim is not None
    assert claim["source_id"] == f"job:{job['id']}"
    assert repository.get_job(job["id"])["status"] in (
        "assigned", "dispatching", "running", "stopping"
    )


def test_startup_reconciliation_fails_unfenceable_running_job(tmp_path):
    repository = _repository(tmp_path)
    locks = DeviceLockManager(
        tmp_path / "device_claims.sqlite3",
        local_worker_id="ats-worker-controller",
    )
    job = repository.create_job_with_leases(
        {"worker_id": "ats-worker-controller", "owner_id": "alice", "devices": ["ABC"]}
    )
    repository.claims.release(f"job:{job['id']}", status="expired")
    # 他人抢走物理设备：reconcile 无法安全重取 claim。
    acquired, _message = locks.lock_device(
        "ABC", "bob", "Bob", source_id="test:bob", source_type="test"
    )
    assert acquired

    stats = repository.reconcile_claims()

    assert stats["unfenceable_jobs_failed"] == 1
    assert repository.get_job(job["id"])["status"] == "failed"
    assert "fencing lost" in str(repository.get_job(job["id"]).get("error") or "")
