"""Automatic recovery freezes target calibration before any firmware dispatch."""
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from features.automation.aoa_snapshot import freeze_recovery
from features.automation.executors import HttpAutomationExecutor
from tests.aoa_fixtures import firmware_declaration, verified_mapping

from .test_integrations import FakeResponse, FakeSession


def prepared_run(monkeypatch, *, target=None):
    source = verified_mapping()
    target = target or source
    monkeypatch.setattr('features.devices.aoa_mapping_service.calibration_store', lambda: SimpleNamespace(
        find_matching=lambda owner, context: {'mapping': target.to_dict(), 'mapping_policy': target.effective_policy}
        if owner == 'alice' and context == target.context else None))
    plan = {'aoa': {'mode': 'user_build_recovery', 'commands': ['click 180 320'],
                   'metadata': {'hid_touch_mode': 'direct', 'calibration': {'mapping': source.to_dict()},
                                'firmware_compatibility': firmware_declaration(target.context)}}}
    freeze_recovery(plan, 'alice')
    run = {'id': 'run', 'worker_id': 'w', 'created_by': 'alice', 'test_plan_json': json.dumps(plan)}
    inventory = [{'id': 'w:ABC', 'properties': {'usb': '1-2', 'aoa_context': target.context}}]
    repository = SimpleNamespace(list_devices=lambda _: inventory)
    return run, repository, source, target


def test_automatic_recovery_dispatches_frozen_source_and_target_mapping(monkeypatch):
    run, repository, source, target = prepared_run(monkeypatch, target=verified_mapping(1200, 1920))
    session = FakeSession()
    session.queue(FakeResponse(json_data={'success': True, 'command_id': 'durable-command'}))
    executor = HttpAutomationExecutor(session=session)
    snapshots = executor.snapshot_devices(repository, 'w', [{'id': 'w:ABC'}], run)
    run['devices_json'] = json.dumps(snapshots)
    # Changes to subsequent inventory must not affect the run's saved mapping.
    repository.list_devices = lambda _: []
    assert executor.aoa_recover(run)['command_id'] == 'durable-command'
    sent = session.calls[0][2]['json']
    assert sent['aoa_source_touch_mapping'] == source.to_dict()
    assert sent['aoa_touch_mapping'] == target.to_dict()
    assert sent['aoa_mapping_policy'] == 'direct_calibrated'


def test_incompatible_target_is_rejected_before_reserving_or_flashing(monkeypatch):
    run, repository, _, _ = prepared_run(monkeypatch)
    repository.list_devices = lambda _: [{'id': 'w:ABC', 'properties': {
        'usb': '1-2', 'aoa_context': {'model': 'unknown'}}}]
    with pytest.raises(ValueError, match='target device model'):
        HttpAutomationExecutor().snapshot_devices(repository, 'w', [{'id': 'w:ABC'}], run)


def test_legacy_run_with_missing_mapping_snapshot_cannot_inject_touch(monkeypatch):
    run, _, _, _ = prepared_run(monkeypatch)
    run['devices_json'] = '[{"serial":"w:ABC","usb_port_path":"1-2"}]'
    session = FakeSession()
    result = HttpAutomationExecutor(session=session).aoa_recover(run)
    assert result['success'] is False
    assert 'snapshot is missing' in result['error']
    assert session.calls == []


def test_mapping_capability_is_required_before_flash(monkeypatch):
    run, repository, _, _ = prepared_run(monkeypatch)
    executor = HttpAutomationExecutor()
    run['devices_json'] = json.dumps(executor.snapshot_devices(repository, 'w', [{'id': 'w:ABC'}], run))
    capabilities = dict.fromkeys(['aoa_identity_fencing_v1', 'aoa_hid_modes_v1',
                                 'aoa_usb_hid_v1', 'aoa_record_input_v1'], True)
    repository.get_worker = lambda _: {'capabilities': capabilities}
    cluster = SimpleNamespace(repository=repository)
    assert 'mapping protocol' in executor.recovery_preflash_error(cluster, 'w', run)
    capabilities['aoa_affine_mapping_v2'] = True
    assert executor.recovery_preflash_error(cluster, 'w', run) == ''


def test_invalid_mapping_does_not_leave_an_untracked_reservation(monkeypatch):
    run, repository, _, _ = prepared_run(monkeypatch)
    run['devices_json'] = '[{"serial":"w:ABC"}]'
    repository.get_reservation_by_source = lambda _: None
    repository.list_devices = lambda _: [{'id': 'w:ABC', 'properties': {'usb': '1-2'}}]
    repository.reserve_devices = Mock()
    cluster = SimpleNamespace(repository=repository, config=SimpleNamespace(local_worker_id='w'),
                              has_command_agent=lambda _: True)
    monkeypatch.setattr('features.cluster.get_cluster_service', lambda: cluster)
    result = HttpAutomationExecutor().select_devices(run)
    assert result['success'] is False
    assert 'target device model' in result['error']
    repository.reserve_devices.assert_not_called()


def test_changed_firmware_declaration_cannot_reuse_a_frozen_run(monkeypatch):
    run, repository, _, _ = prepared_run(monkeypatch)
    executor = HttpAutomationExecutor(session=FakeSession())
    run['devices_json'] = json.dumps(executor.snapshot_devices(repository, 'w', [{'id': 'w:ABC'}], run))
    plan = json.loads(run['test_plan_json'])
    plan['aoa']['metadata']['firmware_compatibility']['sha256'] = 'b' * 64
    run['test_plan_json'] = json.dumps(plan)
    result = executor.aoa_recover(run)
    assert result['success'] is False and 'snapshot changed' in result['error']
    assert executor.session.calls == []
