"""Mixed borrowed/new targets must each carry a fencing token."""

import pytest
from fastapi import HTTPException

from features.cluster.operation_claims import device_action_claim_payload
from features.cluster.repository import ClusterRepository


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
