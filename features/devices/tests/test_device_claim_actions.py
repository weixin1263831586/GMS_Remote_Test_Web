from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from features.auth import CurrentUser
from features.devices import (
    config_explorer_api,
    config_override_api,
    device_lock_manager,
    operations_api,
    ui_control_api,
)
from features.devices.models import DeviceActionRequest
from features.devices.ui_control_api import UiTapRequest


DEVICE_ID = "CLAIM-ACTION-DEVICE"


def _request(username: str) -> Request:
    request = Request({
        "type": "http",
        "method": "POST",
        "path": "/",
        "query_string": b"",
        "headers": [],
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 80),
        "scheme": "http",
    })
    request.state.current_user = CurrentUser(
        id=f"id-{username}", username=username, role="user"
    )
    return request


@pytest.fixture(autouse=True)
def active_test_claim():
    device_lock_manager.force_unlock_device(DEVICE_ID)
    acquired, _message = device_lock_manager.lock_device(
        DEVICE_ID,
        "alice",
        "Alice",
        source_id="test:alice",
        source_type="test",
    )
    assert acquired
    yield
    device_lock_manager.registry.release("test:alice", status="cancelled")


def test_reboot_rejects_claimed_device_before_side_effect():
    with patch.object(
        operations_api.runtime,
        "get_client_id_from_request",
        return_value="bob",
    ), patch.object(operations_api.device_manager, "reboot_device") as reboot:
        response = asyncio.run(operations_api.reboot_devices(
            DeviceActionRequest(devices=[DEVICE_ID]), _request("bob")
        ))

    assert response.status_code == 409
    reboot.assert_not_called()


def test_ui_tap_rejects_even_the_active_claim_owner():
    with patch.object(
        ui_control_api.runtime,
        "get_client_id_from_request",
        return_value="alice",
    ), patch.object(ui_control_api.runtime, "config_manager") as config:
        response = asyncio.run(ui_control_api.ui_tap(
            UiTapRequest(serial=DEVICE_ID, x=10, y=20), _request("alice")
        ))

    assert response.status_code == 409
    config.load_config.assert_not_called()


def test_config_explorer_allows_owner_but_rejects_other_user():
    with patch.object(
        config_explorer_api,
        "get_client_id_from_request",
        side_effect=lambda request: request.state.current_user.username,
    ), patch.object(
        config_explorer_api, "list_packages", return_value=["android"]
    ) as packages:
        owner_response = asyncio.run(config_explorer_api.api_list_packages(
            _request("alice"), device_id=DEVICE_ID, help=False
        ))
        with pytest.raises(HTTPException) as denied:
            asyncio.run(config_explorer_api.api_list_packages(
                _request("bob"), device_id=DEVICE_ID, help=False
            ))

    assert owner_response.status_code == 200
    assert denied.value.status_code == 409
    packages.assert_called_once_with(DEVICE_ID)


def test_config_override_mutation_rejects_active_claim_before_reboot():
    with patch.object(
        config_override_api,
        "get_client_id_from_request",
        return_value="bob",
    ), patch.object(config_override_api, "reboot_device") as reboot:
        response = asyncio.run(config_override_api.api_reboot(
            _request("bob"), device_id=DEVICE_ID
        ))

    assert response.status_code == 409
    reboot.assert_not_called()
