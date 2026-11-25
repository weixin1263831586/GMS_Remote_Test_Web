"""Authenticated radar history and admin-owned scanning/notification settings."""

from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, Response

from features.auth import access as auth_access
from features.auth import is_elevated, require_authenticated_user_when_auth_required, require_role_when_auth_required
from foundation.config import settings as runtime_settings

from .models import CATEGORIES, RadarConfig
from .service import get_service


router = APIRouter()
READ = Depends(require_authenticated_user_when_auth_required)
ADMIN = Depends(require_role_when_auth_required('admin'))


@router.get('/gms-update-radar', response_class=HTMLResponse)
def radar_page(_user=READ):
    html = Path(__file__).with_name('ui').joinpath('page.html').read_text(encoding='utf-8')
    dialog = runtime_settings.project_root / 'web/shell/dialogs/update-radar-settings.html'
    html = html.replace('{{RADAR_SETTINGS_DIALOG}}', dialog.read_text(encoding='utf-8'))
    return HTMLResponse(html,
                        headers={'Cache-Control': 'no-cache'})


@router.get('/gms-update-radar/page.js')
def radar_script():
    return Response(Path(__file__).with_name('ui').joinpath('page.js').read_text(encoding='utf-8'),
                    media_type='application/javascript', headers={'Cache-Control': 'no-cache'})


@router.get('/api/gms-update-radar/status')
def status(request: Request, response: Response, _user=READ):
    # The capability is session-specific; do not cache it across account changes.
    response.headers['Cache-Control'] = 'no-store'
    config = get_service().store.settings()
    # Read-only capability flag. Match the actual admin dependency, including
    # anonymous dev mode and a temporarily elevated human session. Never expose
    # private notification settings or use GET /settings as a role probe.
    can_manage = (not auth_access.authentication_required()
                  or (_user is not None and (_user.role == 'admin' or is_elevated(request))))
    return {'success': True, 'data': {
        **{k: config[k] for k in ('enabled', 'scan_time', 'timezone', 'next_run_at', 'tasks')},
        'can_manage': can_manage,
    }}


@router.get('/api/gms-update-radar/settings')
def settings(_user=ADMIN):
    return {'success': True, 'data': get_service().store.settings()}


@router.put('/api/gms-update-radar/settings')
def save_settings(body: RadarConfig, user=Depends(require_role_when_auth_required('admin'))):
    config = get_service().store.save_settings(body.model_dump(mode='json'),
                                              getattr(user, 'resource_owner_id', ''), getattr(user, 'username', ''))
    return {'success': True, 'data': config}


@router.post('/api/gms-update-radar/scan')
def scan(user=Depends(require_role_when_auth_required('admin'))):
    run_id = get_service().start_scan(owner_id=getattr(user, 'resource_owner_id', ''), username=getattr(user, 'username', ''))
    return {'success': True, 'data': {'run_id': run_id}}


@router.get('/api/gms-update-radar/runs')
def history(limit: int = Query(30, ge=1, le=100), offset: int = Query(0, ge=0),
            kind: Literal['all', 'daily', 'weekly', 'monthly'] = 'all', _user=READ):
    return {'success': True, 'data': {**get_service().store.history(limit, offset, kind), 'categories': CATEGORIES}}


@router.get('/api/gms-update-radar/runs/{run_id}')
def detail(run_id: int, limit: int = Query(100, ge=1, le=200), offset: int = Query(0, ge=0), _user=READ):
    service = get_service()
    run = service.store.run(run_id)
    run.pop('config')
    return {'success': True, 'data': {'run': run, 'changes': service.store.changes(run_id, limit, offset)}}


@router.get('/api/gms-update-radar/runs/{run_id}/email')
def email_preview(run_id: int, _user=ADMIN):
    service = get_service()
    subject, body = service.preview(run_id)
    return {'success': True, 'data': {'subject': subject, 'body': body,
                                     'html': service.preview_html(run_id)}}


@router.post('/api/gms-update-radar/runs/{run_id}/email')
async def retry_email(run_id: int, _user=Depends(require_role_when_auth_required('admin'))):
    await run_in_threadpool(get_service().notify, run_id, retry=True)
    return {'success': True, 'data': {'email_status': get_service().store.run(run_id)['email_status']}}
