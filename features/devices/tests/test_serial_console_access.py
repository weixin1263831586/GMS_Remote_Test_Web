from types import SimpleNamespace
from unittest.mock import patch

import pytest

from features.devices import serial_console_access


def test_availability_confidence_distinguishes_present_open_and_output():
    worker_id = "ats-worker-controller"
    base = {
        "port_key": "p1",
        "online": True,
        "binding": {"device_id": "D1", "worker_id": worker_id},
    }
    with patch.object(
        serial_console_access, "get_local_worker_id", return_value=worker_id
    ):
        present = serial_console_access.device_serial_availability(
            [base], device_id="D1", worker_id=worker_id
        )
        opened = serial_console_access.device_serial_availability(
            [{**base, "capture_active": True}], device_id="D1", worker_id=worker_id
        )
        output = serial_console_access.device_serial_availability(
            [{**base, "capture_active": True, "last_output_at": "now"}],
            device_id="D1",
            worker_id=worker_id,
        )
    assert present["confidence"] == "explicit_binding"
    assert opened["confidence"] == "open_verified"
    assert output["confidence"] == "output_verified"


def test_writer_claim_fences_the_bound_device():
    user = SimpleNamespace(
        resource_owner_id="owner-1", username="operator", actor_id="user-1"
    )
    port = {
        "port_key": "p1",
        "binding": {
            "device_id": "D1",
            "worker_id": "ats-worker-controller",
            "identity_verified": True,
        },
    }
    record = {"device_key": "ats-worker-controller:D1", "id": "claim-1"}
    with patch.object(
        serial_console_access.device_lock_manager,
        "lock_devices",
        return_value=(True, [record]),
    ) as lock_devices, patch.object(
        serial_console_access, "audit_console_event"
    ), patch.object(
        serial_console_access,
        "get_local_worker_id",
        return_value="ats-worker-controller",
    ):
        claim = serial_console_access.acquire_writer_claim(user, port)
    assert claim.device_id == "D1"
    assert claim.device_key == "ats-worker-controller:D1"
    assert lock_devices.call_args.kwargs["allow_existing_source"] is False


def test_writer_claim_rejects_legacy_unverified_binding():
    with patch.object(
        serial_console_access,
        "get_local_worker_id",
        return_value="ats-worker-controller",
    ), pytest.raises(RuntimeError, match="重新绑定"):
        serial_console_access.acquire_writer_claim(
            None,
            {
                "port_key": "p1",
                "binding": {
                    "device_id": "D1",
                    "worker_id": "ats-worker-controller",
                    "identity_verified": False,
                },
            },
        )
