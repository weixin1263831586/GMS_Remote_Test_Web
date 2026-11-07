"""Management lists must not wait for ADB when a fresh Worker snapshot exists."""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from starlette.requests import Request

from features.auth import CurrentUser
from features.devices import management_api, management_inventory
from foundation.command_result import CommandResult


@pytest.fixture
def cluster(monkeypatch):
    observed = datetime.now(timezone.utc).isoformat()
    repo = SimpleNamespace(
        get_worker=Mock(return_value={'status': 'online', 'last_heartbeat_at': observed}),
        list_devices=Mock(return_value=[
            {'worker_id': 'local', 'serial': 'LOCAL', 'state': 'available',
             'properties': {'model': 'Box', 'android_version': '16', 'battery_level': '90', 'soc_model': 'RK3562'}},
            {'worker_id': 'local', 'serial': 'BOOT', 'state': 'fastboot', 'properties': {}},
            {'worker_id': 'remote', 'serial': 'OTHER', 'state': 'available', 'properties': {}},
            {'worker_id': 'local', 'serial': 'GONE', 'state': 'offline', 'properties': {}},
            {'worker_id': 'local', 'serial': 'UNAUTHORIZED', 'state': 'unauthorized', 'properties': {}},
        ]),
    )
    service = SimpleNamespace(config=SimpleNamespace(local_worker_id='local'), repository=repo, offline_seconds=45)
    monkeypatch.setattr(management_inventory, 'get_cluster_service', lambda: service)
    return service


def test_fresh_inventory_preserves_properties_protocol_and_worker_target(cluster):
    devices, properties, protocols, _ = management_inventory.local_management_inventory()
    assert devices == ['LOCAL', 'BOOT']
    assert properties['LOCAL']['android_version'] == '16'
    assert protocols == {'LOCAL': 'adb', 'BOOT': 'fastboot'}
    cluster.repository.list_devices.assert_called_once_with('local')


def test_usb_only_aoa_target_is_preserved_but_not_reported_as_adb(cluster):
    cluster.repository.list_devices.return_value.append({
        'worker_id': 'local', 'serial': 'USB-OFF', 'state': 'usb_only',
        'properties': {'model': 'User build'},
    })

    devices, properties, protocols, _ = management_inventory.local_management_inventory()

    assert 'USB-OFF' in devices
    assert properties['USB-OFF']['model'] == 'User build'
    assert protocols['USB-OFF'] == 'usb_only'


def test_workspace_usb_only_inventory_uses_fresh_local_worker_facts(cluster, monkeypatch):
    from features.devices import runtime

    monkeypatch.setattr(runtime.get_runtime(), 'config_manager', SimpleNamespace(
        load_config=lambda: {'ubuntu_host': '127.0.0.1'}))
    cluster.repository.list_devices.return_value.append({
        'worker_id': 'local', 'serial': 'USB-OFF', 'state': 'usb_only', 'properties': {},
    })
    observed = management_inventory.local_usb_only_inventory()
    assert set(observed) == {'USB-OFF'}
    assert observed['USB-OFF']['protocol'] == observed['USB-OFF']['status'] == 'usb_only'
    cluster.repository.get_worker.return_value = {'status': 'offline'}
    assert management_inventory.local_usb_only_inventory() == {}
    assert observed['USB-OFF']['protocol'] == observed['USB-OFF']['status'] == 'usb_only'
    cluster.repository.get_worker.return_value = {'status': 'offline'}
    assert management_inventory.local_usb_only_inventory() == {}


def test_workspace_does_not_mix_controller_usb_with_remote_legacy_adb_host(cluster, monkeypatch):
    from features.devices import runtime

    monkeypatch.setattr(runtime.get_runtime(), 'config_manager', SimpleNamespace(
        load_config=lambda: {'ubuntu_host': '192.0.2.10'}))
    monkeypatch.setattr(management_inventory, 'is_local_host', lambda _: False)
    assert management_inventory.local_usb_only_inventory() == {}
    cluster.repository.list_devices.assert_not_called()


@pytest.mark.parametrize('worker', [None, {'status': 'offline'},
                                  {'status': 'online', 'last_heartbeat_at': 'bad'},
                                  {'status': 'online', 'last_heartbeat_at':
                                   (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()}])
def test_unavailable_or_stale_worker_uses_existing_fallback(cluster, worker):
    cluster.repository.get_worker.return_value = worker
    assert management_inventory.local_management_inventory() is None
    cluster.repository.list_devices.assert_not_called()


def test_fresh_empty_snapshot_falls_back_to_live_probes(cluster):
    """空心跳不可当权威：返回 None 让端点走本机实时探针。"""
    cluster.repository.list_devices.return_value = []
    assert management_inventory.local_management_inventory() is None
    # 全部条目被过滤（offline/unauthorized）同样视为空清单。
    cluster.repository.list_devices.return_value = [
        {'worker_id': 'local', 'serial': 'GONE', 'state': 'offline', 'properties': {}},
    ]
    assert management_inventory.local_management_inventory() is None


def test_endpoint_bypasses_live_probes_and_sanitizes_current_ownership(cluster, monkeypatch):
    principal = CurrentUser(id='alice', username='alice', role='user')
    request = Request({'type': 'http', 'method': 'GET', 'path': '/', 'headers': [], 'client': ('127.0.0.1', 1)})
    request.state.current_user = principal
    config = SimpleNamespace(load_config=lambda: {}, get_ubuntu_host=lambda _: '127.0.0.1', get_ubuntu_user=lambda _: 'tester')
    monkeypatch.setattr(management_api.runtime.get_runtime(), 'config_manager', config)
    monkeypatch.setattr(management_api.runtime.get_runtime(), 'get_client_id_from_request', lambda _: 'alice')
    monkeypatch.setattr(management_api, 'require_authenticated_user_when_auth_required', lambda _: principal)
    monkeypatch.setattr(management_api, 'known_usbip_sources', lambda: {})
    monkeypatch.setattr(management_api, '_local_adb_proxy_sources', lambda _: {})
    monkeypatch.setattr('features.users.auto_assign_new_devices', lambda *_: [])
    monkeypatch.setattr('features.users.build_device_group_map', lambda *_: {})
    monkeypatch.setattr(management_api.device_lock_manager, 'get_all_locks', lambda: {
        'LOCAL': {'client_id': 'bob', 'username': 'bob', 'lease_id': 'private-lease', 'generation': 7},
    })
    probe = Mock(side_effect=AssertionError('A list request must not probe devices'))
    monkeypatch.setattr(management_api.runtime.get_runtime(), 'run_local_shell_command', probe)
    monkeypatch.setattr(management_api.device_manager, 'get_fastboot_devices', probe)
    monkeypatch.setattr(management_api, 'has_blocked_adb_process', probe)
    payload = json.loads(asyncio.run(management_api.devices_management(request)).body)
    assert payload['source'] == 'worker-heartbeat'
    probe.assert_not_called()
    device = payload['devices'][0]
    assert device['model'] == 'Box' and device['android_version'] == '16'
    assert device['locked_by'] == 'occupied'
    assert device['locked_client_id'] == device['lease_id'] == ''
    assert device['lease_generation'] == 0


def test_endpoint_empty_heartbeat_falls_back_to_live_probes(cluster, monkeypatch):
    """空心跳回退本机探针：source=local 且探针结果可见。"""
    cluster.repository.list_devices.return_value = []
    principal = CurrentUser(id='alice', username='alice', role='user')
    request = Request({'type': 'http', 'method': 'GET', 'path': '/', 'headers': [], 'client': ('127.0.0.1', 1)})
    request.state.current_user = principal
    config = SimpleNamespace(load_config=lambda: {}, get_ubuntu_host=lambda _: '127.0.0.1', get_ubuntu_user=lambda _: 'tester')
    monkeypatch.setattr(management_api.runtime.get_runtime(), 'config_manager', config)
    monkeypatch.setattr(management_api.runtime.get_runtime(), 'get_client_id_from_request', lambda _: 'alice')
    monkeypatch.setattr(management_api, 'require_authenticated_user_when_auth_required', lambda _: principal)
    monkeypatch.setattr(management_api, 'known_usbip_sources', lambda: {})
    monkeypatch.setattr(management_api, '_local_adb_proxy_sources', lambda _: {})
    monkeypatch.setattr('features.users.auto_assign_new_devices', lambda *_: [])
    monkeypatch.setattr('features.users.build_device_group_map', lambda *_: {})
    monkeypatch.setattr(management_api.device_lock_manager, 'get_all_locks', lambda: {})
    monkeypatch.setattr(management_api, 'has_blocked_adb_process', lambda: False)
    monkeypatch.setattr(management_api.device_manager, 'get_fastboot_devices', lambda: [])

    def run_local(command, _timeout):
        if command == 'adb devices':
            return CommandResult(stdout='List of devices attached\nADB001\tdevice\n',
                                 stderr='', code=0)
        return CommandResult(stdout='===DEVICE:ADB001===\nADB001\nBox\n16\n90\nRK\n',
                             stderr='', code=0)

    monkeypatch.setattr(management_api.runtime.get_runtime(), 'run_local_shell_command', run_local)
    payload = json.loads(asyncio.run(management_api.devices_management(request)).body)
    assert payload['source'] == 'local'
    assert [item['device_id'] for item in payload['devices']] == ['ADB001']
