"""Per-run queue and idle budgets, enforced through the existing cancellation path."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from foundation.test_execution_options import validate_execution_options


def run_options(run: dict) -> dict:
    try:
        value = json.loads(run.get('test_plan_json') or '{}')
        return validate_execution_options(value if isinstance(value, dict) else {})
    except (ValueError, TypeError):
        return validate_execution_options({})


def elapsed(value: str) -> float | None:
    try:
        started = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return (datetime.now(timezone.utc) - started.replace(tzinfo=started.tzinfo or timezone.utc)).total_seconds()
    except (ValueError, AttributeError):
        return None


def enforce_extra_timeouts(service, cfg: dict) -> int:
    cancelled = 0
    for run in service.list_runs(limit=500):
        options = run_options(run)
        reason = ''
        if run['status'] == 'queued' and options['queue_timeout_seconds']:
            age = elapsed(max(run.get('created_at') or '', run.get('not_before') or ''))
            if age is not None and age > options['queue_timeout_seconds']:
                reason = 'Run queue timeout exceeded'
        elif run['status'] == 'test_running' and options['output_idle_timeout_seconds'] and run.get('cluster_job_id'):
            from features.cluster import get_cluster_service

            try:
                repo = get_cluster_service().repository
                job = repo.get_job(run['cluster_job_id'])
                if not job or job.get('current_attempt_id') != run.get('attempt_id') or job.get('owner_id') != run.get('created_by'):
                    continue
                last = repo.job_last_output_at(job['id'], run['attempt_id'])
                if not last:
                    # Start measuring at test acceptance, rather than firmware/build time.
                    last = next((event['created_at'] for event in service.list_events(run['id'])
                                 if event.get('stage') == 'test_running'), '')
                age = elapsed(last)
                if age is not None and age > options['output_idle_timeout_seconds']:
                    reason = 'Test output idle timeout exceeded'
            except (RuntimeError, AttributeError):
                continue
        if reason:
            service.orchestrator(cfg['executor']).cancel_run(run['id'], reason=reason, cleanup=True)
            cancelled += 1
    return cancelled
