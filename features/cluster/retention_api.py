"""Explicit human actions for previewed, owner-scoped artifact retention."""

from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from features.auth import require_human_principal

from .artifact_retention import ArtifactRetention


router = APIRouter(prefix='/retention')


def retention():
    from .api import service

    return ArtifactRetention(service().repository)


class RetentionPolicyRequest(BaseModel):
    enabled: bool = False
    archive_days: int = Field(default=7, ge=1, le=3650, strict=True)
    delete_days: int = Field(default=30, ge=0, le=3650, strict=True)


class RetentionSelection(BaseModel):
    id: str = Field(min_length=1, max_length=128)
    sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    operation: str = Field(pattern=r'^(archive|delete)$')


class RetentionApplyRequest(BaseModel):
    selections: list[RetentionSelection] = Field(min_length=1, max_length=25)


class PinRequest(BaseModel):
    pinned: bool


@router.get('/preview')
def preview_retention(request: Request):
    principal = require_human_principal(request)
    return {'success': True, 'data': retention().preview(principal.resource_owner_id)}


@router.put('/policy')
def update_retention_policy(body: RetentionPolicyRequest, request: Request):
    principal = require_human_principal(request)
    return {'success': True, 'data': retention().save_policy(principal, **body.model_dump())}


@router.post('/apply')
def apply_retention(body: RetentionApplyRequest, request: Request):
    principal = require_human_principal(request)
    return {'success': True, 'data': retention().apply(principal.resource_owner_id,
                                                      [item.model_dump() for item in body.selections])}


@router.post('/artifacts/{artifact_id}/pin')
def pin_artifact(artifact_id: str, body: PinRequest, request: Request):
    principal = require_human_principal(request)
    retention().pin(principal.resource_owner_id, artifact_id, body.pinned)
    return {'success': True, 'data': {'pinned': body.pinned}}
