"""Attempt-bound module telemetry; archived reports remain the result authority."""

from __future__ import annotations

from datetime import datetime, timezone


def run_progress(run: dict, cluster) -> dict:
    if not run.get('cluster_job_id'):
        return {'modules': [], 'preparations': [], 'available': False}
    repository = cluster.repository
    job = repository.get_job(run['cluster_job_id'])
    if (not job or job.get('owner_id') != run.get('created_by')
            or job.get('current_attempt_id') != run.get('attempt_id')):
        return {'modules': [], 'preparations': [], 'available': False}
    modules, preparations = {}, {}
    for event in repository.recent_job_progress(job['id'], run['attempt_id']):
        progress = (event.get('payload') or {}).get('progress')
        if not isinstance(progress, dict):
            continue
        if progress.get('kind') == 'module' and progress.get('module'):
            key = (str(progress.get('module'))[:256], str(progress.get('abi') or '')[:32])
            previous = modules.get(key, {})
            modules[key] = {**previous, **{k: progress[k] for k in ('module', 'abi', 'state', 'passed', 'failed') if k in progress},
                            'first_reported_at': previous.get('first_reported_at', event['created_at']),
                            'last_reported_at': event['created_at']}
        elif progress.get('kind') == 'preparation':
            key = (str(progress.get('action') or '')[:64], str(progress.get('device') or '')[:256])
            preparations[key] = {k: progress.get(k, '') for k in ('action', 'device', 'state')}
    for module in modules.values():
        try:
            started = datetime.fromisoformat(module['first_reported_at'].replace('Z', '+00:00'))
            ended = datetime.now(timezone.utc) if module.get('state') == 'running' else datetime.fromisoformat(module['last_reported_at'].replace('Z', '+00:00'))
            module['observed_seconds'] = max(0, int((ended - started).total_seconds()))
        except (ValueError, TypeError):
            module['observed_seconds'] = None
    return {'modules': list(modules.values()), 'preparations': list(preparations.values()),
            'available': bool(modules or preparations), 'last_output_at': repository.job_last_output_at(job['id'], run['attempt_id']),
            'observed_module_count': len(modules), 'partial': True}
