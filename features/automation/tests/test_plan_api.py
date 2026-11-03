"""Plan API boundaries: human actors, ownership, revision conflicts and secrecy."""

from unittest.mock import patch

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from features.auth import CurrentUser
from features.automation import plan_api
from features.automation.plan_models import PlanSettingsRequest
from features.automation.plan_models import TestPlanRequest as PlanRequest
from features.automation.planning import AutomationPlanner
from features.automation.repository import AutomationStore
from features.automation.service import AutomationService
from foundation.error_model import ApiError


def request_for(user):
    request = Request({'type': 'http', 'method': 'POST', 'path': '/', 'headers': []})
    request.state.current_user = user
    return request


@pytest.fixture
def context(tmp_path):
    user = CurrentUser(id='alice', username='alice', role='admin')
    service = AutomationService(store=AutomationStore(tmp_path / 'runs.sqlite3'), profiles_path=tmp_path / 'profiles.json')
    planner = AutomationPlanner(service, owner_resolver=lambda _: user)
    body = PlanRequest(name='Nightly', runs=[{'test_plan': {
        'test_type': 'CTS', 'test_suite': '/opt/cts', 'flash': {'mode': 'skip'}, 'worker_id': 'worker-1',
    }}])
    with patch.object(plan_api, 'planner', return_value=planner):
        yield planner, service, user, body


def test_plan_api_owner_scope_and_revision_checks(context):
    _, service, owner, body = context
    request = request_for(owner)
    created = plan_api.save_plan(body, request)['data']
    assert plan_api.list_plans(request)['data']['items'][0]['id'] == created['id']
    assert not service.list_runs()
    outsider = request_for(CurrentUser(id='bob', username='bob', role='admin'))
    assert plan_api.list_plans(outsider)['data']['items'] == []
    with pytest.raises(ApiError) as exc:
        plan_api.run_plan(created['id'], outsider)
    assert exc.value.code == 'NOT_FOUND'
    with pytest.raises(ApiError) as exc:
        plan_api.update_plan(created['id'], body, request)
    assert exc.value.code == 'STATE_CONFLICT'
    settings = PlanSettingsRequest(name='New name', version=created['version'])
    updated = plan_api.update_plan_settings(created['id'], settings, request)['data']
    assert updated['name'] == 'New name'
    assert updated['version'] == 2
    assert 'template_ciphertext' not in updated
    plan_api.run_plan(created['id'], request)
    batches = plan_api.list_plan_batches(created['id'], request)['data']['items']
    assert len(batches) == 1
    assert 'template_ciphertext' not in batches[0]
    with pytest.raises(ApiError) as exc:
        plan_api.run_plan(created['id'], request)
    assert exc.value.code == 'STATE_CONFLICT'
    plan_api.stop_plan_batch(created['id'], request)
    assert service.list_runs()[0]['status'] == 'queued'


@pytest.mark.parametrize(('actor', 'auth_method'), [
    (CurrentUser(id='agent:x', username='agent', role='agent_service'), 'agent_token'),
    (CurrentUser(id='automation:x', username='machine', role='automation'), 'machine_authority'),
])
def test_all_plan_mutations_refuse_agent_and_machine_principals(context, actor, auth_method):
    planner, _, owner, body = context
    plan = planner.save(body, owner)
    request = request_for(actor)
    request.state.auth_method = auth_method
    calls = [
        lambda: plan_api.save_plan(body, request),
        lambda: plan_api.update_plan(plan['id'], body, request),
        lambda: plan_api.run_plan(plan['id'], request),
        lambda: plan_api.set_plan_enabled(plan['id'], plan_api.PlanEnabledRequest(enabled=True, version=1), request),
        lambda: plan_api.stop_plan_batch(plan['id'], request),
    ]
    for call in calls:
        with pytest.raises(HTTPException) as exc:
            call()
        assert exc.value.status_code == 403
