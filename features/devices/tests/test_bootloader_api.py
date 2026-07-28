from __future__ import annotations

import json

from features.devices import bootloader_api


def test_adb_ready_requires_exact_successful_device_state() -> None:
    assert bootloader_api._adb_state_is_ready("device\n", 0)
    assert not bootloader_api._adb_state_is_ready("error: device not found\n", 0)
    assert not bootloader_api._adb_state_is_ready("device\n", 1)


def test_failed_bootloader_result_is_not_reported_as_success() -> None:
    response = bootloader_api._bootloader_operation_response(
        [
            {
                "device": "RK3572GMS1",
                "success": False,
                "error": "Bootloader remains locked",
            }
        ],
        "unlock",
    )
    payload = json.loads(response.body)

    assert response.status_code == 200
    assert payload["success"] is False
    assert payload["data"]["summary"]["failed"] == 1
    assert "Bootloader remains locked" in payload["error"]
