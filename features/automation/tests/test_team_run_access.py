"""Shared Web visibility must never become authority to operate a colleague's run."""

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from features.auth import CurrentUser
from features.automation import api
from features.automation.models import AutomationRunCreateRequest
from features.automation.repository import AutomationStore
from features.automation.service import AutomationService
from foundation.error_model import ApiError


def request_for(owner='alice', *, role='user', actor='', method='session'):
    request = Request({'type': 'http', 'method': 'GET', 'path': '/', 'headers': []})
    request.state.current_user = CurrentUser(id=actor or owner, username=owner, role=role, resource_owner_id=owner)
    request.state.auth_method = method
    return request


@pytest.fixture
def team(tmp_path, monkeypatch):
    store = AutomationStore(tmp_path / 'runs.sqlite3')
    service = AutomationService(store=store, profiles_path=tmp_path / 'profiles.json')
    for owner in ['alice', 'bob']:
        run = AutomationRunCreateRequest(
            artifact_path='/tmp/update.img', devices=['DEVICE'],
            test_plan={'test_type': 'CTS', 'wifi': {'password': 'wifi-credential'}},
        ).to_run_dict(f'run-{owner}')
        run.update(created_by=owner, created_by_username=owner, status='completed',
                   created_at='2026-11-10T00:00:00Z', cluster_job_id=f'job-{owner}',
                   result_json=json.dumps({'access_token': 'run-credential'}))
        store.create_run(run)
        store.append_event(run['id'], 'completed', 'info', 'Finished',
                           {'password': 'event-credential'})
    monkeypatch.setattr(api, 'automation_service', service)
    cluster = SimpleNamespace(repository=SimpleNamespace(
        list_timeline=lambda **kwargs: [{'id': 100, 'payload': {'dispatch_token': 'timeline-credential'}}],
        get_job=lambda job_id: {'id': job_id, 'dispatch_token': 'trace-credential'},
    ))
    monkeypatch.setattr(api, 'get_cluster_service', lambda: cluster)
    monkeypatch.setattr('features.build.get_build_service', lambda: SimpleNamespace(list_jobs=lambda **kwargs: []))
    monkeypatch.setattr('features.automation.telemetry.run_progress', lambda run, _: {'run_id': run['id'], 'password': 'progress-credential'})
    return service


@pytest.mark.asyncio
async def test_web_user_can_read_the_entire_foreign_run_evidence_chain(team):
    request = request_for()
    listed = await api.list_automation_runs(request, status='', limit=50)
    assert {run['id'] for run in listed['data']['items']} == {'run-alice', 'run-bob'}
    assert {run['id']: run['permissions']['can_manage'] for run in listed['data']['items']} == {
        'run-alice': True, 'run-bob': False,
    }
    detail = await api.get_automation_run('run-bob', request)
    events = await api.get_automation_run_events('run-bob', request)
    timeline = await api.get_automation_run_timeline('run-bob', request)
    trace = await api.get_automation_run_trace('run-bob', request)
    progress = api.get_automation_run_progress('run-bob', request)
    dashboard = await api.automation_dashboard(request)
    assert detail['data']['id'] == 'run-bob' and detail['data']['permissions']['can_manage'] is False
    assert events['data']['items'] and timeline['data']['items']
    assert trace['data']['cluster_job']['id'] == 'job-bob'
    assert progress['data']['run_id'] == 'run-bob'
    assert dashboard['data']['run_total'] == 2
    public = json.dumps([listed, detail, events, timeline, trace, progress])
    assert all(value not in public for value in [
        'wifi-credential', 'run-credential', 'event-credential', 'timeline-credential',
        'trace-credential', 'progress-credential',
    ])


@pytest.mark.asyncio
@pytest.mark.parametrize('operation', ['cancel_automation_run', 'retry_automation_run', 'release_automation_run_reservation'])
async def test_foreign_web_user_gets_forbidden_before_any_mutation(team, operation, monkeypatch):
    mutate = Mock(side_effect=AssertionError('must authorize before changing a run'))
    method = {'cancel_automation_run': 'cancel_run', 'retry_automation_run': 'retry_run',
              'release_automation_run_reservation': 'release_completed_resources'}[operation]
    monkeypatch.setattr(team, method, mutate)
    with pytest.raises(ApiError) as raised:
        await getattr(api, operation)('run-bob', request_for())
    assert raised.value.code == 'FORBIDDEN' and raised.value.status_code == 403
    mutate.assert_not_called()
    assert team.get_run('run-bob')['status'] == 'completed'


@pytest.mark.asyncio
@pytest.mark.parametrize('owner, role', [('bob', 'user'), ('admin', 'admin')])
@pytest.mark.parametrize('operation', ['cancel_automation_run', 'retry_automation_run', 'release_automation_run_reservation'])
async def test_owner_and_admin_can_operate_the_run(team, monkeypatch, owner, role, operation):
    run = team.get_run('run-bob')
    mutate = Mock(return_value=run)
    method = {'cancel_automation_run': 'cancel_run', 'retry_automation_run': 'retry_run',
              'release_automation_run_reservation': 'release_completed_resources'}[operation]
    monkeypatch.setattr(team, method, mutate)
    request = request_for(owner, role=role)
    detail = await api.get_automation_run('run-bob', request)
    assert detail['data']['permissions']['can_manage'] is True
    assert (await getattr(api, operation)('run-bob', request))['success']
    mutate.assert_called_once_with('run-bob')


@pytest.mark.asyncio
@pytest.mark.parametrize('role, actor, method', [
    ('agent_service', 'agent:token', 'agent_token'),
    ('automation', 'automation:run', 'machine_authority'),
    ('worker_service', 'worker:controller', 'worker_token'),
])
async def test_machine_credentials_keep_owner_scope_and_cannot_manage(team, role, actor, method):
    request = request_for(role=role, actor=actor, method=method)
    listed = await api.list_automation_runs(request, status='', limit=50)
    assert [run['id'] for run in listed['data']['items']] == ['run-alice']
    assert listed['data']['items'][0]['permissions']['can_manage'] is False
    detail = await api.get_automation_run('run-bob', request)
    assert detail.status_code == 404
    dashboard = await api.automation_dashboard(request)
    assert dashboard['data']['run_total'] == 1
    for operation in [api.cancel_automation_run, api.retry_automation_run, api.release_automation_run_reservation]:
        with pytest.raises((ApiError, HTTPException)) as raised:
            await operation('run-alice', request)
        assert raised.value.status_code == 403


@pytest.mark.asyncio
async def test_team_visibility_still_requires_authentication(team, monkeypatch):
    request = Request({'type': 'http', 'method': 'GET', 'path': '/', 'headers': []})
    monkeypatch.setattr(api, 'get_authenticated_user', lambda _: None)

    def deny(_):
        raise HTTPException(401, 'Authentication required')

    monkeypatch.setattr(api, 'require_authenticated_user_when_auth_required', deny)
    with pytest.raises(HTTPException) as raised:
        await api.list_automation_runs(request, status='', limit=50)
    assert raised.value.status_code == 401
