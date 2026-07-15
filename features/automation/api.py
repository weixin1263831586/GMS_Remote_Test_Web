from __future__ import annotations

import asyncio
import hmac
import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse

from features.auth import get_authenticated_user
from features.automation.repository import AutomationStore
from features.automation.service import (
    AutomationNotFoundError,
    AutomationService,
)
from features.cluster import get_cluster_service
from features.users import owner_id_from_request
from foundation.config import settings
from foundation.responses import error_response


router = APIRouter(prefix='/api/automation')
page_router = APIRouter()


def _default_profiles_path() -> Path:
    configured = settings.project_root / 'configs/automation_profiles.json'
    if configured.exists():
        return configured
    return settings.project_root / 'configs/automation_profiles.example.json'


automation_service = AutomationService(
    store=AutomationStore(settings.data_root / 'automation/automation.sqlite3'),
    profiles_path=_default_profiles_path(),
    cluster_provider=get_cluster_service,
)


def _request_owner(request: Request | None) -> tuple[str, bool]:
    """Return the ATS owner filter and whether the caller may see all runs."""
    if request is None:
        return "", True
    user = get_authenticated_user(request)
    if user and user.role == "admin":
        return "", True
    return owner_id_from_request(request, default="anonymous"), False


def _owned_run(run_id: str, request: Request | None) -> dict[str, Any]:
    run = automation_service.get_run(run_id)
    owner, see_all = _request_owner(request)
    if not see_all and run.get("created_by") != owner:
        raise AutomationNotFoundError('Automation run not found')
    return run


def configure_automation_service(service: AutomationService) -> None:
    global automation_service
    automation_service = service


@page_router.get('/automation', response_class=HTMLResponse)
async def automation_page():
    ui_dir = Path(__file__).with_name('ui')
    html = (ui_dir / 'page.html').read_text(encoding='utf-8')
    html = html.replace(
        '{{AUTOMATION_CSS}}',
        (ui_dir / 'page.css').read_text(encoding='utf-8'),
    )
    html = html.replace(
        '{{AUTOMATION_JS}}',
        (ui_dir / 'page.js').read_text(encoding='utf-8'),
    )
    return HTMLResponse(html, headers={'Cache-Control': 'no-store, no-cache, must-revalidate'})


@router.get('/profiles')
async def list_automation_profiles(enabled_only: bool = Query(False)):
    return {
        'success': True,
        'data': {
            'items': automation_service.list_profiles(
                enabled_only=enabled_only
            )
        },
    }


@router.post('/profiles')
async def save_automation_profile(req: dict[str, Any]):
    try:
        profile = automation_service.save_profile(req)
    except ValueError as exc:
        return error_response(str(exc), 400)
    return {
        'success': True,
        'data': {
            'profile': profile,
            'items': automation_service.list_profiles(),
        },
    }


@router.put('/profiles/{profile_id}')
async def update_automation_profile(
    profile_id: str,
    req: dict[str, Any],
):
    body = dict(req or {})
    body['id'] = profile_id
    return await save_automation_profile(body)


@router.post('/profiles/{profile_id}/dry-run')
async def dry_run_automation_profile(
    profile_id: str,
    req: dict[str, Any],
):
    try:
        data = automation_service.dry_run_profile(profile_id, req)
    except AutomationNotFoundError as exc:
        return error_response(str(exc), 404)
    return {'success': True, 'data': data}


@router.post('/runs')
async def create_automation_run(
    req: dict[str, Any], request: Request = None
):
    try:
        owner, _ = _request_owner(request)
        run = automation_service.create_run(req, created_by=owner)
    except ValueError as exc:
        return error_response(str(exc), 400)
    return {'success': True, 'data': run}


@router.post('/runs/preflight')
async def preflight_automation_run(req: dict[str, Any]):
    try:
        data = automation_service.preflight(req)
    except ValueError as exc:
        return error_response(str(exc), 409)
    return {'success': True, 'data': data}


@router.get('/runs')
async def list_automation_runs(
    request: Request = None,
    status: str = Query(''),
    limit: int = Query(50, ge=1, le=500),
):
    owner, see_all = _request_owner(request)
    return {
        'success': True,
        'data': {
            'items': automation_service.list_run_summaries(
                status=status,
                limit=limit,
                created_by='' if see_all else owner,
            )
        },
    }


@router.get('/runs/{run_id}')
async def get_automation_run(run_id: str, request: Request = None):
    try:
        run = _owned_run(run_id, request)
    except AutomationNotFoundError as exc:
        return error_response(str(exc), 404)
    return {'success': True, 'data': run}


@router.get('/dashboard')
async def automation_dashboard(request: Request = None):
    """Aggregate run/build counts for the GMS ATS dashboard."""
    from collections import Counter

    from features.automation.models import TERMINAL_STATUSES
    from features.build import get_build_service

    owner, see_all = _request_owner(request)
    runs = automation_service.list_run_summaries(
        limit=500, created_by='' if see_all else owner
    )
    run_by_status = dict(Counter(run['status'] for run in runs))
    completed_total = sum(
        count for status, count in run_by_status.items() if status in TERMINAL_STATUSES
    )
    run_by_profile: dict[str, dict[str, int]] = {}
    for run in runs:
        bucket = run_by_profile.setdefault(run.get('profile_id') or 'manual', {})
        bucket[run['status']] = bucket.get(run['status'], 0) + 1

    try:
        build_jobs = get_build_service().list_jobs(limit=500)
        if not see_all:
            visible_runs = {run['id'] for run in runs}
            build_jobs = [
                job for job in build_jobs
                if job.get('automation_run_id') in visible_runs
            ]
    except Exception:
        build_jobs = []
    build_by_status = dict(Counter(job['status'] for job in build_jobs))

    return {
        'success': True,
        'data': {
            'run_total': len(runs),
            'run_by_status': run_by_status,
            'run_by_profile': run_by_profile,
            'completed_total': completed_total,
            'build_total': len(build_jobs),
            'build_by_status': build_by_status,
        },
    }


@router.get('/runs/{run_id}/events')
async def get_automation_run_events(
    run_id: str, request: Request = None
):
    try:
        _owned_run(run_id, request)
        events = automation_service.list_events(run_id)
    except AutomationNotFoundError as exc:
        return error_response(str(exc), 404)
    return {'success': True, 'data': {'items': events}}


@router.get('/runs/{run_id}/trace')
async def get_automation_run_trace(
    run_id: str, request: Request = None
):
    """Correlate a run with its build job, commit, artifact and report."""
    try:
        run = _owned_run(run_id, request)
    except AutomationNotFoundError as exc:
        return error_response(str(exc), 404)

    import json

    from features.build import get_build_service

    build_job = None
    build_job_id = run.get('jenkins_build_number') or ''
    if build_job_id:
        try:
            build_job = get_build_service().get_job(build_job_id)
        except Exception:
            build_job = None
    try:
        result_summary = json.loads(run.get('result_json') or '{}')
    except json.JSONDecodeError:
        result_summary = {}
    cluster_job = None
    if run.get('cluster_job_id'):
        try:
            cluster_job = get_cluster_service().repository.get_job(
                run['cluster_job_id']
            )
        except Exception:
            cluster_job = None
    return {
        'success': True,
        'data': {
            'run_id': run['id'],
            'profile_id': run['profile_id'],
            'status': run['status'],
            'build_job_id': build_job_id,
            'build_job': build_job,
            'artifact_path': run.get('artifact_path') or '',
            'build_artifact_id': run.get('build_artifact_id') or '',
            'worker_id': run.get('worker_id') or '',
            'device_reservation_id': run.get('device_reservation_id') or '',
            'flash_stage_id': run.get('flash_stage_id') or '',
            'flash_command_id': run.get('flash_command_id') or '',
            'cluster_job_id': run.get('cluster_job_id') or '',
            'attempt_id': run.get('attempt_id') or '',
            'cluster_job': cluster_job,
            'commit': {
                'gerrit_change_id': run.get('gerrit_change_id') or '',
                'branch': run.get('branch') or '',
                'gerrit_patchset': run.get('gerrit_patchset') or '',
                'gerrit_subject': run.get('gerrit_subject') or '',
            },
            'report_timestamp': run.get('report_timestamp') or '',
            'report_id': run.get('report_id') or '',
            'result_summary': result_summary,
        },
    }


@router.post('/runs/{run_id}/cancel')
async def cancel_automation_run(
    run_id: str, request: Request = None
):
    try:
        _owned_run(run_id, request)
        run = automation_service.cancel_run(run_id)
    except AutomationNotFoundError as exc:
        return error_response(str(exc), 404)
    except RuntimeError as exc:
        return error_response(str(exc), 409)
    return {'success': True, 'data': run}


@router.post('/runs/{run_id}/retry')
async def retry_automation_run(
    run_id: str, request: Request = None
):
    try:
        _owned_run(run_id, request)
        run = automation_service.retry_run(run_id)
    except AutomationNotFoundError as exc:
        return error_response(str(exc), 404)
    except ValueError as exc:
        return error_response(str(exc), 409)
    return {'success': True, 'data': run}


@router.post('/worker/tick')
async def automation_worker_tick(executor: str = Query('http', pattern='^(http|stub)$')):
    # worker_tick drives the whole automation state machine synchronously —
    # Jenkins trigger/poll, firmware flash (HTTP timeout up to 3600s), test
    # start, report analysis. Run it off the event loop or a single tick can
    # freeze the server for the duration of a flash.
    data = await asyncio.to_thread(automation_service.worker_tick, executor)
    return {
        'success': True,
        'data': data,
    }


@router.get('/worker/status')
async def automation_worker_status():
    from features.automation.worker import get_worker_status

    return {'success': True, 'data': get_worker_status()}


@router.post('/gerrit/webhook')
async def handle_gerrit_webhook(
    payload: dict[str, Any],
    request: Request = None,
    automation_token: str = Header(default='', alias='X-GMS-Automation-Token'),
):
    expected = os.getenv('GMS_AUTOMATION_WEBHOOK_TOKEN', '').strip()
    if expected and not hmac.compare_digest(automation_token, expected):
        raise HTTPException(status_code=401, detail='Invalid automation webhook token')
    if not expected and settings.environment == 'production' and request is not None:
        raise HTTPException(
            status_code=503,
            detail='GMS_AUTOMATION_WEBHOOK_TOKEN is required in production',
        )
    return {
        'success': True,
        'data': automation_service.handle_gerrit_webhook(payload),
    }


@router.post('/gerrit/poll')
async def poll_gerrit_changes(limit: int = Query(100, ge=1, le=500)):
    return {
        'success': True,
        'data': await automation_service.poll_gerrit_changes(limit),
    }
