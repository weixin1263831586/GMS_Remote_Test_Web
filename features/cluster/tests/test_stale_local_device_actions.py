"""重启孤儿本地 device_action 回收（fail_stale_local_device_actions）。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from features.cluster import ClusterRepository


WORKER_ID = "ats-worker-controller"


def _repository(root: Path) -> ClusterRepository:
    repository = ClusterRepository(
        root / "cluster.sqlite3",
        claim_db_path=root / "device_claims.sqlite3",
    )
    repository.register_worker({
        "worker_id": WORKER_ID, "name": "local", "hostname": "localhost",
        "address": "127.0.0.1", "agent_version": "1", "max_jobs": 1,
        "capabilities": {},
    })
    return repository


def _create_local_aoa_command(
    repository: ClusterRepository,
    operation_id: str,
    *,
    with_claim_payload: bool = True,
) -> dict:
    payload = {
        "action": "aoa_sequence",
        "owner_id": "gms",
        "request_sha256": "ab" * 32,
        "devices": ["RK3576GMS1"],
    }
    if with_claim_payload:
        payload.update({
            "release_claim_on_terminal": True,
            "claim_source_id": f"operation:{operation_id}",
        })
    return repository.create_command({
        "worker_id": WORKER_ID,
        "command_type": "device_action",
        "operation_id": operation_id,
        "initial_status": "accepted",
        "payload": payload,
    })


def _age_command(repository: ClusterRepository, command_id: str, seconds: int) -> None:
    stale = (
        datetime.now(timezone.utc) - timedelta(seconds=seconds)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    with repository.connect() as conn:
        conn.execute(
            "UPDATE cluster_commands SET updated_at=? WHERE id=?",
            (stale, command_id),
        )


def test_orphaned_local_aoa_command_fails_and_releases_claim(tmp_path):
    repository = _repository(tmp_path)
    command = _create_local_aoa_command(repository, "op-orphan-1")
    repository.start_local_command(command["id"])
    repository.acquire_device_operation_claim(
        WORKER_ID, ["RK3576GMS1"],
        owner_id="gms", source_type="cluster-device-action",
        source_id="operation:op-orphan-1", username="gms",
    )
    # 把 updated_at 拨回超过回收窗口。
    _age_command(repository, command["id"], 10_000)

    failed = repository.fail_stale_local_device_actions()

    assert [item["command_id"] for item in failed] == [command["id"]]
    current = repository.get_command(command["id"])
    assert current["status"] == "failed"
    assert "restart" in (current.get("error") or "")
    assert repository.claims.active_claim(
        "ats-worker-controller:RK3576GMS1") is None


def test_fresh_running_local_command_is_not_touched(tmp_path):
    repository = _repository(tmp_path)
    command = _create_local_aoa_command(repository, "op-live-1")
    repository.start_local_command(command["id"])
    repository.acquire_device_operation_claim(
        WORKER_ID, ["RK3576GMS1"],
        owner_id="gms", source_type="cluster-device-action",
        source_id="operation:op-live-1", username="gms",
    )

    assert repository.fail_stale_local_device_actions() == []
    assert repository.get_command(command["id"])["status"] == "running"
    assert repository.claims.active_claim(
        "ats-worker-controller:RK3576GMS1") is not None


def test_command_without_local_claim_signature_is_ignored(tmp_path):
    repository = _repository(tmp_path)
    command = _create_local_aoa_command(
        repository, "op-foreign-1", with_claim_payload=False)
    repository.start_local_command(command["id"])
    _age_command(repository, command["id"], 10_000)

    assert repository.fail_stale_local_device_actions() == []
    assert repository.get_command(command["id"])["status"] == "running"


def test_terminal_local_commands_are_never_revisited(tmp_path):
    repository = _repository(tmp_path)
    command = _create_local_aoa_command(repository, "op-done-1")
    repository.start_local_command(command["id"])
    repository.ack_command(WORKER_ID, command["id"], {
        "status": "completed", "result": {"success": True},
    })
    _age_command(repository, command["id"], 10_000)

    assert repository.fail_stale_local_device_actions() == []
    assert repository.get_command(command["id"])["status"] == "completed"


def test_payload_json_corruption_is_tolerated(tmp_path):
    repository = _repository(tmp_path)
    command = _create_local_aoa_command(repository, "op-corrupt-1")
    repository.start_local_command(command["id"])
    _age_command(repository, command["id"], 10_000)
    with repository.connect() as conn:
        conn.execute(
            "UPDATE cluster_commands SET payload_json='{not-json' WHERE id=?",
            (command["id"],),
        )

    assert repository.fail_stale_local_device_actions() == []
    assert repository.get_command(command["id"])["status"] == "running"
    # JSON 损坏时保守不动（识别签名缺失视为非本地路径）。


def test_accepted_stale_local_command_is_also_swept(tmp_path):
    """派发即孤儿（start_local_command 未生效的 accepted 行）同样被回收。"""
    repository = _repository(tmp_path)
    command = _create_local_aoa_command(repository, "op-accepted-1")
    repository.acquire_device_operation_claim(
        WORKER_ID, ["RK3576GMS1"],
        owner_id="gms", source_type="cluster-device-action",
        source_id="operation:op-accepted-1", username="gms",
    )
    _age_command(repository, command["id"], 10_000)

    failed = repository.fail_stale_local_device_actions()

    assert [item["command_id"] for item in failed] == [command["id"]]
    assert repository.get_command(command["id"])["status"] == "failed"
    assert repository.claims.active_claim(
        "ats-worker-controller:RK3576GMS1") is None


def test_double_sweep_is_idempotent(tmp_path):
    """两进程先后 sweep：第二次不得重复记账或重复释放。"""
    first = _repository(tmp_path)
    second = ClusterRepository(
        tmp_path / "cluster.sqlite3",
        claim_db_path=tmp_path / "device_claims.sqlite3",
    )
    command = _create_local_aoa_command(first, "op-twice-1")
    first.acquire_device_operation_claim(
        WORKER_ID, ["RK3576GMS1"],
        owner_id="gms", source_type="cluster-device-action",
        source_id="operation:op-twice-1", username="gms",
    )
    _age_command(first, command["id"], 10_000)

    assert [item["command_id"] for item in first.fail_stale_local_device_actions()] == [command["id"]]
    assert second.fail_stale_local_device_actions() == []
    assert second.get_command(command["id"])["status"] == "failed"
