"""Durable schedule dispatch, permission revalidation and bounded retries."""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from features.auth import CurrentUser
from features.automation.plan_models import PlanSchedule
from features.automation.plan_models import TestPlanRequest as PlanRequest
from features.automation.plan_store import PlanConflictError
from features.automation.planning import AutomationPlanner
from features.automation.repository import AutomationStore
from features.automation.service import AutomationService


@pytest.fixture
def context(tmp_path):
    owner = CurrentUser(id='alice', username='alice', role='admin')
    clock = [datetime.now(timezone.utc)]
    identity = [owner]
    service = AutomationService(store=AutomationStore(tmp_path / 'automation.sqlite3'), profiles_path=tmp_path / 'profiles.json')
    planner = AutomationPlanner(service, owner_resolver=lambda _: identity[0], clock=lambda: clock[0])
    return planner, service, owner, clock, identity


def step(*, retries=0, test_type='CTS'):
    return {'profile_id': 'nightly', 'test_plan': {
        'test_type': test_type, 'test_suite': '/opt/cts/tools', 'worker_id': 'worker-1',
        'test_module': 'CtsExampleTestCases', 'flash': {'mode': 'skip'},
        'retry_policy': {'max_retries': retries, 'delay_seconds': 60},
    }, 'devices': ['worker-1:ABC']}


def save(context, *, count=2, retries=0, enabled=False, stop=True):
    planner, _, owner, _, _ = context
    return planner.save(PlanRequest(name='Nightly', enabled=enabled, stop_on_failure=stop,
                                   schedule={'kind': 'interval', 'interval_minutes': 5},
                                   runs=[step(retries=retries) for _ in range(count)]), owner)


def finish(service, run_id, *, failed=0, status='completed', folder='2026.10.31_10.00.00'):
    service.store.update_run(run_id, status=status, report_id='report-' + run_id,
                             test_summary_json=json.dumps({
                                 'outcome': 'failed' if failed else 'passed',
                                 'failed': failed, 'passed': 10 - failed, 'total': 10,
                                 'retry_dir': folder,
                                 'retry_worker_id': 'worker-1',
                             }))


def test_schedules_use_local_timezone_and_skip_dst_gap():
    now = datetime(2026, 10, 31, 17, tzinfo=timezone.utc)
    assert PlanSchedule(kind='daily', time='02:00').next_after(now).startswith('2026-10-31T18:00:00')
    weekly = PlanSchedule(kind='weekly', time='02:00', weekdays=[0])
    assert weekly.next_after(now).startswith('2026-11-01T18:00:00')
    gap = PlanSchedule(kind='daily', timezone='America/New_York', time='02:30')
    assert gap.next_after(datetime(2026, 3, 8, 5, tzinfo=timezone.utc)).startswith('2026-03-09T06:30:00')
    fold = PlanSchedule(kind='daily', timezone='America/New_York', time='01:30')
    assert fold.next_after(datetime(2026, 11, 1, 5, 31, tzinfo=timezone.utc)).startswith('2026-11-02T06:30:00')


@pytest.mark.parametrize('body', [
    {'timezone': 'not/a/timezone'}, {'time': '24:01'},
    {'kind': 'weekly', 'weekdays': []}, {'kind': 'interval', 'interval_minutes': 1},
])
def test_invalid_schedules_are_rejected(body):
    with pytest.raises(ValueError):
        PlanSchedule(**body)


def test_saving_plan_seals_credentials_and_does_not_launch(context):
    planner, service, owner, _, _ = context
    body = step()
    body['test_plan']['private_token'] = 'private-test-credential'
    plan = planner.save(PlanRequest(name='Secrets', runs=[body]), owner)
    assert not service.list_runs()
    assert 'template_ciphertext' not in plan
    assert b'private-test-credential' not in service.store.db_path.read_bytes()
    with pytest.raises(LookupError):
        planner.owned(plan['id'], CurrentUser(id='bob', username='bob', role='admin'))
    with pytest.raises(ValueError):
        planner.save(PlanRequest(name='No machine', runs=[step()]), CurrentUser(id='agent:x', username='agent', role='agent'))


def test_due_dispatch_is_atomic_and_skips_overlap_and_missed_occurrences(context):
    planner, service, _, clock, _ = context
    plan = save(context, enabled=True)
    clock[0] += timedelta(hours=2)
    with ThreadPoolExecutor(max_workers=4) as pool:
        counts = list(pool.map(lambda _: planner.tick(), range(4)))
    assert sum(item['scheduled_batches'] for item in counts) == 1
    assert len(service.list_runs()) == 1
    clock[0] += timedelta(minutes=6)
    planner.tick()
    assert len(service.list_runs()) == 1
    updated = planner.store.get(plan['id'])
    assert 'still active' in updated['last_error']
    assert updated['next_run_at'] > clock[0].isoformat().replace('+00:00', 'Z')


def test_sequence_resumes_after_restart_without_duplicating_steps(context):
    planner, service, owner, clock, identity = context
    plan = save(context)
    batch_id = planner.start(plan['id'], owner)
    with pytest.raises(PlanConflictError):
        planner.start(plan['id'], owner)
    first = planner.store.batches(plan['id'])[0]['last_run_id']
    finish(service, first)
    restarted = AutomationPlanner(service, owner_resolver=lambda _: identity[0], clock=lambda: clock[0])
    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(lambda _: restarted.tick(), range(3)))
    runs = service.list_runs()
    assert len(runs) == 2
    batch = restarted.store.batches(plan['id'])[0]
    assert batch['id'] == batch_id
    assert batch['next_step'] == 2
    finish(service, batch['last_run_id'])
    restarted.tick()
    assert restarted.store.batches(plan['id'])[0]['status'] == 'completed'
    assert restarted.store.get(plan['id'])['active_batch_id'] == ''


def test_stop_future_work_keeps_active_guard_and_never_cancels_running_job(context):
    planner, service, owner, _, _ = context
    plan = save(context, retries=2)
    planner.start(plan['id'], owner)
    run = service.list_runs()[0]
    planner.store.stop_batch(plan['id'])
    assert service.store.get_run(run['id'])['status'] == 'queued'
    with pytest.raises(PlanConflictError):
        planner.start(plan['id'], owner)
    finish(service, run['id'], failed=1)
    planner.tick()
    assert len(service.list_runs()) == 1
    assert planner.store.batches(plan['id'])[0]['status'] == 'cancelled'


def test_auto_retry_is_failure_only_bounded_delayed_and_keeps_worker(context):
    planner, service, owner, _, _ = context
    root = service.create_run(step(retries=2), created_by=owner.id, principal=owner)
    finish(service, root['id'], failed=2)
    with ThreadPoolExecutor(max_workers=4) as pool:
        result = list(pool.map(lambda _: planner.tick(), range(4)))
    assert sum(item['automatic_retries'] for item in result) == 1
    child = next(run for run in service.list_runs() if run['id'] != root['id'])
    assert child['worker_id'] == 'worker-1'
    assert child['retry_index'] == 1
    assert child['retry_root_run_id'] == root['id']
    config = json.loads(child['test_plan_json'])
    assert config['flash'] == {'mode': 'skip'}
    assert config['retry_dir'] == '2026.10.31_10.00.00'
    assert 'test_module' not in config
    assert 'build' not in config
    assert 'firmware.stage' not in child['granted_capabilities']
    assert 'build.execute' not in child['granted_capabilities']
    assert service.store.claim_next_active('worker') is None
    finish(service, child['id'], failed=1)
    planner.tick()
    last = next(run for run in service.list_runs() if run['retry_index'] == 2)
    finish(service, last['id'], failed=1)
    planner.tick()
    planner.tick()
    assert len(service.list_runs()) == 3
    assert service.store.get_run(last['id'])['followup_state'] == 'retry_limit_reached'


@pytest.mark.parametrize(('status', 'folder'), [
    ('test_failed', 'valid'), ('cancelled', 'valid'), ('completed', ''), ('completed', '../other'),
])
def test_auto_retry_never_guesses_context_or_retries_infrastructure_errors(context, status, folder):
    planner, service, owner, _, _ = context
    root = service.create_run(step(retries=2), created_by=owner.id, principal=owner)
    finish(service, root['id'], failed=1, status=status, folder=folder)
    planner.tick()
    assert len(service.list_runs()) == 1


def test_permission_revocation_blocks_future_work_but_still_allows_pausing(context):
    planner, service, owner, clock, identity = context
    plan = save(context, enabled=True)
    identity[0] = CurrentUser(id='alice', username='alice', role='user')
    clock[0] += timedelta(minutes=6)
    planner.tick()
    assert not service.list_runs()
    assert 'permissions' in planner.store.get(plan['id'])['last_error']
    planner.enabled(plan['id'], owner, enabled=False, version=plan['version'])
    assert planner.store.get(plan['id'])['enabled'] == 0


def test_sequence_waits_for_retry_and_aggregate_failure_is_preserved(context):
    planner, service, owner, _, _ = context
    plan = save(context, retries=1, stop=False)
    planner.start(plan['id'], owner)
    root = service.list_runs()[0]
    finish(service, root['id'], failed=1)
    planner.tick()
    assert len(service.list_runs()) == 2
    child = next(run for run in service.list_runs() if run['retry_index'] == 1)
    assert planner.store.batches(plan['id'])[0]['next_step'] == 1
    finish(service, child['id'], failed=1)
    planner.tick()
    batch = planner.store.batches(plan['id'])[0]
    assert batch['next_step'] == 2
    finish(service, batch['last_run_id'])
    planner.tick()
    assert planner.store.batches(plan['id'])[0]['status'] == 'failed'


def test_unknown_results_stop_following_steps_by_default(context):
    planner, service, owner, _, _ = context
    plan = save(context)
    planner.start(plan['id'], owner)
    run = service.list_runs()[0]
    service.store.update_run(run['id'], status='completed')
    planner.tick()
    assert len(service.list_runs()) == 1
    assert planner.store.batches(plan['id'])[0]['status'] == 'failed'


def test_stop_cancels_only_unstarted_automatic_retry(context):
    planner, service, owner, _, _ = context
    plan = save(context, retries=2)
    planner.start(plan['id'], owner)
    root = service.list_runs()[0]
    finish(service, root['id'], failed=1)
    planner.tick()
    child = next(run for run in service.list_runs() if run['retry_index'] == 1)
    planner.store.stop_batch(plan['id'])
    assert service.store.get_run(root['id'])['status'] == 'completed'
    assert service.store.get_run(child['id'])['status'] == 'cancelled'
    planner.tick()
    assert planner.store.batches(plan['id'])[0]['status'] == 'cancelled'
    assert len(service.list_runs()) == 2


def test_collected_retry_context_survives_analysis_and_drives_exact_retry(context):
    from features.automation.executors import StubAutomationExecutor
    from features.automation.orchestrator import AutomationOrchestrator

    class ResultExecutor(StubAutomationExecutor):
        def collect_report(self, run):
            return {'success': True, 'report_id': 'report-1', 'report_timestamp': 'report-1', 'report': {
                'source_timestamp': '2026.10.31_10.00.00', 'worker_id': 'worker-1',
                'summary': {'total': 10, 'pass': 9, 'fail': 1},
            }}

        def analyze_report(self, run):
            return {'success': True, 'response': {'data': {'summary': {'total': 10, 'pass': 9, 'fail': 1}}}}

    planner, service, owner, _, _ = context
    root = service.create_run(step(retries=1), created_by=owner.id, principal=owner)
    service.store.update_run(root['id'], status='report_collecting')
    orchestrator = AutomationOrchestrator(service.store, ResultExecutor())
    for _ in range(3):
        orchestrator.advance_run(root['id'])
    planner.tick()
    retry = next(run for run in service.list_runs() if run['id'] != root['id'])
    assert retry['source_type'] == 'auto_retry'
    assert json.loads(retry['test_plan_json'])['retry_dir'] == '2026.10.31_10.00.00'


def test_retry_refuses_report_from_other_worker_and_disabled_owner(context):
    planner, service, owner, _, identity = context
    run = service.create_run(step(retries=1), created_by=owner.id, principal=owner)
    finish(service, run['id'], failed=1)
    snapshot = json.loads(service.store.get_run(run['id'])['test_summary_json'])
    snapshot['retry_worker_id'] = 'worker-2'
    service.store.update_run(run['id'], test_summary_json=json.dumps(snapshot))
    planner.tick()
    assert len(service.list_runs()) == 1
    run2 = service.create_run(step(retries=1), created_by=owner.id, principal=owner)
    finish(service, run2['id'], failed=1)
    identity[0] = None
    planner.tick()
    assert len(service.list_runs()) == 2
    assert service.store.get_run(run2['id'])['followup_state'] == 'retry_authorization_unavailable'


@pytest.mark.parametrize('policy', [{'max_retries': 6}, {'max_retries': True},
                                   {'max_retries': '2'}, {'delay_seconds': 0}])
def test_retry_limits_are_validated_by_both_create_and_preflight(context, policy):
    _, service, owner, _, _ = context
    request = step()
    request['test_plan']['retry_policy'] = policy
    with pytest.raises(ValueError):
        service.create_run(request, created_by=owner.id, principal=owner)
    with pytest.raises(ValueError):
        service.preflight(request, principal=owner)
