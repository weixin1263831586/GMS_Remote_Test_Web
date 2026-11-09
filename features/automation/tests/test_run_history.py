"""History pagination, result semantics and durable rerun provenance."""

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pytest
from starlette.requests import Request

from features.auth import CurrentUser
from features.automation.models import AutomationRunCreateRequest
from features.automation.repository import AutomationStore
from features.automation.run_history import report_snapshot
from features.automation.service import AutomationService
from foundation.error_model import ApiError


@pytest.fixture
def store(tmp_path):
    return AutomationStore(tmp_path / 'runs.sqlite3')


def add_run(store, run_id, *, owner='alice', status='queued', **fields):
    run = AutomationRunCreateRequest(
        profile_id='nightly', project='rk3576', devices=['ABC'],
        test_plan={'test_type': 'CTS', 'test_suite': '/opt/cts', 'token': 'private-plan-secret'},
    ).to_run_dict(run_id)
    run.update(created_by=owner, status=status, created_at='2026-10-31T00:00:00Z', **fields)
    return store.create_run(run)


def test_history_filters_owner_before_pagination_and_never_returns_plan_secrets(store):
    for number in range(8):
        add_run(store, f'run_{number}', owner='alice' if number % 2 else 'bob')
    first = store.list_run_page(created_by='alice', limit=2)
    assert [run['id'] for run in first['items']] == ['run_7', 'run_5']
    assert first['has_more']
    # A new record must not duplicate or displace rows on the next page.
    add_run(store, 'run_9')
    second = store.list_run_page(created_by='alice', limit=2, cursor=first['next_cursor'])
    assert [run['id'] for run in second['items']] == ['run_3', 'run_1']
    assert second['has_more'] is False
    assert second['next_cursor'] == ''
    serialized = json.dumps(first)
    assert 'private-plan-secret' not in serialized
    assert 'test_plan_json' not in serialized
    assert 'result_json' not in serialized
    assert first['items'][0]['test_type'] == 'CTS'
    assert first['items'][0]['test_suite'] == '/opt/cts'


def test_filters_search_literal_text_and_include_actual_running_and_failure_states(store):
    add_run(store, 'a', status='testing', worker_id='worker-1')
    add_run(store, 'b', status='test_running', worker_id='worker-1')
    add_run(store, 'c', status='flash_failed', worker_id='worker-2')
    assert len(store.list_run_page(status='testing')['items']) == 2
    assert len(store.list_run_page(status='running')['items']) == 2
    assert [r['id'] for r in store.list_run_page(status='failed')['items']] == ['c']
    assert len(store.list_run_page(query='abc', worker_id='worker-1', profile_id='nightly')['items']) == 2
    assert not store.list_run_page(query="%' OR 1=1 --")['items']
    assert not store.list_run_page(query='private-plan-secret')['items']
    assert not store.list_run_page(profile_id='other')['items']


@pytest.mark.parametrize(('payload', 'outcome'), [
    ({}, None),
    ({'summary': {'total': 0, 'pass': 0, 'fail': 0}}, 'incomplete'),
    ({'summary': {'total': 10, 'pass': 10, 'fail': 0}}, 'passed'),
    ({'summary': {'total': 10, 'pass': 9, 'fail': 1}}, 'failed'),
    ({'summary': {'total': 10, 'fail': 0}}, 'incomplete'),
    ({'summary': {'total': 10, 'pass': 5, 'fail': 0}}, 'incomplete'),
    ({'summary': {'total': True, 'pass': -1, 'fail': 'bad'}}, None),
    ({'response': {'data': {'summary': {'total': '2', 'pass': '1', 'failed': '1'}}}}, 'failed'),
])
def test_report_snapshot_requires_explicit_consistent_counts(payload, outcome):
    assert report_snapshot(payload).get('outcome') == outcome


def test_report_filter_is_applied_before_page_limit_and_unknown_is_not_passed(store):
    add_run(store, 'a', status='completed', report_timestamp='report-a')
    add_run(store, 'b', status='completed')
    add_run(store, 'c', status='completed')
    page = store.list_run_page(reports_only=True, limit=1)
    assert [item['id'] for item in page['items']] == ['a']
    assert page['has_more'] is False
    assert len(store.list_run_page(outcome='unknown')['items']) == 3
    assert not store.list_run_page(outcome='passed')['items']


def test_summary_survives_engine_loss_and_analysis_failure(store):
    from features.automation.executors import StubAutomationExecutor
    from features.automation.orchestrator import AutomationOrchestrator

    class ReportExecutor(StubAutomationExecutor):
        def collect_report(self, run):
            return {'success': True, 'report_timestamp': 'report-a', 'report': {
                'summary': {'total': 100, 'pass': 98, 'fail': 2},
                'details': {'suite_version': '15_r2'},
            }}

    add_run(store, 'a', status='report_collecting')
    orchestrator = AutomationOrchestrator(store, ReportExecutor(fail_stage='analyze_report'))
    orchestrator.advance_run('a')
    orchestrator.advance_run('a')
    reopened = AutomationStore(store.db_path)
    item = reopened.list_run_page(outcome='failed')['items'][0]
    assert item['status'] == 'analysis_failed'
    assert item['test_summary']['failed'] == 2
    assert item['test_summary']['suite_version'] == '15_r2'


def test_analysis_snapshot_is_persisted_even_when_completion_has_test_failures(store):
    from features.automation.executors import StubAutomationExecutor
    from features.automation.orchestrator import AutomationOrchestrator

    class AnalysisExecutor(StubAutomationExecutor):
        def analyze_report(self, run):
            return {'success': True, 'response': {'data': {
                'summary': {'total': 10, 'pass': 9, 'fail': 1},
            }}}

    add_run(store, 'a', status='analyzing', report_timestamp='report-a')
    orchestrator = AutomationOrchestrator(store, AnalysisExecutor())
    orchestrator.advance_run('a')
    orchestrator.advance_run('a')
    item = store.list_run_page()['items'][0]
    assert item['status'] == 'completed'
    assert item['test_summary']['outcome'] == 'failed'


def test_retry_preserves_target_and_source_but_clears_previous_result(store, tmp_path):
    original = add_run(store, 'a', status='completed', worker_id='worker-2', report_timestamp='old',
                       test_summary_json='{"outcome":"passed"}')
    service = AutomationService(store=store, profiles_path=tmp_path / 'profiles.json')
    retried = service.retry_run(original['id'])
    assert retried['retry_of_run_id'] == 'a'
    assert retried['worker_id'] == 'worker-2'
    assert retried['created_by'] == 'alice'
    assert retried['report_timestamp'] == ''
    assert json.loads(retried['test_summary_json']) == {}
    assert json.loads(retried['result_json']) == {}


def test_schema_upgrade_is_idempotent_across_connections(store):
    with store._connect() as connection:
        connection.execute('ALTER TABLE automation_runs DROP COLUMN retry_of_run_id')
        connection.execute('ALTER TABLE automation_runs DROP COLUMN test_summary_json')
    with ThreadPoolExecutor(max_workers=4) as pool:
        migrated = list(pool.map(lambda _: AutomationStore(store.db_path), range(4)))
    add_run(migrated[0], 'a')
    assert migrated[1].list_run_page()['items'][0]['test_summary'] == {}


def test_api_pages_remain_owner_scoped_and_reject_invalid_cursors(store, tmp_path):
    from features.automation import api

    add_run(store, 'a')
    add_run(store, 'b', owner='bob')
    request = Request({'type': 'http', 'method': 'GET', 'path': '/', 'headers': []})
    request.state.current_user = CurrentUser(id='alice', username='alice', role='user')
    service = AutomationService(store=store, profiles_path=tmp_path / 'profiles.json')
    with patch.object(api, 'automation_service', service):
        result = asyncio.run(api.list_automation_runs(request, status='', limit=1))
        assert [item['id'] for item in result['data']['items']] == ['a']
        assert result['data']['has_more'] is False
        with pytest.raises(ApiError) as exc:
            asyncio.run(api.list_automation_runs(request, status='', limit=1, cursor='bad'))
        assert exc.value.code == 'MALFORMED_REQUEST'
        with pytest.raises(ApiError):
            asyncio.run(api.list_automation_runs(request, status='', limit=1, outcome='arbitrary'))


def test_active_run_ids_are_not_bounded_by_recent_window(store):
    # 回归：ops 暂存固件清理依赖“全量非终态任务”。list_runs(limit=500)
    # 只返回最近 500 条，更早的 queued 任务会被漏掉、其暂存产物可能被
    # 误清理——active_run_ids 必须不受该窗口限制。
    add_run(store, 'recent-terminal', status='completed')
    for index in range(505):
        add_run(store, f'old-{index:03d}', status='queued')

    active = set(store.active_run_ids())

    assert len(active) == 505
    assert all(f'old-{index:03d}' in active for index in range(505))
    assert 'recent-terminal' not in active
    # 对比口径：受 limit 钳制的 list_runs 确实覆盖不了全部活跃任务。
    assert len(store.list_runs(limit=500)) < len(active)
