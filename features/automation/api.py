from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Query
from fastapi.responses import HTMLResponse

from features.automation.repository import AutomationStore
from features.automation.service import (
    AutomationNotFoundError,
    AutomationService,
)
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
)


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
    return HTMLResponse(html)


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
async def create_automation_run(req: dict[str, Any]):
    return {'success': True, 'data': automation_service.create_run(req)}


@router.get('/runs')
async def list_automation_runs(
    status: str = Query(''),
    limit: int = Query(50, ge=1, le=500),
):
    return {
        'success': True,
        'data': {
            'items': automation_service.list_runs(
                status=status,
                limit=limit,
            )
        },
    }


@router.get('/runs/{run_id}')
async def get_automation_run(run_id: str):
    try:
        run = automation_service.get_run(run_id)
    except AutomationNotFoundError as exc:
        return error_response(str(exc), 404)
    return {'success': True, 'data': run}


@router.get('/runs/{run_id}/events')
async def get_automation_run_events(run_id: str):
    try:
        events = automation_service.list_events(run_id)
    except AutomationNotFoundError as exc:
        return error_response(str(exc), 404)
    return {'success': True, 'data': {'items': events}}


@router.post('/runs/{run_id}/cancel')
async def cancel_automation_run(run_id: str):
    try:
        run = automation_service.cancel_run(run_id)
    except AutomationNotFoundError as exc:
        return error_response(str(exc), 404)
    return {'success': True, 'data': run}


@router.post('/runs/{run_id}/retry')
async def retry_automation_run(run_id: str):
    try:
        run = automation_service.retry_run(run_id)
    except AutomationNotFoundError as exc:
        return error_response(str(exc), 404)
    return {'success': True, 'data': run}


@router.post('/worker/tick')
async def automation_worker_tick(executor: str = Query('stub')):
    return {
        'success': True,
        'data': automation_service.worker_tick(executor),
    }


@router.post('/gerrit/webhook')
async def handle_gerrit_webhook(payload: dict[str, Any]):
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
