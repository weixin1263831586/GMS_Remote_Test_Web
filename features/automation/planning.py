"""Human-authorized plan dispatch and bounded failed-case retries (ADR 0020)."""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timedelta, timezone

from features.auth import AGENT_ROLE, auth_service, automation_granted_capabilities, is_machine_principal
from foundation.secrets import decrypt_secret, encrypt_secret

from .models import TERMINAL_STATUSES, AutomationRunCreateRequest
from .plan_models import PlanSchedule, RetryPolicy, TestPlanRequest, utc_text
from .plan_store import PlanConflictError, PlanStore


def json_object(value: str) -> dict:
    try:
        result = json.loads(value or '{}')
    except (ValueError, TypeError):
        return {}
    return result if isinstance(result, dict) else {}


def public_plan(plan: dict) -> dict:
    return {key: value for key, value in plan.items() if key not in ('template_ciphertext', 'username', 'schedule_json', 'labels_json')} | {
        'schedule': json_object(plan['schedule_json']),
        'labels': json.loads(plan.get('labels_json') or '[]'),
    }


class AutomationPlanner:
    def __init__(self, service, *, owner_resolver=None, clock=None):
        self.service = service
        self.store = PlanStore(service.store)
        self.owner_resolver = owner_resolver or auth_service.get_enabled_user
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    @staticmethod
    def require_human(principal):
        if (principal is None or principal.role in (AGENT_ROLE, 'agent') or is_machine_principal(principal)
                or principal.actor_id != principal.resource_owner_id):
            raise ValueError('Test plans require an authenticated human owner')

    def owned(self, plan_id: str, principal) -> dict:
        self.require_human(principal)
        plan = self.store.get(plan_id)
        if plan is None or plan['created_by'] != principal.resource_owner_id:
            raise LookupError('Test plan not found')
        return plan

    def save(self, request: TestPlanRequest, principal, *, plan_id: str = '') -> dict:
        self.require_human(principal)
        if not request.name.strip():
            raise ValueError('Test plan name is required')
        if plan_id:
            self.owned(plan_id, principal)
        steps = []
        for body in request.runs:
            run, secrets = self.service.prepare_run(body, created_by=principal.resource_owner_id, principal=principal)
            steps.append({'run': run, 'secrets': secrets})
        now = self.clock()
        return public_plan(self.store.save({
            'id': plan_id or 'plan_' + uuid.uuid4().hex[:12],
            'created_by': principal.resource_owner_id, 'username': principal.username,
            'name': request.name.strip(), 'enabled': int(request.enabled),
            'stop_on_failure': int(request.stop_on_failure),
            'schedule_json': request.schedule.model_dump_json(),
            'labels_json': json.dumps(request.labels, ensure_ascii=False),
            'template_ciphertext': encrypt_secret(json.dumps(steps)), 'step_count': len(steps),
            'next_run_at': request.schedule.next_after(now) if request.enabled else '',
            'created_at': utc_text(now), 'updated_at': utc_text(now),
        }, version=request.version))

    def enabled(self, plan_id: str, principal, *, enabled: bool, version: int) -> dict:
        plan = self.owned(plan_id, principal)
        # Re-enabling requires current authority for all stored templates.
        if enabled:
            for step in self._steps(plan):
                self._authorize(step['run'])
        schedule = PlanSchedule.model_validate_json(plan['schedule_json'])
        return public_plan(self.store.set_enabled(plan_id, enabled,
                           schedule.next_after(self.clock()) if enabled else '', version=version))

    def settings(self, plan_id: str, request, principal) -> dict:
        plan = self.owned(plan_id, principal)
        if not request.name.strip():
            raise ValueError('Test plan name is required')
        if request.enabled:
            for step in self._steps(plan):
                self._authorize(step['run'])
        plan.update(name=request.name.strip(), enabled=int(request.enabled),
                    labels_json=json.dumps(request.labels, ensure_ascii=False),
                    stop_on_failure=int(request.stop_on_failure), schedule_json=request.schedule.model_dump_json(),
                    next_run_at=request.schedule.next_after(self.clock()) if request.enabled else '',
                    updated_at=utc_text(self.clock()))
        return public_plan(self.store.save(plan, version=request.version))

    @staticmethod
    def _steps(holder: dict) -> list[dict]:
        steps = json.loads(decrypt_secret(holder['template_ciphertext']))
        if not isinstance(steps, list) or not steps:
            raise ValueError('Stored test plan is invalid')
        return steps

    def _authorize(self, run: dict):
        principal = self.owner_resolver(run.get('created_by_username', ''))
        self.require_human(principal)
        if principal.resource_owner_id != run['created_by']:
            raise ValueError('Test plan owner is unavailable')
        required = set(automation_granted_capabilities(principal, json_object(run['test_plan_json'])))
        granted = set(filter(None, run.get('granted_capabilities', '').split(',')))
        if not required.issubset(granted):
            raise ValueError('Stored authorization does not cover the test plan')
        return principal

    def _materialize(self, template: dict, *, plan_id: str, batch_id: str, index: int) -> dict:
        base = template['run']
        self._authorize(base)
        body = {key: base[key] for key in AutomationRunCreateRequest.model_fields if key in base}
        body.update(test_plan=json_object(base['test_plan_json']), devices=json.loads(base['devices_json']))
        self.service._prepare_cluster_plan(body, body['test_plan'])
        run = AutomationRunCreateRequest(**body).to_run_dict(self.service.new_run_id())
        run.update(created_by=base['created_by'], created_by_username=base['created_by_username'],
                   granted_capabilities=base['granted_capabilities'], retry_limit=base['retry_limit'],
                   plan_id=plan_id, plan_batch_id=batch_id, source_type='schedule',
                   source_key=f'plan:{batch_id}:{index}')
        return {'run': run, 'secrets': template.get('secrets', {})}

    def start(self, plan_id: str, principal=None, *, due_plan: dict | None = None) -> str:
        plan = due_plan if due_plan is not None else self.owned(plan_id, principal)
        batch_id = 'batch_' + uuid.uuid4().hex[:12]
        first = None if due_plan is not None and plan['active_batch_id'] else self._materialize(
            self._steps(plan)[0], plan_id=plan['id'], batch_id=batch_id, index=0)
        now = self.clock()
        schedule = PlanSchedule.model_validate_json(plan['schedule_json'])
        started = self.store.start_batch(plan, batch_id, first, now=utc_text(now),
                                        next_run_at=schedule.next_after(now), due=due_plan is not None)
        return batch_id if started else ''

    def _retry(self, run: dict) -> tuple[str, dict | None]:
        limit, index = int(run.get('retry_limit') or 0), int(run.get('retry_index') or 0)
        if run['status'] != 'completed' or not limit:
            return 'done', None
        summary = json_object(run['test_summary_json'])
        if summary.get('outcome') != 'failed':
            return 'done', None
        if index >= limit:
            return 'retry_limit_reached', None
        folder = summary.get('retry_dir', '')
        if (not isinstance(folder, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,255}', folder)
                or not run.get('worker_id') or not run.get('report_id')
                or summary.get('retry_worker_id') != run['worker_id']):
            return 'missing_retry_context', None
        plan = json_object(run['test_plan_json'])
        if not plan.get('test_suite'):
            return 'missing_retry_context', None
        policy = RetryPolicy.model_validate(plan.get('retry_policy') or {})
        plan.update(flash={'mode': 'skip'}, retry_dir=folder, worker_id=run['worker_id'])
        for key in ('build', 'jenkins', 'test_module', 'test_case', 'modules'):
            plan.pop(key, None)
        principal = self._authorize({**run, 'test_plan_json': json.dumps(plan)})
        retry_capabilities = automation_granted_capabilities(principal, plan)
        child = AutomationRunCreateRequest(
            profile_id=run['profile_id'], project=run['project'], branch=run['branch'],
            artifact_path=run['artifact_path'], devices=json.loads(run['devices_json']), test_plan=plan,
        ).to_run_dict(self.service.new_run_id())
        root = run.get('retry_root_run_id') or run['id']
        child.update(created_by=run['created_by'], created_by_username=run['created_by_username'],
                     granted_capabilities=','.join(retry_capabilities), worker_id=run['worker_id'],
                     retry_of_run_id=run['id'], retry_root_run_id=root, retry_index=str(index + 1),
                     retry_limit=str(limit), source_type='auto_retry', source_key=f'retry:{root}:{index + 1}',
                     plan_id=run.get('plan_id', ''), plan_batch_id=run.get('plan_batch_id', ''),
                     not_before=utc_text(self.clock() + timedelta(seconds=policy.delay_seconds)),
                     build_artifact_id=run.get('build_artifact_id', ''))
        return 'retried', {'run': child, 'secrets': {}}

    def tick(self) -> dict[str, int]:
        counts = {'automatic_retries': 0, 'scheduled_batches': 0, 'advanced_batches': 0}
        for run in self.store.terminal_followups():
            try:
                decision, child = self._retry(run)
            except (ValueError, LookupError):
                decision, child = 'retry_authorization_unavailable', None
            counts['automatic_retries'] += self.store.finish_followup(run, decision=decision, prepared=child)
        for batch in self.store.batches(active_only=True):
            run = self.service.store.get_run(batch['last_run_id'])
            if not run or run['status'] not in TERMINAL_STATUSES:
                continue
            if run['followup_state'] in ('', 'retried'):
                continue
            failed = run['status'] != 'completed' or json_object(run['test_summary_json']).get('outcome') != 'passed'
            if batch['status'] == 'stopping' or run['status'] == 'cancelled':
                counts['advanced_batches'] += self.store.advance_batch(batch, None, status='cancelled')
            elif failed and batch['stop_on_failure']:
                counts['advanced_batches'] += self.store.advance_batch(batch, None, status='failed')
            elif batch['next_step'] >= batch['step_count']:
                counts['advanced_batches'] += self.store.advance_batch(batch, None, status='completed', step_failed=failed)
            else:
                try:
                    step = self._steps(batch)[batch['next_step']]
                    prepared = self._materialize(step, plan_id=batch['plan_id'], batch_id=batch['id'], index=batch['next_step'])
                    counts['advanced_batches'] += self.store.advance_batch(batch, prepared, step_failed=failed)
                except (ValueError, LookupError):
                    self.store.advance_batch(batch, None, status='failed')
        now = self.clock()
        for plan in self.store.due(utc_text(now)):
            try:
                counts['scheduled_batches'] += bool(self.start(plan['id'], due_plan=plan))
            except PlanConflictError:
                continue
            except (ValueError, LookupError):
                self.store.defer_error(plan, next_run_at=PlanSchedule.model_validate_json(plan['schedule_json']).next_after(now),
                                       message='Scheduled dispatch failed; check owner permissions and resource preflight')
        return counts
