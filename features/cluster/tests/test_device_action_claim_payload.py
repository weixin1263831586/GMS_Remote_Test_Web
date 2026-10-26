"""Mixed borrowed/new targets must each carry a fencing token."""

import pytest
from fastapi import HTTPException

from features.cluster.operation_claims import device_action_claim_payload
from features.cluster.repository import ClusterRepository
from worker_agent.config import WorkerConfig
from worker_agent.runtime import WorkerRuntime


def test_mixed_claims_cover_all_devices_and_only_release_new_claims(tmp_path):
    repository = ClusterRepository(tmp_path / "cluster.sqlite3")
    repository.acquire_device_operation_claim(
        "worker", ["worker:A"], owner_id="owner", source_type="cluster-reservation",
        source_id="reservation:original", ttl_seconds=3600,
    )
    payload = device_action_claim_payload(
        repository, "worker", ["worker:A", "worker:B"], "operation1", "owner",
    )
    assert {token["device_id"] for token in payload["lease_tokens"]} == {"worker:A", "worker:B"}
    assert payload["release_claim_on_terminal"] is True
    assert repository.claims.release(payload["claim_source_id"]) == 1
    assert repository.claims.active_claim("worker:A")["source_id"] == "reservation:original"
    assert repository.claims.active_claim("worker:B") is None


def test_concurrent_operation_claim_is_not_borrowed(tmp_path):
    repository = ClusterRepository(tmp_path / "cluster.sqlite3")
    device_action_claim_payload(repository, "worker", ["worker:A"], "operation1", "owner")
    with pytest.raises(HTTPException) as caught:
        device_action_claim_payload(repository, "worker", ["worker:A"], "operation2", "owner")
    assert caught.value.status_code == 409


def test_running_cluster_job_claim_is_not_borrowed_by_same_owner(tmp_path):
    """cluster 侧设备操作同样不得借用运行中 cluster-job 的 claim。"""
    repository = ClusterRepository(tmp_path / "cluster.sqlite3")
    repository.acquire_device_operation_claim(
        "worker", ["worker:A"], owner_id="owner", source_type="cluster-job",
        source_id="job:job-1", ttl_seconds=3600,
    )
    with pytest.raises(HTTPException) as caught:
        device_action_claim_payload(
            repository, "worker", ["worker:A"], "operation1", "owner",
        )
    assert caught.value.status_code == 409
    assert "already claimed" in str(caught.value.detail)
    assert repository.claims.active_claim("worker:A")["source_id"] == "job:job-1"


@pytest.mark.parametrize("reservation", [False, True])
def test_proxy_claim_payload_passes_worker_fencing_and_keeps_physical_lock(tmp_path, reservation):
    repository = ClusterRepository(tmp_path / "cluster.sqlite3", claim_db_path=tmp_path / "claims.sqlite3")
    for worker in ("proxy", "source"):
        repository.register_worker({"worker_id": worker, "name": worker, "hostname": worker,
                                    "address": "127.0.0.1", "agent_version": "1", "max_jobs": 2})
    repository.heartbeat("source", {"devices": [{"serial": "PHYSICAL", "state": "available"}],
                                     "running_jobs": [], "suites": []})
    repository.heartbeat("proxy", {"devices": [{
        "serial": "PROXY", "state": "available", "transport": "adb_proxy",
        "properties": {"adb_proxy_source_worker_id": "source", "adb_proxy_source_serial": "PHYSICAL"},
    }], "running_jobs": [], "suites": []})
    if reservation:
        repository.reserve_devices("proxy", ["proxy:PROXY"], owner_id="owner", source_id="run")
    payload = device_action_claim_payload(repository, "proxy", ["proxy:PROXY"], "operation", "owner")
    payload["devices"] = ["proxy:PROXY"]
    assert {token["device_id"] for token in payload["lease_tokens"]} == {"proxy:PROXY"}
    runtime = WorkerRuntime(WorkerConfig(worker_id="proxy", controller_url="http://127.0.0.1",
                                         token="synthetic", data_root=tmp_path / "worker"))
    runtime.validate_fencing({"command_type": "device_action", "payload": payload})
    assert repository.claims.active_claim("source:PHYSICAL") is not None
    with pytest.raises(ValueError, match="already claimed"):
        repository.acquire_device_operation_claim("source", ["source:PHYSICAL"], owner_id="other",
                                                   source_type="operation", source_id="other")
    foreign = {**payload["lease_tokens"][0], "device_id": "source:UNRELATED"}
    with pytest.raises(ValueError, match="do not match"):
        runtime.validate_fencing({"command_type": "device_action", "payload": {
            **payload, "lease_tokens": [*payload["lease_tokens"], foreign],
        }})
