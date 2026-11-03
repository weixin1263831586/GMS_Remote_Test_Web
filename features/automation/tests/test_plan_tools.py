"""Plan exchange strips authority and secrets; bulk changes are all-or-nothing."""

import pytest

from features.auth import CurrentUser
from features.automation.plan_models import TestPlanRequest as PlanRequest
from features.automation.plan_store import PlanConflictError
from features.automation.plan_tools import bulk_settings, export_plan, import_plan
from features.automation.planning import AutomationPlanner
from features.automation.repository import AutomationStore
from features.automation.service import AutomationService


@pytest.fixture
def context(tmp_path):
    user = CurrentUser(id='alice', username='alice', role='admin')
    service = AutomationService(store=AutomationStore(tmp_path / 'runs.sqlite3'), profiles_path=tmp_path / 'profiles.json')
    planner = AutomationPlanner(service, owner_resolver=lambda _: user)
    body = {'test_plan': {'test_type': 'CTS', 'test_suite': '/opt/cts', 'flash': {'mode': 'skip'},
                          'api_token': 'private-value'}, 'build_server_password': 'another-private-value',
            'artifact_url': 'https://user:password@example.com/image?token=secret'}
    plan = planner.save(PlanRequest(name='Nightly', labels=['rk3576', 'nightly'], runs=[body]), user)
    return planner, user, plan


def test_export_strips_credentials_and_import_stays_paused(context):
    planner, user, plan = context
    payload = export_plan(planner, plan['id'], user)
    serialized = str(payload)
    assert 'private-value' not in serialized
    assert 'password@' not in serialized
    assert '?token=' not in serialized
    assert 'granted_capabilities' not in serialized
    payload['plan']['enabled'] = True
    imported = import_plan(planner, payload, user)
    assert imported['enabled'] == 0
    assert imported['id'] != plan['id']
    assert imported['labels'] == ['nightly', 'rk3576']
    assert not planner.service.list_runs()


def test_label_filter_and_stale_bulk_update_are_owner_scoped(context):
    planner, user, plan = context
    other = import_plan(planner, export_plan(planner, plan['id'], user), user)
    assert len(planner.store.list(owner='alice', label='rk3576')) == 2
    assert not planner.store.list(owner='bob', label='rk3576')
    with pytest.raises(PlanConflictError):
        bulk_settings(planner, [{'id': plan['id'], 'version': 1}, {'id': other['id'], 'version': 999}], user,
                      {'labels': ['changed']})
    assert planner.store.get(plan['id'])['version'] == 1
    result = bulk_settings(planner, [{'id': plan['id'], 'version': 1}, {'id': other['id'], 'version': 1}], user,
                           {'labels': ['changed'], 'enabled': False})
    assert result['updated'] == 2
    assert len(planner.store.list(owner='alice', label='changed')) == 2
    assert planner.store.get(plan['id'])['version'] == 2
