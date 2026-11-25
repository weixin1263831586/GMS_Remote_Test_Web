"""The single GMS update center HTTP router and scanning API."""
from fastapi import APIRouter, Depends, Query

from features.auth import require_authenticated_user_when_auth_required, require_role_when_auth_required
from foundation.error_model import ApiError

from . import mainline_queries, queries, service
from .sources import SOURCE_KEYS


router=APIRouter()


@router.post('/api/gms-update-center/scan')
def scan(mode: str=Query('incremental',pattern='^(full|incremental)$'),
         source: list[str]=Query(default=[]), _admin=Depends(require_role_when_auth_required('admin'))):
    if set(source)-set(SOURCE_KEYS):
        raise ApiError.malformed_request('unknown scan source')
    return {'success':True,'data':{'status':service.start(mode,source)}}


@router.get('/api/gms-update-center/scan/status')
def scan_status(_user=Depends(require_authenticated_user_when_auth_required)):
    return {'success':True,'data':{'status':service.status()}}


@router.get('/api/gms-update-center/scan/runs/{run_id}')
def scan_evidence(run_id: int,limit: int=Query(100,ge=1,le=500),offset: int=Query(0,ge=0),
                  _user=Depends(require_authenticated_user_when_auth_required)):
    data=service.documents(run_id,limit,offset)
    if data is None:
        raise ApiError.not_found('scan run not found')
    return {'success':True,'data':data}


# Domain query handlers and the page share the one registered API surface.
router.include_router(queries.router)
router.include_router(mainline_queries.router)
