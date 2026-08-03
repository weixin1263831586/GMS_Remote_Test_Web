import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from features.devices.adb_proxy_service import ADBProxyService


class _ConfigManager:
    def __init__(self):
        self.runtime = {}

    def get_runtime_config(self):
        return dict(self.runtime)

    def update_runtime_config(self, updates):
        self.runtime.update(updates)
        return True


class _Repository:
    def __init__(self):
        self.workers = {
            "worker-source": {
                "id": "worker-source",
                "name": "Device Host",
                "hostname": "source",
                "address": "10.10.10.206",
                "status": "online",
                "capabilities": {
                    "adb_proxy": True,
                    "adb_proxy_version": "adb-proxy 0.4.5",
                },
            },
            "worker-target": {
                "id": "worker-target",
                "name": "Test Host",
                "hostname": "target",
                "address": "10.10.10.207",
                "status": "online",
                "capabilities": {
                    "adb_proxy": True,
                    "adb_proxy_version": "adb-proxy 0.4.5",
                },
            },
        }
        self.devices = {
            "worker-source": [{
                "serial": "RK3572GMS1",
                "state": "available",
                "transport": "local_usb",
                "properties": {"model": "RK3572"},
            }],
            "worker-target": [],
        }

    def get_worker(self, worker_id):
        return self.workers.get(worker_id)

    def list_devices(self, worker_id=""):
        if worker_id:
            return list(self.devices.get(worker_id, []))
        return [
            {**item, "worker_id": key}
            for key, values in self.devices.items()
            for item in values
        ]


@pytest.mark.asyncio
async def test_proxy_mutations_are_serialized():
    service = ADBProxyService()
    active = 0
    max_active = 0

    async def fake_connect(source, target, devices):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.01)
        active -= 1
        return {"source": source, "target": target, "devices": devices}

    with patch.object(service, "_connect", side_effect=fake_connect):
        await asyncio.gather(
            service.connect("source-1", "target", ["D1"]),
            service.connect("source-2", "target", ["D2"]),
        )

    assert max_active == 1


@pytest.mark.asyncio
async def test_logs_fall_back_to_heartbeat_summary_for_older_worker():
    repository = _Repository()
    cluster = SimpleNamespace(
        repository=repository,
        config=SimpleNamespace(local_worker_id="worker-local"),
    )
    service = ADBProxyService()
    service.observe_worker("worker-source", {
        "recent_errors": {"proxy": "backend temporarily unavailable"},
    })

    with patch("features.cluster.get_cluster_service", return_value=cluster), patch(
        "features.cluster.api._run_worker_command",
        new=AsyncMock(),
    ) as run:
        result = await service.logs("worker-source")

    run.assert_not_awaited()
    assert result["success"] is True
    assert result["supported"] is False
    assert result["proxy"] == ["backend temporarily unavailable"]


@pytest.mark.asyncio
async def test_logs_use_worker_action_when_capability_is_advertised():
    repository = _Repository()
    repository.workers["worker-source"]["capabilities"]["adb_proxy_logs"] = True
    cluster = SimpleNamespace(
        repository=repository,
        config=SimpleNamespace(local_worker_id="worker-local"),
    )
    service = ADBProxyService()
    run = AsyncMock(return_value={
        "proxy": ["proxy-line"],
        "hub": ["hub-line"],
    })

    with patch("features.cluster.get_cluster_service", return_value=cluster), patch(
        "features.cluster.api._run_worker_command",
        run,
    ):
        result = await service.logs("worker-source")

    run.assert_awaited_once_with(
        "worker-source",
        "adb_proxy",
        {"action": "logs"},
        timeout=20,
    )
    assert result["supported"] is True
    assert result["proxy"] == ["proxy-line"]


@pytest.mark.asyncio
async def test_connect_coordinates_source_then_target_without_pair_code_in_state():
    repository = _Repository()
    cluster = SimpleNamespace(
        repository=repository,
        config=SimpleNamespace(local_worker_id="worker-local"),
        effective_enabled=True,
        list_workers=lambda: list(repository.workers.values()),
    )
    service = ADBProxyService()
    service.config_manager = _ConfigManager()
    run = AsyncMock(side_effect=[
        {"running": True},
        {"connected": True},
    ])

    with patch(
        "features.cluster.get_cluster_service", return_value=cluster
    ), patch(
        "features.cluster.api._require_cluster_enabled"
    ), patch(
        "features.cluster.api._run_worker_command", run
    ), patch(
        "features.devices.adb_proxy_service.create_pair_grant",
        return_value="short-lived-grant",
    ):
        result = await service.connect(
            "worker-source",
            "worker-target",
            ["RK3572GMS1"],
        )

    assert result["success"] is True
    assert run.await_args_list[0].args[:2] == ("worker-source", "adb_proxy")
    assert run.await_args_list[0].args[2]["action"] == "source_start"
    assert run.await_args_list[0].args[2]["listen_address"] == "10.10.10.206"
    assert run.await_args_list[0].args[2]["allowed_peer_address"] == "10.10.10.207"
    assert run.await_args_list[0].args[2]["access_token"] == "short-lived-grant"
    target_payload = run.await_args_list[1].args[2]
    assert target_payload["action"] == "target_connect"
    assert target_payload["source_address"] == "10.10.10.206"
    assert target_payload["access_token"] == "short-lived-grant"
    persisted = service.config_manager.runtime["adb_proxy_assignments"]
    assignment = next(iter(persisted.values()))
    assert assignment["source_address"] == "10.10.10.206"
    assert assignment["target_name"] == "Test Host"
    assert assignment["target_address"] == "10.10.10.207"
    assert "pair_code" not in repr(persisted)
    assert "short-lived-grant" not in repr(persisted)


@pytest.mark.asyncio
async def test_connect_refuses_device_that_is_not_available():
    repository = _Repository()
    repository.devices["worker-source"][0]["state"] = "allocated"
    cluster = SimpleNamespace(
        repository=repository,
        config=SimpleNamespace(local_worker_id="worker-local"),
        effective_enabled=True,
    )
    service = ADBProxyService()
    service.config_manager = _ConfigManager()

    with patch(
        "features.cluster.get_cluster_service", return_value=cluster
    ), patch(
        "features.cluster.api._require_cluster_enabled"
    ), pytest.raises(HTTPException, match="当前不可执行ADB操作"):
        await service.connect(
            "worker-source",
            "worker-target",
            ["RK3572GMS1"],
        )


@pytest.mark.asyncio
async def test_connect_refuses_same_serial_already_attached_over_usbip():
    repository = _Repository()
    cluster = SimpleNamespace(
        repository=repository,
        config=SimpleNamespace(local_worker_id="worker-local"),
        effective_enabled=True,
    )
    service = ADBProxyService()
    service.config_manager = _ConfigManager()
    service.config_manager.runtime["usbip_cluster_assignments"] = {
        "source|1-8": {
            "worker_id": "worker-target",
            "device_serials": ["RK3572GMS1"],
            "status": "attached",
        }
    }

    with patch(
        "features.cluster.get_cluster_service", return_value=cluster
    ), patch(
        "features.cluster.api._require_cluster_enabled"
    ), pytest.raises(HTTPException, match="同序列号USB/IP设备"):
        await service.connect(
            "worker-source",
            "worker-target",
            ["RK3572GMS1"],
        )


@pytest.mark.asyncio
async def test_connect_refuses_same_serial_from_another_proxy_source():
    repository = _Repository()
    cluster = SimpleNamespace(
        repository=repository,
        config=SimpleNamespace(local_worker_id="worker-local"),
        effective_enabled=True,
    )
    service = ADBProxyService()
    service.config_manager = _ConfigManager()
    service.config_manager.runtime["adb_proxy_assignments"] = {
        "worker-other|worker-target": {
            "source_worker_id": "worker-other",
            "target_worker_id": "worker-target",
            "devices": ["RK3572GMS1"],
            "status": "connected",
        },
    }

    with patch(
        "features.cluster.get_cluster_service", return_value=cluster
    ), patch(
        "features.cluster.api._require_cluster_enabled"
    ), patch(
        "features.devices.integrations_api._usbip_assignments",
        return_value={},
    ), pytest.raises(HTTPException, match="其他ADB Proxy来源"):
        await service.connect(
            "worker-source",
            "worker-target",
            ["RK3572GMS1"],
        )


@pytest.mark.asyncio
async def test_multiple_proxy_sources_share_one_target_and_disconnect_independently():
    repository = _Repository()
    repository.workers["worker-source-2"] = {
        "id": "worker-source-2",
        "name": "Second Device Host",
        "hostname": "source-2",
        "address": "10.10.10.208",
        "status": "online",
        "capabilities": {
            "adb_proxy": True,
            "adb_proxy_version": "adb-proxy 0.4.5",
        },
    }
    repository.devices["worker-source-2"] = [{
        "serial": "ATS357629",
        "state": "available",
        "transport": "local_usb",
        "properties": {"model": "RK3576"},
    }]
    cluster = SimpleNamespace(
        repository=repository,
        config=SimpleNamespace(local_worker_id="worker-local"),
        effective_enabled=True,
        list_workers=lambda: list(repository.workers.values()),
    )
    service = ADBProxyService()
    service.config_manager = _ConfigManager()
    run = AsyncMock(side_effect=[
        {"running": True},
        {"connected": True},
        {"running": True},
        {"connected": True},
        {"connected": True},
        {"running": False},
    ])

    with patch(
        "features.cluster.get_cluster_service", return_value=cluster
    ), patch(
        "features.cluster.api._require_cluster_enabled"
    ), patch(
        "features.cluster.api._run_worker_command", run
    ), patch(
        "features.devices.adb_proxy_service.create_pair_grant",
        return_value="short-lived-grant",
    ), patch(
        "features.devices.integrations_api._usbip_assignments",
        return_value={},
    ):
        await service.connect(
            "worker-source",
            "worker-target",
            ["RK3572GMS1"],
        )
        await service.connect(
            "worker-source-2",
            "worker-target",
            ["ATS357629"],
        )
        assignments = service.assignments()
        assert set(assignments) == {
            "worker-source|worker-target",
            "worker-source-2|worker-target",
        }
        await service.disconnect("worker-source", "worker-target")

    remaining = service.assignments()
    assert set(remaining) == {"worker-source-2|worker-target"}
    assert remaining["worker-source-2|worker-target"]["devices"] == [
        "ATS357629"
    ]


@pytest.mark.asyncio
async def test_connect_adds_remaining_device_to_existing_assignment():
    repository = _Repository()
    repository.devices["worker-source"].append({
        "serial": "RK3576GMS1",
        "state": "available",
        "transport": "local_usb",
        "properties": {"model": "RK3576"},
    })
    repository.devices["worker-target"].append({
        "serial": "RK3572GMS1",
        "state": "available",
        "transport": "adb_proxy",
        "properties": {"adb_proxy_source_worker_id": "worker-source"},
    })
    cluster = SimpleNamespace(
        repository=repository,
        config=SimpleNamespace(local_worker_id="worker-local"),
        effective_enabled=True,
        list_workers=lambda: list(repository.workers.values()),
    )
    service = ADBProxyService()
    service.config_manager = _ConfigManager()
    service.config_manager.runtime["adb_proxy_assignments"] = {
        "worker-source|worker-target": {
            "source_worker_id": "worker-source",
            "source_name": "Device Host",
            "source_address": "10.10.10.206",
            "target_worker_id": "worker-target",
            "target_name": "Test Host",
            "target_address": "10.10.10.207",
            "devices": ["RK3572GMS1"],
            "status": "connected",
        }
    }
    run = AsyncMock(side_effect=[
        {"running": True},
        {"connected": True},
    ])

    with patch(
        "features.cluster.get_cluster_service", return_value=cluster
    ), patch(
        "features.cluster.api._require_cluster_enabled"
    ), patch(
        "features.cluster.api._run_worker_command", run
    ), patch(
        "features.devices.adb_proxy_service.create_pair_grant",
        return_value="short-lived-grant",
    ):
        result = await service.connect(
            "worker-source",
            "worker-target",
            ["RK3576GMS1"],
        )

    expected = ["RK3572GMS1", "RK3576GMS1"]
    assert run.await_args_list[0].args[2]["devices"] == expected
    assert run.await_args_list[1].args[2]["devices"] == expected
    assert result["assignment"]["devices"] == expected
    assert "设备：RK3572GMS1, RK3576GMS1" in result["message"]


@pytest.mark.asyncio
async def test_disconnect_stops_source_even_when_target_is_offline():
    service = ADBProxyService()
    service.config_manager = _ConfigManager()
    service.config_manager.runtime["adb_proxy_assignments"] = {
        "worker-source|worker-target": {
            "source_worker_id": "worker-source",
            "target_worker_id": "worker-target",
            "status": "connected",
        }
    }
    run = AsyncMock(side_effect=[
        RuntimeError("target offline"),
        {"running": False},
    ])

    with patch(
        "features.cluster.get_cluster_service",
        return_value=SimpleNamespace(
            repository=SimpleNamespace(get_worker=lambda _worker_id: None)
        ),
    ), patch(
        "features.cluster.api._run_worker_command", run
    ), pytest.raises(RuntimeError, match="target offline"):
        await service.disconnect("worker-source", "worker-target")

    assert run.await_args_list[1].args[:2] == ("worker-source", "adb_proxy")
    assert run.await_args_list[1].args[2]["action"] == "source_stop"
    assignment = service.config_manager.runtime[
        "adb_proxy_assignments"
    ]["worker-source|worker-target"]
    assert assignment["status"] == "disconnect_failed"


@pytest.mark.asyncio
async def test_source_only_host_can_connect_to_local_target_without_cluster_mode():
    repository = _Repository()
    repository.workers["worker-source"]["capabilities"][
        "adb_proxy_source_only"
    ] = True
    repository.workers["worker-local"] = {
        "id": "worker-local",
        "name": "Controller Local Worker",
        "hostname": "controller",
        "address": "172.16.14.233",
        "status": "online",
        "capabilities": {
            "adb_proxy": True,
            "adb_proxy_version": "adb-proxy 0.4.5",
        },
    }
    repository.devices["worker-local"] = []
    cluster = SimpleNamespace(
        repository=repository,
        config=SimpleNamespace(local_worker_id="worker-local"),
        effective_enabled=False,
        list_workers=lambda: list(repository.workers.values()),
    )
    service = ADBProxyService()
    service.config_manager = _ConfigManager()
    run = AsyncMock(side_effect=[
        {"running": True},
        {"connected": True},
    ])

    with patch(
        "features.cluster.get_cluster_service", return_value=cluster
    ), patch(
        "features.cluster.api._require_cluster_enabled"
    ) as require_cluster, patch(
        "features.cluster.api._run_worker_command", run
    ), patch(
        "features.devices.adb_proxy_service.create_pair_grant",
        return_value="short-lived-grant",
    ), patch(
        "worker_agent.adb_proxy.capability_status",
        return_value={"installed": True, "version": "adb-proxy 0.4.5"},
    ):
        status = service.status()
        result = await service.connect(
            "worker-source",
            "worker-local",
            ["RK3572GMS1"],
        )

    assert result["success"] is True
    assert {
        host["worker_id"] for host in status["hosts"]
    } == {"worker-source", "worker-local"}
    source = next(
        host for host in status["hosts"]
        if host["worker_id"] == "worker-source"
    )
    assert source["adb_proxy_source_only"] is True
    require_cluster.assert_not_called()


@pytest.mark.asyncio
async def test_connect_refuses_busy_target_before_restarting_adb():
    repository = _Repository()
    repository.workers["worker-target"].update({
        "status": "busy",
        "running_jobs": 1,
    })
    cluster = SimpleNamespace(
        repository=repository,
        config=SimpleNamespace(local_worker_id="worker-local"),
        effective_enabled=True,
    )
    service = ADBProxyService()
    service.config_manager = _ConfigManager()

    with patch(
        "features.cluster.get_cluster_service", return_value=cluster
    ), patch(
        "features.cluster.api._require_cluster_enabled"
    ), pytest.raises(HTTPException, match="正在执行测试"):
        await service.connect(
            "worker-source",
            "worker-target",
            ["RK3572GMS1"],
        )


def test_status_does_not_report_failed_or_offline_assignment_as_connected():
    repository = _Repository()
    repository.workers["worker-target"]["status"] = "offline"
    cluster = SimpleNamespace(
        repository=repository,
        config=SimpleNamespace(local_worker_id="worker-local"),
        effective_enabled=True,
        list_workers=lambda: list(repository.workers.values()),
    )
    service = ADBProxyService()
    service.config_manager = _ConfigManager()
    service.config_manager.runtime["adb_proxy_assignments"] = {
        "worker-source|worker-target": {
            "source_worker_id": "worker-source",
            "target_worker_id": "worker-target",
            "status": "connect_failed",
        }
    }

    with patch("features.cluster.get_cluster_service", return_value=cluster):
        status = service.status()

    assert status["connected"] is False
    assert status["assignments"][0]["status"] == "host_offline"


def test_status_reconciles_both_worker_processes_and_inventory():
    repository = _Repository()
    repository.devices["worker-target"] = [{
        "serial": "worker-source:RK3572GMS1",
        "state": "available",
        "transport": "adb_proxy",
        "properties": {},
    }]
    cluster = SimpleNamespace(
        repository=repository,
        config=SimpleNamespace(local_worker_id="worker-local"),
        effective_enabled=True,
        list_workers=lambda: list(repository.workers.values()),
    )
    service = ADBProxyService()
    service.config_manager = _ConfigManager()
    service.config_manager.runtime["adb_proxy_assignments"] = {
        "worker-source|worker-target": {
            "source_worker_id": "worker-source",
            "target_worker_id": "worker-target",
            "devices": ["RK3572GMS1"],
            "generation": 12,
            "status": "connected",
        }
    }
    service.observe_worker("worker-source", {
        "proxy_running": True,
        "source": {
            "running": True,
            "devices": ["RK3572GMS1"],
            "generation": 12,
        },
    })
    service.observe_worker("worker-target", {
        "hub_running": True,
        "target": {"imports": [{
            "source_worker_id": "worker-source",
            "devices": ["RK3572GMS1"],
            "generation": 12,
        }]},
    })

    with patch("features.cluster.get_cluster_service", return_value=cluster):
        status = service.status()

    assert status["connected"] is True
    assert status["assignments"][0]["status"] == "connected"


def test_status_hides_stale_generation_during_heartbeat_grace_period():
    service = ADBProxyService()
    assignment = {
        "source_worker_id": "worker-source",
        "target_worker_id": "worker-target",
        "devices": ["RK3572GMS1"],
        "generation": 12,
        "status": "connected",
        "updated_at": 1_000,
    }
    service.observe_worker("worker-source", {
        "proxy_running": True,
        "source": {
            "running": True,
            "devices": ["RK3572GMS1"],
            "generation": 11,
        },
    })
    service.observe_worker("worker-target", {
        "hub_running": True,
        "target": {"imports": [{
            "source_worker_id": "worker-source",
            "devices": ["RK3572GMS1"],
            "generation": 11,
        }]},
    })

    with patch("features.devices.adb_proxy_service.time.time", return_value=1_010):
        reconciled = service._reconciled_status(assignment, [])

    assert reconciled == "recovering"


def test_status_reports_stale_generation_after_heartbeat_grace_period():
    service = ADBProxyService()
    assignment = {
        "source_worker_id": "worker-source",
        "target_worker_id": "worker-target",
        "devices": ["RK3572GMS1"],
        "generation": 12,
        "status": "connected",
        "updated_at": 1_000,
    }
    service.observe_worker("worker-source", {
        "proxy_running": True,
        "source": {
            "running": True,
            "devices": ["RK3572GMS1"],
            "generation": 11,
        },
    })
    service.observe_worker("worker-target", {
        "hub_running": True,
        "target": {"imports": [{
            "source_worker_id": "worker-source",
            "devices": ["RK3572GMS1"],
            "generation": 11,
        }]},
    })

    with patch("features.devices.adb_proxy_service.time.time", return_value=1_040):
        reconciled = service._reconciled_status(assignment, [])

    assert reconciled == "degraded_source"


@pytest.mark.asyncio
async def test_connect_rejects_worker_with_legacy_default_allow_proxy():
    repository = _Repository()
    repository.workers["worker-source"]["capabilities"][
        "adb_proxy_version"
    ] = "adb-proxy 0.4.4"
    cluster = SimpleNamespace(
        repository=repository,
        config=SimpleNamespace(local_worker_id="worker-local"),
        effective_enabled=True,
        list_workers=lambda: list(repository.workers.values()),
    )
    service = ADBProxyService()
    service.config_manager = _ConfigManager()

    with patch(
        "features.cluster.get_cluster_service", return_value=cluster
    ), pytest.raises(HTTPException, match=r"请升级到 0\.4\.5"):
        await service.connect(
            "worker-source",
            "worker-target",
            ["RK3572GMS1"],
        )

    with patch("features.cluster.get_cluster_service", return_value=cluster):
        status = service.status()
    source = next(
        item for item in status["hosts"]
        if item["worker_id"] == "worker-source"
    )
    assert source["adb_proxy"] is False
