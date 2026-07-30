import tempfile
from pathlib import Path
from unittest.mock import patch

from starlette.requests import Request

from features.auth import CurrentUser
from features.devices import operation_claims, support
from features.devices.locks import DeviceLockManager


def authenticated_request(user_id: str, username: str) -> Request:
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
        role="device_operator",
    )
    return request


def test_dynamic_operation_claim_is_atomic_and_fenced():
    with tempfile.TemporaryDirectory() as directory:
        manager = DeviceLockManager(
            Path(directory) / "claims.sqlite3",
            local_worker_id="worker-local",
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
                "device_id": "worker-local:SERIAL-1",
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


def test_lock_status_resolves_internal_owner_to_user_management_identity():
    with tempfile.TemporaryDirectory() as directory:
        manager = DeviceLockManager(
            Path(directory) / "claims.sqlite3",
            local_worker_id="worker-local",
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
