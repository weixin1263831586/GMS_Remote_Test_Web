"""Sanitized plan exchange and atomic owner-scoped bulk schedule updates."""

from __future__ import annotations

import json
from urllib.parse import urlsplit, urlunsplit

from foundation.redaction import redact_sensitive_text

from .models import AutomationRunCreateRequest
from .plan_models import PlanSettingsRequest, TestPlanRequest
from .plan_store import PlanConflictError
from .planning import json_object


def clean_export(value):
    if isinstance(value, dict):
        return {key: clean_export(item) for key, item in value.items()
                if not any(word in key.lower() for word in ('password', 'token', 'secret', 'credential', 'private_key'))}
    if isinstance(value, list):
        return [clean_export(item) for item in value]
    if isinstance(value, str):
        if value.startswith(('http://', 'https://')):
            parsed = urlsplit(value)
            host = parsed.netloc.rsplit('@', 1)[-1]
            return urlunsplit((parsed.scheme, host, parsed.path, '', ''))
        return redact_sensitive_text(value)
    return value


def export_plan(planner, plan_id, principal):
    plan = planner.owned(plan_id, principal)
    runs = []
    for step in planner._steps(plan):
        run = step['run']
        body = {key: run[key] for key in AutomationRunCreateRequest.model_fields if key in run}
        body.update(test_plan=json_object(run['test_plan_json']), devices=json.loads(run['devices_json']),
                    source_type='manual', source_key='')
        runs.append(clean_export(body))
    return {'schema_version': 1, 'plan': {
        'name': plan['name'], 'schedule': json_object(plan['schedule_json']), 'enabled': False,
        'stop_on_failure': bool(plan['stop_on_failure']), 'labels': json.loads(plan.get('labels_json') or '[]'),
        'runs': runs,
    }}


def import_plan(planner, payload, principal):
    if payload.get('schema_version') != 1 or not isinstance(payload.get('plan'), dict):
        raise ValueError('Unsupported plan export format')
    body = clean_export(payload['plan'])
    body.update(enabled=False, version=0)
    return planner.save(TestPlanRequest.model_validate(body), principal)


def bulk_settings(planner, selections: list, principal, changes: dict):
    if not selections or len(selections) > 100:
        raise ValueError('Select between 1 and 100 plans')
    if len({entry['id'] for entry in selections}) != len(selections):
        raise ValueError('Duplicate plan selection')
    updates = []
    for selection in selections:
        old = planner.owned(selection['id'], principal)
        body = {'name': old['name'], 'enabled': bool(old['enabled']),
                'stop_on_failure': bool(old['stop_on_failure']), 'schedule': json_object(old['schedule_json']),
                'labels': json.loads(old.get('labels_json') or '[]'), 'version': selection['version'], **changes}
        settings = PlanSettingsRequest.model_validate(body)
        if settings.enabled:
            for step in planner._steps(old):
                planner._authorize(step['run'])
        updates.append((old, settings))
    with planner.service.store._connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        for old, settings in updates:
            current = conn.execute('SELECT version,created_by FROM automation_test_plans WHERE id=?', (old['id'],)).fetchone()
            if not current or current['version'] != settings.version or current['created_by'] != principal.resource_owner_id:
                raise PlanConflictError('A selected plan changed; reload before applying bulk changes')
        for old, settings in updates:
            conn.execute('''UPDATE automation_test_plans SET enabled=?,stop_on_failure=?,schedule_json=?,labels_json=?,
                next_run_at=?,version=version+1 WHERE id=?''',
                         (settings.enabled, settings.stop_on_failure, settings.schedule.model_dump_json(),
                          json.dumps(settings.labels, ensure_ascii=False),
                          settings.schedule.next_after(planner.clock()) if settings.enabled else '', old['id']))
    return {'updated': len(updates)}
