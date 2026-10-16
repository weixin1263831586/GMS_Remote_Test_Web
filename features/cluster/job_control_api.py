"""Cancellation endpoint for durable Cluster Jobs."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from features.auth import (
    require_permission_when_auth_required,
    require_resource_owner_when_auth_required,
)

from .api import service
from .state_machine import InvalidJobTransitionError


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
    require_permission_when_auth_required("tests.cancel")(request)
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
    # 与终态判定（上方 already_terminal 检查）存在竞态：Worker 可能恰在
    # 此间隙把任务转入 completed/failed。transition_job 会按状态机拒绝
    # 该过渡；此时返回刷新后的终态任务（幂等取消语义）而不是 500。
    try:
        service().repository.transition_job(
            job_id,
            "stopping",
            source="api",
            message="Cancellation requested",
            operation_id=command.get("operation_id", command["id"]),
            worker_id=job["assigned_worker_id"],
            payload={"command_id": command["id"]},
        )
    except InvalidJobTransitionError:
        refreshed = service().repository.get_job(job_id)
        terminal = {"completed", "failed", "cancelled"}
        if refreshed is None or refreshed["status"] not in terminal:
            raise
        return {"success": True, "job": refreshed, "already_terminal": True}
    return {
        "success": True,
        "job": service().repository.get_job(job_id),
        "command": command,
    }
