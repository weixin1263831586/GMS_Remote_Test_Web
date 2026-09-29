from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from features.firmware import usbip_transport


@pytest.mark.asyncio
async def test_release_matches_host_and_busid_as_one_route_target():
    routes = [
        {"device_host": "192.0.2.10", "busids": ["1-1"], "device_ids": []},
        {"device_host": "192.0.2.20", "busids": ["2-2"], "device_ids": []},
    ]
    initial_entries = [
        {"host": "192.0.2.10", "busid": "1-1", "port": "1"},
        # Host and busid each occur in routes, but never as this pair.
        {"host": "192.0.2.10", "busid": "2-2", "port": "9"},
    ]
    command_results = [
        SimpleNamespace(ok=True, stdout="initial", stderr=""),
        SimpleNamespace(ok=True, stdout="", stderr=""),
        SimpleNamespace(ok=True, stdout="verified", stderr=""),
    ]

    execute = MagicMock(side_effect=command_results)
    ssh_manager = SimpleNamespace(execute_command=execute)
    with patch.object(
        usbip_transport.runtime, "ssh_manager", ssh_manager
    ), patch(
        "features.devices.parse_usbip_port_entries",
        side_effect=[initial_entries, []],
    ), patch(
        "features.devices.resolve_usbip_command", return_value="usbip"
    ), patch.object(usbip_transport, "pause_usbip_reconnect"), patch.object(
        usbip_transport.asyncio, "sleep"
    ):
        released, error = await usbip_transport.release_usbip_devices_to_source(
            object(), routes
        )

    assert released is True
    assert error == ""
    detach_commands = [
        call.args[1] for call in execute.call_args_list if " detach " in call.args[1]
    ]
    assert detach_commands == ["sudo -n usbip detach -p 1"]
