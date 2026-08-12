from __future__ import annotations

import asyncio
import json
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from features.auth import CurrentUser
from features.devices.adb_proxy_service import ADBProxyService
from features.devices.models import USBIPDisconnectRequest
from features.devices.usbip_operations import (
    USBIPOperationGate,
    serialize_usbip_operation,
)


def _request() -> SimpleNamespace:
    return SimpleNamespace(headers={}, client=SimpleNamespace(host="127.0.0.1"))


@pytest.mark.asyncio
async def test_usbip_operation_gate_rejects_same_source_but_allows_other_source():
    gate = USBIPOperationGate()
    entered = asyncio.Event()
    release = asyncio.Event()

    @serialize_usbip_operation(gate=gate)
    async def guarded(request, req=None):
        entered.set()
        await release.wait()
        return SimpleNamespace(status_code=200)

    source_a = USBIPDisconnectRequest(device_host="user@10.0.0.1")
    source_b = USBIPDisconnectRequest(device_host="user@10.0.0.2")
    first = asyncio.create_task(guarded(request=_request(), req=source_a))
    await entered.wait()

    duplicate = await guarded(request=_request(), req=source_a)
    other = asyncio.create_task(guarded(request=_request(), req=source_b))
    await asyncio.sleep(0)

    assert duplicate.status_code == 409
    duplicate_body = json.loads(duplicate.body.decode("utf-8"))
    assert duplicate_body["error_code"] == "USBIP_OPERATION_IN_PROGRESS"
    assert not other.done()

    release.set()
    assert (await first).status_code == 200
    assert (await other).status_code == 200


@pytest.mark.asyncio
async def test_usbip_operation_gate_releases_source_after_failure():
    gate = USBIPOperationGate()
    attempts = 0

    @serialize_usbip_operation(gate=gate)
    async def guarded(request, req=None):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("attach failed")
        return SimpleNamespace(status_code=200)

    model = USBIPDisconnectRequest(device_host="user@10.0.0.1")
    with pytest.raises(RuntimeError, match="attach failed"):
        await guarded(request=_request(), req=model)
    assert (await guarded(request=_request(), req=model)).status_code == 200


def test_adb_proxy_disconnect_fails_closed_when_claim_inventory_is_unavailable():
    repository = SimpleNamespace(
        list_devices=MagicMock(side_effect=RuntimeError("database unavailable"))
    )
    cluster = SimpleNamespace(repository=repository)

    with patch("features.cluster.get_cluster_service", return_value=cluster), pytest.raises(
        HTTPException,
        match="设备占用状态",
    ) as raised:
        ADBProxyService._require_proxy_devices_not_claimed(
            "worker-target",
            {"SERIAL-1"},
        )

    assert raised.value.status_code == 503


@pytest.mark.asyncio
async def test_adb_proxy_empty_device_values_are_rejected_as_empty_selection():
    repository = SimpleNamespace(
        get_worker=lambda worker_id: {
            "id": worker_id,
            "address": "10.0.0.1" if worker_id == "source" else "10.0.0.2",
            "status": "online",
            "capabilities": {"adb_proxy": True, "adb_proxy_version": "adb-proxy 0.4.5"},
        },
        list_devices=lambda _worker_id="": [],
    )
    cluster = SimpleNamespace(
        repository=repository,
        config=SimpleNamespace(local_worker_id="controller"),
        effective_enabled=True,
    )
    service = ADBProxyService()

    with patch("features.cluster.get_cluster_service", return_value=cluster), patch(
        "features.cluster.api._require_cluster_enabled"
    ), patch.object(
        service,
        "_require_idle_target",
    ), pytest.raises(HTTPException, match="至少选择一个ADB设备") as raised:
        await service.connect("source", "target", ["", "  "])

    assert raised.value.status_code == 400


@pytest.mark.asyncio
async def test_local_usbip_partial_disconnect_preserves_sibling_assignment_and_state():
    import features.cluster as cluster_module
    import features.devices.integrations_api as integrations

    device_host = "user@10.0.0.1"
    runtime_config = {
        "usbip_devices_source": {
            "SERIAL-1": {"source": device_host},
            "SERIAL-2": {"source": device_host},
        },
        "usbip_cluster_assignments": {
            f"{device_host}|1-1": {
                "device_host": device_host,
                "worker_id": "controller",
                "busid": "1-1",
                "device_serials": ["SERIAL-1"],
                "status": "attached",
            },
            f"{device_host}|1-2": {
                "device_host": device_host,
                "worker_id": "controller",
                "busid": "1-2",
                "device_serials": ["SERIAL-2"],
                "status": "attached",
            },
        },
    }

    class ConfigManager:
        def load_config(self):
            return {"device_host": device_host, "device_pswd": "secret"}

        def get_runtime_config(self):
            return dict(runtime_config)

        def update_runtime_config(self, updates):
            runtime_config.update(updates)
            return True

        def save_runtime_config(self, data):
            runtime_config.clear()
            runtime_config.update(data)
            return True

    class DeviceConnection:
        def __init__(self, _config):
            pass

        def __enter__(self):
            return "windows-ssh"

        def __exit__(self, *_args):
            return False

    ssh_manager = SimpleNamespace(
        get_connection=lambda _config: "ubuntu-ssh",
        return_connection=lambda _ssh: None,
        execute_command=lambda *_args, **_kwargs: ("", "", 0),
    )
    fake_cluster = SimpleNamespace(
        config=SimpleNamespace(local_worker_id="controller")
    )
    clear_sources = MagicMock()
    previous_state = {
        "connected": True,
        "transport_connected": True,
        "adb_ready": True,
        "protocol_status": {"mode": "adb"},
    }
    global_state = SimpleNamespace(
        usbip_states={device_host: dict(previous_state)},
        usbip_states_lock=threading.RLock(),
        usbip_devices_source={},
        usbip_devices_source_lock=threading.RLock(),
        device_cache={"devices": [], "timestamp": 0},
        device_cache_lock=threading.RLock(),
    )

    with patch.object(integrations.runtime, "global_state", global_state), patch.object(
        integrations.runtime, "config_manager", ConfigManager()
    ), patch.object(
        integrations.runtime, "ssh_manager", ssh_manager
    ), patch.object(
        integrations.runtime, "get_client_id_from_request", return_value=device_host
    ), patch.object(
        integrations.runtime, "resolve_tailscale_device_host", return_value=(None, None)
    ), patch.object(
        cluster_module, "get_cluster_service", return_value=fake_cluster
    ), patch.object(
        integrations, "DeviceSSHConnection", DeviceConnection
    ), patch.object(
        integrations,
        "_detach_ubuntu_usbip_for_devices",
        return_value={"detached_ports": ["00"], "remaining_devices": []},
    ) as detach, patch.object(
        integrations, "_clear_usbip_device_sources", clear_sources
    ), patch.object(
        integrations, "notify_device_change", AsyncMock()
    ), patch.object(
        integrations,
        "acquire_device_operation_claim",
        return_value=("", [], None),
    ), patch.object(
        integrations, "release_device_operation_claim"
    ), patch.object(
        integrations, "audit_device_operation"
    ), patch("features.devices.reconnect.stop_usbip_reconnect_for_host"):
        response = await integrations.stop_usbip(
            request=_request(),
            req=USBIPDisconnectRequest(
                device_host=device_host,
                worker_id="controller",
                busids=["1-1"],
            ),
            _elevated=CurrentUser(id="admin", username="admin", role="admin"),
        )

    assert response.status_code == 200
    assert detach.call_args.kwargs["devices_to_remove"] == ["SERIAL-1"]
    clear_sources.assert_called_once_with(device_host, ["SERIAL-1"])
    assert f"{device_host}|1-1" not in runtime_config["usbip_cluster_assignments"]
    assert f"{device_host}|1-2" in runtime_config["usbip_cluster_assignments"]
    with global_state.usbip_states_lock:
        assert global_state.usbip_states[device_host] == previous_state
