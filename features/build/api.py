from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Query, Request

from features.auth import (
    get_authenticated_user,
    principal_owner_id,
    require_authenticated_user_when_auth_required,
)
from features.build.repository import BuildStore
from features.build.service import BuildExecutionError, BuildNotFoundError, BuildService
from features.users import owner_id_from_request
from foundation.config import settings
from foundation.responses import error_response


router = APIRouter(prefix="/api/build")


def _default_config_path() -> Path:
    configured = settings.project_root / "configs/build_servers.json"
    if configured.exists():
        return configured
    return settings.project_root / "configs/build_servers.example.json"


build_service = BuildService(
    store=BuildStore(settings.data_root / "build/build.sqlite3"),
    config_path=_default_config_path(),
)


def configure_build_service(service: BuildService) -> None:
    global build_service
    build_service = service


def _request_owner(request: Request) -> tuple[str, bool]:
    user = get_authenticated_user(request)
    if user is None:
        user = require_authenticated_user_when_auth_required(request)
    if user is None:
        return owner_id_from_request(request), False
    if user.role == "admin":
        return "", True
    return user.id, False


def _owned_build_job(job_id: str, request: Request) -> dict[str, Any]:
    job = build_service.get_job(job_id)
    owner, see_all = _request_owner(request)
    if not see_all and job.get("owner") != owner:
        raise BuildNotFoundError("Build job not found")
    return job


@router.get("/servers")
async def list_build_servers():
    return {"success": True, "data": {"items": build_service.list_servers()}}


@router.get("/templates")
async def list_build_templates(enabled_only: bool = Query(False)):
    return {"success": True, "data": {"items": build_service.list_templates(enabled_only=enabled_only)}}


@router.post("/discover/workspaces")
async def discover_build_workspaces(req: dict[str, Any]):
    try:
        items = build_service.discover_workspaces(
            str(req.get("server_id") or ""),
            server_password=str(req.get("server_password") or ""),
            base_dir=str(req.get("base_dir") or ""),
        )
    except (BuildExecutionError, BuildNotFoundError, ValueError) as exc:
        return error_response(str(exc), 400)
    return {"success": True, "data": {"items": items}}


@router.post("/discover/lunch-options")
async def discover_lunch_options(req: dict[str, Any]):
    try:
        items = build_service.discover_lunch_options(
            str(req.get("server_id") or ""),
            str(req.get("workspace") or ""),
            server_password=str(req.get("server_password") or ""),
            force_refresh=bool(req.get("force_refresh", False)),
        )
    except (BuildExecutionError, BuildNotFoundError, ValueError) as exc:
        return error_response(str(exc), 400)
    return {"success": True, "data": {"items": items}}


@router.post("/jobs")
async def create_build_job(
    req: dict[str, Any],
    request: Request,
    start: bool = Query(True),
):
    try:
        body = dict(req or {})
        body["owner"] = principal_owner_id(request)
        job = build_service.create_job(body, start=start)
    except (BuildExecutionError, BuildNotFoundError, ValueError) as exc:
        return error_response(str(exc), 400)
    return {"success": True, "data": job}


@router.get("/jobs")
async def list_build_jobs(
    request: Request,
    status: str = Query(""),
    limit: int = Query(50, ge=1, le=500),
):
    jobs = build_service.list_jobs(status=status, limit=limit)
    owner, see_all = _request_owner(request)
    if not see_all:
        jobs = [job for job in jobs if job.get("owner") == owner]
    return {"success": True, "data": {"items": jobs}}


@router.get("/jobs/{job_id}")
async def get_build_job(
    job_id: str,
    request: Request,
    poll: bool = Query(False),
):
    try:
        _owned_build_job(job_id, request)
        job = build_service.poll_job(job_id) if poll else build_service.get_job(job_id)
    except BuildNotFoundError as exc:
        return error_response(str(exc), 404)
    except BuildExecutionError as exc:
        return error_response(str(exc), 503)
    return {"success": True, "data": job}


@router.post("/jobs/{job_id}/poll")
async def poll_build_job(job_id: str, request: Request):
    try:
        _owned_build_job(job_id, request)
        job = build_service.poll_job(job_id)
    except BuildNotFoundError as exc:
        return error_response(str(exc), 404)
    except BuildExecutionError as exc:
        return error_response(str(exc), 503)
    return {"success": True, "data": job}


@router.post("/jobs/{job_id}/password")
async def set_build_job_password(
    job_id: str,
    req: dict[str, Any],
    request: Request,
):
    try:
        _owned_build_job(job_id, request)
        job = build_service.set_job_password(
            job_id,
            str(req.get("server_password") or ""),
        )
    except BuildNotFoundError as exc:
        return error_response(str(exc), 404)
    return {"success": True, "data": job}


@router.get("/jobs/{job_id}/log")
async def tail_build_log(
    job_id: str,
    request: Request,
    lines: int = Query(200, ge=1, le=5000),
):
    try:
        _owned_build_job(job_id, request)
        text = build_service.tail_log(job_id, lines=lines)
    except BuildNotFoundError as exc:
        return error_response(str(exc), 404)
    except BuildExecutionError as exc:
        return error_response(str(exc), 503)
    return {"success": True, "data": {"text": text}}


@router.post("/jobs/{job_id}/cancel")
async def cancel_build_job(job_id: str, request: Request):
    try:
        _owned_build_job(job_id, request)
        job = build_service.cancel_job(job_id)
    except BuildNotFoundError as exc:
        return error_response(str(exc), 404)
    except BuildExecutionError as exc:
        return error_response(str(exc), 503)
    return {"success": True, "data": job}


@router.delete("/jobs/{job_id}")
async def delete_build_job(job_id: str, request: Request):
    try:
        _owned_build_job(job_id, request)
        build_service.delete_job(job_id)
    except BuildNotFoundError as exc:
        return error_response(str(exc), 404)
    except BuildExecutionError as exc:
        return error_response(str(exc), 409)
    return {"success": True, "data": {"deleted": True, "id": job_id}}
