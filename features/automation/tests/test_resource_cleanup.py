"""Completed workflows release shared claims without repeating device operations."""

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from features.automation.executor_contract import StubAutomationExecutor
from features.automation.executors import HttpAutomationExecutor
from features.automation.models import AutomationRunCreateRequest
from features.automation.orchestrator import AutomationOrchestrator
from features.automation.repository import RUN_COLUMNS, AutomationStore
from features.automation.resource_cleanup import cleanup_state
from features.cluster import ClusterRepository


@pytest.fixture
def context(tmp_path, monkeypatch):
    repository = ClusterRepository(tmp_path / 'cluster.sqlite3', claim_db_path=tmp_path / 'device_claims.sqlite3')
    repository.register_worker({'worker_id': 'ats-worker-controller', 'name': 'local', 'hostname': 'localhost',
                                'address': '127.0.0.1', 'agent_version': '1', 'max_jobs': 1, 'capabilities': {}})
    repository.heartbeat('ats-worker-controller', {'running_jobs': [], 'devices': [{'serial': 'ABC', 'state': 'available'}],
                                                   'suites': []})
    reservation = repository.reserve_devices('ats-worker-controller', ['ABC'], owner_id='alice', source_id='run')
    store = AutomationStore(tmp_path / 'runs.sqlite3')
    run = AutomationRunCreateRequest(devices=['ABC'], test_plan={'test_mode': 'skip'}).to_run_dict('run')
    run.update(status='flash_verified', current_stage='flash_verified', created_by='alice',
               worker_id='ats-worker-controller', device_reservation_id=reservation['id'],
               result_json=json.dumps({'post_flash': {'verified': True}}))
    store.create_run(run)
    monkeypatch.setattr('features.cluster.get_cluster_service', lambda: SimpleNamespace(repository=repository))
    executor = StubAutomationExecutor()
    release = Mock(side_effect=HttpAutomationExecutor.release_resources)
    executor.release_resources = release
    return SimpleNamespace(repository=repository, reservation=reservation, store=store, executor=executor,
                           release=release, orchestrator=AutomationOrchestrator(store, executor))


@pytest.mark.parametrize('stage', ['flash_verified', 'reporting'])
def test_successful_completion_releases_reservation_and_claim(context, stage):
    context.store.update_run('run', status=stage, current_stage=stage)
    result = context.orchestrator.advance_run('run')
    assert result['status'] == 'completed' and result['finished_at']
    assert cleanup_state(result) == 'released'
    assert context.repository.get_reservation(context.reservation['id'])['status'] == 'released'
    assert context.repository.claims.active_claim('ats-worker-controller:ABC') is None
    context.orchestrator.advance_run('run')
    context.orchestrator.release_completed_resources('run')
    assert context.release.call_count == 1


def test_cleanup_failure_is_durable_and_retry_never_replays_firmware_or_notifications(context):
    flash = context.executor.flash = Mock(side_effect=AssertionError('must not flash again'))
    report = context.executor.report_result = Mock(side_effect=AssertionError('must not notify again'))
    context.release.side_effect = RuntimeError('dependency unavailable')
    result = context.orchestrator.advance_run('run')
    assert result['status'] == 'completed' and cleanup_state(result) == 'pending'
    assert context.store.completed_cleanup_candidates() == ['run']
    assert context.repository.get_reservation(context.reservation['id'])['status'] == 'active'
    context.release.side_effect = HttpAutomationExecutor.release_resources
    result = context.orchestrator.release_completed_resources('run')
    assert cleanup_state(result) == 'released' and result['status'] == 'completed'
    assert context.store.completed_cleanup_candidates() == []
    flash.assert_not_called()
    report.assert_not_called()


@pytest.mark.parametrize('field', ['reservation_id', 'device_reservation_id', 'claim_source_id'])
def test_active_device_command_preserves_the_reservation(context, field):
    value = 'reservation:' + context.reservation['id'] if field == 'claim_source_id' else context.reservation['id']
    context.repository.create_command({'worker_id': 'ats-worker-controller', 'command_type': 'device_action',
                                       'initial_status': 'running', 'payload': {field: value}})
    result = context.orchestrator.advance_run('run')
    assert result['status'] == 'completed' and cleanup_state(result) == 'pending'
    assert context.repository.get_reservation(context.reservation['id'])['status'] == 'active'
    assert context.repository.claims.active_claim('ats-worker-controller:ABC')


def test_mismatched_reservation_source_is_never_released(context):
    run = context.store.get_run('run')
    outcome = HttpAutomationExecutor.release_resources({**run, 'id': 'another-run', 'status': 'completed'})
    assert outcome['success'] is False
    assert context.repository.get_reservation(context.reservation['id'])['status'] == 'active'


def test_claim_release_retries_after_the_reservation_commit_was_interrupted(context, monkeypatch):
    release = context.repository.claims.release
    monkeypatch.setattr(context.repository.claims, 'release', Mock(side_effect=RuntimeError('interrupted')))
    result = context.orchestrator.advance_run('run')
    assert cleanup_state(result) == 'pending'
    assert context.repository.get_reservation(context.reservation['id'])['status'] == 'released'
    assert context.repository.claims.active_claim('ats-worker-controller:ABC')
    monkeypatch.setattr(context.repository.claims, 'release', release)
    result = context.orchestrator.release_completed_resources('run')
    assert cleanup_state(result) == 'released'
    assert context.repository.claims.active_claim('ats-worker-controller:ABC') is None


def test_repeated_old_release_preserves_a_new_owners_claim(context):
    context.orchestrator.advance_run('run')
    newer = context.repository.reserve_devices('ats-worker-controller', ['ABC'], owner_id='bob', source_id='new-run')
    context.repository.release_reservation(context.reservation['id'], expected_source_id='run')
    active = context.repository.claims.active_claim('ats-worker-controller:ABC')
    assert active['source_id'] == 'reservation:' + newer['id']


def test_cleanup_cannot_detach_an_active_job_after_reservation_conversion(context):
    job = context.repository.create_job_with_leases({
        'worker_id': 'ats-worker-controller', 'owner_id': 'alice', 'devices': ['ABC'],
        'device_reservation_id': context.reservation['id'], 'automation_run_id': 'run',
    })
    result = context.orchestrator.advance_run('run')
    assert cleanup_state(result) == 'pending'
    assert context.repository.get_job(job['id'])['leases'][0]['status'] == 'active'
    assert context.repository.claims.active_claim('ats-worker-controller:ABC')['owner_id'] == 'alice'


def test_explicit_cleanup_refuses_an_active_run(context):
    with pytest.raises(ValueError, match='仅已完成'):
        context.orchestrator.release_completed_resources('run')
    context.release.assert_not_called()


def test_legacy_completed_run_is_repaired_and_keeps_completion_status(context, monkeypatch):
    from features.automation import worker
    from features.automation.service import AutomationService

    context.store.update_run('run', status='completed', current_stage='completed', finished_at='')
    service = AutomationService(store=context.store, profiles_path=context.store.db_path.parent / 'profiles.json')
    monkeypatch.setattr(service, 'orchestrator', lambda _: context.orchestrator)
    monkeypatch.setattr(worker, '_stale_swept', True)
    monkeypatch.setattr(worker, '_last_build_poll_at', 0)
    poll = Mock(return_value=0)
    monkeypatch.setattr(worker, 'poll_running_builds_sync', poll)
    monkeypatch.setattr(worker, 'sweep_waiting_device_timeouts', lambda *args, **kwargs: 0)
    monkeypatch.setattr(worker, 'sweep_active_stage_timeouts', lambda *args, **kwargs: {})
    monkeypatch.setattr('features.automation.execution_policy.enforce_extra_timeouts', lambda *args: None)
    monkeypatch.setattr('features.cluster.run_artifact_retention', lambda: None)
    monkeypatch.setattr(worker, '_last_ops_sweep_at', worker.time.monotonic())
    monkeypatch.setattr(worker, '_last_disk_hygiene_at', worker.time.monotonic())
    worker.run_maintenance_sync({'executor': 'stub'}, automation_service=service, build_service=object())
    result = context.store.get_run('run')
    assert result['status'] == 'completed' and result['finished_at']
    assert cleanup_state(result) == 'released'
    assert context.repository.claims.active_claim('ats-worker-controller:ABC') is None
    monkeypatch.setattr(context.store, 'completed_cleanup_candidates', Mock(side_effect=RuntimeError('database busy')))
    monkeypatch.setattr(worker, '_last_build_poll_at', 0)
    worker.run_maintenance_sync({'executor': 'stub'}, automation_service=service, build_service=object())
    poll.assert_called()


def test_pending_cleanup_is_not_hidden_by_recent_history(context):
    context.store.update_run('run', status='completed', current_stage='completed')
    template = context.store.get_run('run')
    rows = [{**template, 'id': f'new-{index}', 'status': 'queued', 'current_stage': 'queued',
             'device_reservation_id': ''} for index in range(505)]
    with context.store._connect() as conn:
        conn.executemany(
            f"INSERT INTO automation_runs ({','.join(RUN_COLUMNS)}) VALUES ({','.join('?' for _ in RUN_COLUMNS)})",
            [[row[column] for column in RUN_COLUMNS] for row in rows],
        )
    assert context.store.completed_cleanup_candidates() == ['run']
