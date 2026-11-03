"""Timeouts cancel only the matching run/attempt through the existing orchestrator."""

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from features.automation.execution_policy import enforce_extra_timeouts, run_options


def service_for(run):
    cancel = Mock()
    service = SimpleNamespace(list_runs=lambda **_: [run],
                              orchestrator=lambda _: SimpleNamespace(cancel_run=cancel),
                              list_events=lambda _: [{'stage': 'test_running', 'created_at': 'accepted'}])
    return service, cancel


def test_queue_budget_starts_after_scheduled_delay(monkeypatch):
    run = {'id': 'run', 'status': 'queued', 'created_at': '2026-01-01', 'not_before': '2026-01-02',
           'test_plan_json': json.dumps({'queue_timeout_seconds': 60})}
    service, cancel = service_for(run)
    monkeypatch.setattr('features.automation.execution_policy.elapsed', lambda value: -10 if value == '2026-01-02' else 3600)
    assert enforce_extra_timeouts(service, {'executor': 'cluster'}) == 0
    cancel.assert_not_called()
    monkeypatch.setattr('features.automation.execution_policy.elapsed', lambda _: 61)
    assert enforce_extra_timeouts(service, {'executor': 'cluster'}) == 1
    cancel.assert_called_once_with('run', reason='Run queue timeout exceeded', cleanup=True)


@pytest.mark.parametrize('owner,attempt,expected', [('alice', 'a', 1), ('bob', 'a', 0), ('alice', 'old', 0)])
def test_idle_budget_is_owner_and_attempt_bound(monkeypatch, owner, attempt, expected):
    run = {'id': 'run', 'status': 'test_running', 'created_by': 'alice', 'cluster_job_id': 'job', 'attempt_id': 'a',
           'test_plan_json': json.dumps({'output_idle_timeout_seconds': 60})}
    repo = SimpleNamespace(get_job=lambda _: {'id': 'job', 'owner_id': owner, 'current_attempt_id': attempt},
                           job_last_output_at=lambda *_: '')
    monkeypatch.setattr('features.cluster.get_cluster_service', lambda: SimpleNamespace(repository=repo))
    monkeypatch.setattr('features.automation.execution_policy.elapsed', lambda value: 61 if value == 'accepted' else 0)
    service, cancel = service_for(run)
    assert enforce_extra_timeouts(service, {'executor': 'cluster'}) == expected
    assert cancel.call_count == expected


def test_default_and_invalid_historical_options_keep_custom_budgets_disabled():
    for value in ('{}', 'null', '{bad', '{"execution_timeout_seconds":true}'):
        options = run_options({'test_plan_json': value})
        assert options['queue_timeout_seconds'] == options['output_idle_timeout_seconds'] == 0
