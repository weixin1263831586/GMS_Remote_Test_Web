"""Cancellation endpoint for durable Cluster Jobs."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from features.auth import require_resource_owner_when_auth_required

from .api import service


router = APIRouter()


def _require_job_access(request: Request, job: dict) -> None:
    require_resource_owner_when_auth_required(
        request,
        job.get("owner_id"),
        not_found_detail="job not found",
    )


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: str, request: Request):
    job = service().repository.get_job(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    _require_job_access(request, job)
    request.state.device_lease_tokens = [
        {
            "lease_id": lease["id"],
            "device_id": lease["device_id"],
            "generation": lease["generation"],
            "attempt_id": lease["attempt_id"],
            "owner_id": job["owner_id"],
        }
        for lease in job.get("leases") or []
        if lease.get("status") in {"active", "orphaned"}
    ]
    if job["status"] in {"completed", "failed", "cancelled"}:
        return {"success": True, "job": job, "already_terminal": True}
    worker_job_id = (
        (job.get("attempt") or {}).get("worker_job_id", "") or f"wj-{job_id}"
    )
    command = service().repository.create_command({
        "worker_id": job["assigned_worker_id"],
        "command_type": "stop_test",
        "job_id": job_id,
        "attempt_id": job["current_attempt_id"],
        "operation_id": f"{job['current_attempt_id']}:stop_test",
        "payload": {
            "worker_job_id": worker_job_id,
            "trace_id": job.get("trace_id", ""),
            "lease_tokens": [
                {
                    "lease_id": lease["id"],
                    "device_id": lease["device_id"],
                    "generation": lease["generation"],
                    "attempt_id": lease["attempt_id"],
                }
                for lease in job.get("leases") or []
                if lease.get("status") in {"active", "orphaned"}
            ],
        },
    })
    service().repository.transition_job(
        job_id,
        "stopping",
        source="api",
        message="Cancellation requested",
        operation_id=command.get("operation_id", command["id"]),
        worker_id=job["assigned_worker_id"],
        payload={"command_id": command["id"]},
    )
    return {
        "success": True,
        "job": service().repository.get_job(job_id),
        "command": command,
    }
