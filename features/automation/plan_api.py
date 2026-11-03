"""Human-only, owner-scoped test plan configuration and explicit dispatch."""

from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from features.auth import require_human_principal
from foundation.error_model import ApiError

from .plan_models import PlanSettingsRequest, TestPlanRequest
from .plan_store import PlanConflictError
from .plan_tools import bulk_settings, export_plan, import_plan
from .planning import AutomationPlanner, public_plan


router = APIRouter(prefix='/plans')


class PlanEnabledRequest(BaseModel):
    enabled: bool
    version: int = Field(ge=1)


class PlanSelection(BaseModel):
    id: str = Field(min_length=1, max_length=128)
    version: int = Field(ge=1)


class PlanBulkRequest(BaseModel):
    selections: list[PlanSelection] = Field(min_length=1, max_length=100)
    changes: dict


def planner() -> AutomationPlanner:
    from .api import automation_service

    return AutomationPlanner(automation_service)


def plan_error(exc: Exception) -> ApiError:
    if isinstance(exc, LookupError):
        return ApiError.not_found('Test plan not found')
    if isinstance(exc, PlanConflictError):
        return ApiError.conflict(str(exc), next_actions=[{'action': 'Reload the test plan and try again'}])
    return ApiError.malformed_request(str(exc))


@router.get('')
def list_plans(request: Request, label: str = ''):
    principal = require_human_principal(request)
    items = planner().store.list(owner=principal.resource_owner_id, label=label)
    return {'success': True, 'data': {'items': [public_plan(item) for item in items]}}


@router.post('')
def save_plan(req: TestPlanRequest, request: Request):
    principal = require_human_principal(request)
    try:
        item = planner().save(req, principal)
    except (ValueError, LookupError) as exc:
        raise plan_error(exc) from exc
    return {'success': True, 'data': item}


@router.put('/{plan_id}')
def update_plan(plan_id: str, req: TestPlanRequest, request: Request):
    principal = require_human_principal(request)
    try:
        item = planner().save(req, principal, plan_id=plan_id)
    except (ValueError, LookupError) as exc:
        raise plan_error(exc) from exc
    return {'success': True, 'data': item}


@router.post('/{plan_id}/enabled')
def set_plan_enabled(plan_id: str, req: PlanEnabledRequest, request: Request):
    principal = require_human_principal(request)
    try:
        item = planner().enabled(plan_id, principal, enabled=req.enabled, version=req.version)
    except (ValueError, LookupError) as exc:
        raise plan_error(exc) from exc
    return {'success': True, 'data': item}


@router.put('/{plan_id}/settings')
def update_plan_settings(plan_id: str, req: PlanSettingsRequest, request: Request):
    principal = require_human_principal(request)
    try:
        item = planner().settings(plan_id, req, principal)
    except (ValueError, LookupError) as exc:
        raise plan_error(exc) from exc
    return {'success': True, 'data': item}


@router.post('/{plan_id}/run')
def run_plan(plan_id: str, request: Request):
    principal = require_human_principal(request)
    try:
        batch_id = planner().start(plan_id, principal)
    except (ValueError, LookupError) as exc:
        raise plan_error(exc) from exc
    return {'success': True, 'data': {'batch_id': batch_id}}


@router.post('/{plan_id}/stop')
def stop_plan_batch(plan_id: str, request: Request):
    principal = require_human_principal(request)
    service = planner()
    try:
        service.owned(plan_id, principal)
        service.store.stop_batch(plan_id)
    except (ValueError, LookupError) as exc:
        raise plan_error(exc) from exc
    return {'success': True, 'data': {'stopped_future_steps': True}}


@router.get('/{plan_id}/batches')
def list_plan_batches(plan_id: str, request: Request):
    principal = require_human_principal(request)
    service = planner()
    try:
        service.owned(plan_id, principal)
        batches = service.store.batches(plan_id)
    except (ValueError, LookupError) as exc:
        raise plan_error(exc) from exc
    return {'success': True, 'data': {'items': [
        {key: value for key, value in item.items() if key not in ('template_ciphertext', 'username')}
        for item in batches
    ]}}


@router.get('/{plan_id}/export')
def export_test_plan(plan_id: str, request: Request):
    principal = require_human_principal(request)
    try:
        return {'success': True, 'data': export_plan(planner(), plan_id, principal)}
    except (ValueError, LookupError) as exc:
        raise plan_error(exc) from exc


@router.post('/import')
def import_test_plan(body: dict, request: Request):
    principal = require_human_principal(request)
    try:
        return {'success': True, 'data': import_plan(planner(), body, principal)}
    except (ValueError, LookupError) as exc:
        raise plan_error(exc) from exc


@router.post('/bulk-settings')
def update_test_plans(body: PlanBulkRequest, request: Request):
    principal = require_human_principal(request)
    if set(body.changes) - {'enabled', 'schedule', 'labels', 'stop_on_failure'}:
        raise ApiError.malformed_request('Unsupported bulk plan changes')
    try:
        return {'success': True, 'data': bulk_settings(planner(), [entry.model_dump() for entry in body.selections], principal, body.changes)}
    except (ValueError, LookupError) as exc:
        raise plan_error(exc) from exc
