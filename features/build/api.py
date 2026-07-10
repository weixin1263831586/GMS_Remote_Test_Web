from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Query

from features.build.repository import BuildStore
from features.build.service import BuildExecutionError, BuildNotFoundError, BuildService
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
        )
    except (BuildExecutionError, BuildNotFoundError, ValueError) as exc:
        return error_response(str(exc), 400)
    return {"success": True, "data": {"items": items}}


@router.post("/jobs")
async def create_build_job(req: dict[str, Any], start: bool = Query(True)):
    try:
        job = build_service.create_job(req, start=start)
    except (BuildExecutionError, BuildNotFoundError, ValueError) as exc:
        return error_response(str(exc), 400)
    return {"success": True, "data": job}


@router.get("/jobs")
async def list_build_jobs(status: str = Query(""), limit: int = Query(50, ge=1, le=500)):
    return {"success": True, "data": {"items": build_service.list_jobs(status=status, limit=limit)}}


@router.get("/jobs/{job_id}")
async def get_build_job(job_id: str, poll: bool = Query(False)):
    try:
        job = build_service.poll_job(job_id) if poll else build_service.get_job(job_id)
    except BuildNotFoundError as exc:
        return error_response(str(exc), 404)
    return {"success": True, "data": job}


@router.post("/jobs/{job_id}/poll")
async def poll_build_job(job_id: str):
    try:
        job = build_service.poll_job(job_id)
    except BuildNotFoundError as exc:
        return error_response(str(exc), 404)
    return {"success": True, "data": job}


@router.post("/jobs/{job_id}/password")
async def set_build_job_password(job_id: str, req: dict[str, Any]):
    try:
        job = build_service.set_job_password(
            job_id,
            str(req.get("server_password") or ""),
        )
    except BuildNotFoundError as exc:
        return error_response(str(exc), 404)
    return {"success": True, "data": job}


@router.get("/jobs/{job_id}/log")
async def tail_build_log(job_id: str, lines: int = Query(200, ge=1, le=5000)):
    try:
        text = build_service.tail_log(job_id, lines=lines)
    except BuildNotFoundError as exc:
        return error_response(str(exc), 404)
    return {"success": True, "data": {"text": text}}


@router.post("/jobs/{job_id}/cancel")
async def cancel_build_job(job_id: str):
    try:
        job = build_service.cancel_job(job_id)
    except BuildNotFoundError as exc:
        return error_response(str(exc), 404)
    return {"success": True, "data": job}
