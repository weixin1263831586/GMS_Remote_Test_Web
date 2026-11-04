"""A cached connection and unknown properties must not imply device readiness."""
import json
import threading
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from starlette.requests import Request

from features.devices import api, bootloader_api, info_service
from features.devices.manager import DeviceManager
from features.devices.models import DeviceActionRequest
from foundation.command_result import CommandResult
from foundation.error_model import ApiError


@pytest.fixture
def reader(monkeypatch):
    ssh_manager = Mock()
    manager = DeviceManager(ssh_manager=ssh_manager, config_manager=SimpleNamespace(load_config=lambda: {}))
    monkeypatch.setattr(info_service, 'device_manager', manager)
    monkeypatch.setattr(info_service, 'get_device_properties_optimized', lambda *_: {})
    return manager, ssh_manager


@pytest.mark.parametrize('result,code,adb_state', [
    (CommandResult(stderr='error: device unauthorized', code=1), 'STATE_CONFLICT', 'unauthorized'),
    (CommandResult(stdout='offline'), 'STATE_CONFLICT', 'offline'),
    (CommandResult(stderr="error: device 'SERIAL' not found", code=1), 'STATE_CONFLICT', 'absent'),
    (CommandResult(stderr='connection failed', code=255), 'UPSTREAM_FAILURE', 'unknown'),
    (CommandResult(timed_out=True), 'DEPENDENCY_TIMEOUT', 'unknown'),
    (CommandResult(stderr='adb: command not found', code=127), 'DEPENDENCY_UNAVAILABLE', 'unknown'),
])
def test_device_information_requires_live_adb_state(reader, result, code, adb_state):
    manager, ssh_manager = reader
    ssh_manager.execute_command.return_value = result
    with pytest.raises(ApiError) as caught:
        manager.get_device_info('SERIAL', ssh=object())
    assert caught.value.code == code
    assert caught.value.details['adb_state'] == adb_state
    assert ssh_manager.execute_command.call_count == 1


@pytest.mark.parametrize('result,code', [
    (CommandResult(stdout=''), 'UPSTREAM_FAILURE'),
    (CommandResult(stdout='unparseable output'), 'UPSTREAM_FAILURE'),
    (CommandResult(stdout='[ro.product.model]: [未知]'), 'UPSTREAM_FAILURE'),
    (CommandResult(stderr='device offline', code=1), 'STATE_CONFLICT'),
    (CommandResult(timed_out=True), 'DEPENDENCY_TIMEOUT'),
])
def test_empty_or_failed_property_reads_never_return_unknown_success(reader, result, code):
    manager, ssh_manager = reader
    ssh_manager.execute_command.side_effect = [CommandResult(stdout='device'), result]
    with pytest.raises(ApiError) as caught:
        manager.get_device_info('SERIAL', ssh=object())
    assert caught.value.code == code


@pytest.fixture
def info_endpoint(monkeypatch):
    @asynccontextmanager
    async def connection():
        yield object()
    monkeypatch.setattr(bootloader_api, 'AsyncSSHConnection', connection)
    monkeypatch.setattr(bootloader_api, 'device_claim_conflict_response', lambda *_args, **_kwargs: None)
    monkeypatch.setattr(bootloader_api, 'device_fencing_owner_id', lambda _: 'owner')
    return Request({'type': 'http', 'method': 'POST', 'path': '/api/devices/info', 'headers': []})


@pytest.mark.asyncio
async def test_info_endpoint_preserves_failure_envelope(reader, info_endpoint, monkeypatch):
    state = SimpleNamespace(device_cache_lock=threading.RLock(), device_cache={
        'devices': [{'device_id': 'SERIAL', 'status': 'online'}], 'timestamp': time.time()})
    monkeypatch.setattr(api.runtime.get_runtime(), 'global_state', state)
    reader[1].execute_command.return_value = CommandResult(stderr='error: device unauthorized', code=1)
    response = await bootloader_api.get_device_info(DeviceActionRequest(devices=['SERIAL']), info_endpoint)
    body = json.loads(response.body)
    assert response.status_code == 409 and body['success'] is False
    assert body['code'] == 'STATE_CONFLICT'
    assert body['details'] == {'device': 'SERIAL', 'adb_state': 'unauthorized'}
    assert body['next_actions']
    assert state.device_cache['timestamp'] == 0
    assert state.device_cache['devices'][0]['status'] == 'unauthorized'


@pytest.mark.asyncio
async def test_partial_information_is_explicit_and_keeps_valid_properties(reader, info_endpoint):
    reader[1].execute_command.side_effect = [
        CommandResult(stdout='device'), CommandResult(stdout='[ro.product.model]: [Box]'),
    ]
    response = await bootloader_api.get_device_info(DeviceActionRequest(devices=['SERIAL']), info_endpoint)
    body = json.loads(response.body)
    result = body['data']['results'][0]
    assert body['success'] is True and '部分' in body['message']
    assert result['properties']['Model'] == 'Box'
    assert result['complete'] is False and 'Serial Number' in result['missing_properties']


@pytest.mark.asyncio
@pytest.mark.parametrize('usb_observed', [False, True])
async def test_empty_scan_does_not_refresh_cached_online_state(monkeypatch, usb_observed):
    globals_ = SimpleNamespace(
        device_cache={'devices': [{'device_id': 'SERIAL', 'status': 'online', 'protocol': 'adb'}],
                      'timestamp': time.time() if usb_observed else time.time() - 1000},
        device_cache_lock=threading.RLock(), user_states={}, user_states_lock=threading.RLock(),
    )
    runtime = api.runtime.get_runtime()
    monkeypatch.setattr(runtime, 'global_state', globals_)
    monkeypatch.setattr(runtime, 'config_manager', SimpleNamespace(load_config=lambda: {}))
    monkeypatch.setattr(runtime, 'get_client_id_from_request', lambda _: 'owner')
    scan = Mock(return_value=[])
    monkeypatch.setattr(api.device_manager, 'get_connected_devices', scan)
    monkeypatch.setattr(api.device_manager, 'get_fastboot_devices', lambda: [])
    monkeypatch.setattr(api.device_manager, 'get_rockusb_loader_devices', lambda *_: [])
    usb_scan = Mock(return_value={'SERIAL': {'device_id': 'SERIAL', 'status': 'usb_only',
                                           'protocol': 'usb_only', 'transport': 'local_usb'}} if usb_observed else {})
    monkeypatch.setattr(api, 'local_usb_only_inventory', usb_scan)
    monkeypatch.setattr(api, 'known_usbip_sources', lambda: {})
    monkeypatch.setattr(api, '_prune_inactive_usbip_sources', lambda *_: {})
    monkeypatch.setattr(api.reconnect, 'reconcile_observed_usbip_devices', lambda _: None)
    monkeypatch.setattr(api.reconnect, 'filter_suppressed_usbip_devices', list)
    monkeypatch.setattr('features.users.load_device_groups', lambda _: [])
    monkeypatch.setattr('features.users.build_device_group_map', lambda _: {})
    monkeypatch.setattr('features.users.current_username_for_request', lambda _: 'owner')
    request = Request({'type': 'http', 'method': 'GET', 'path': '/api/devices/list', 'headers': []})
    for _ in range(2):
        response = await api.get_connected_devices(request, force_refresh=not usb_observed)
        device = json.loads(response.body)[0]
        if usb_observed:
            assert device['status'] == device['protocol'] == 'usb_only'
        else:
            assert device['status'] == 'unknown' and device['stale'] is True
        assert globals_.device_cache['timestamp'] == 0
    assert scan.call_count == (1 if usb_observed else 2)
    usb_scan.return_value = {}
    scan.return_value = ['SERIAL']
    response = await api.get_connected_devices(request, force_refresh=True)
    device = json.loads(response.body)[0]
    assert device['status'] == 'online' and not device.get('stale')
    assert globals_.device_cache['timestamp'] > 0
