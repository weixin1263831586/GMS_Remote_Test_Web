"""Correlated state and command timeline for Cluster Jobs."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request

from features.auth import require_resource_owner

from .api import service


router = APIRouter()


@router.get("/jobs/{job_id}/timeline")
def list_job_timeline(
    job_id: str,
    request: Request,
    after: int = Query(default=0, ge=0),
    limit: int = Query(default=500, ge=1, le=2000),
):
    job = service().repository.get_job(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    require_resource_owner(
        request,
        job.get("owner_id"),
        not_found_detail="job not found",
    )
    return {
        "success": True,
        "trace_id": job.get("trace_id", ""),
        "events": service().repository.list_timeline(
            job_id=job_id, after=after, limit=limit
        ),
    }
