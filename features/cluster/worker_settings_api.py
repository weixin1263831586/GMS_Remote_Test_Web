from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from features.auth import CurrentUser, require_role_when_auth_required

from .api import _run_worker_command, service


router = APIRouter()


@router.get("/workers/{worker_id}/config")
async def get_worker_config(
    worker_id: str,
    _admin: CurrentUser | None = Depends(require_role_when_auth_required("admin")),
):
    """Return the configurable parameters of a Worker."""
    worker = service().repository.get_worker(worker_id)
    if worker is None:
        raise HTTPException(404, "worker not found")
    if worker_id == service().config.local_worker_id and not service().has_command_agent(worker_id):
        raise HTTPException(503, "local Worker Agent is offline")
    result = await _run_worker_command(worker_id, "get_config", {}, timeout=15)
    return {"success": True, "config": result}


@router.post("/workers/{worker_id}/config")
async def update_worker_config(
    worker_id: str,
    body: dict,
    _admin: CurrentUser | None = Depends(require_role_when_auth_required("admin")),
):
    """Update configurable Worker parameters and restart the agent to apply."""
    worker = service().repository.get_worker(worker_id)
    if worker is None:
        raise HTTPException(404, "worker not found")
    if worker.get("status") not in {"online", "busy"}:
        raise HTTPException(409, "worker is not online")
    if int(worker.get("running_jobs") or 0) > 0:
        raise HTTPException(409, "Worker configuration cannot change while tests are running")
    max_jobs = body.get("max_jobs")
    if max_jobs is not None:
        try:
            max_jobs = int(max_jobs)
        except (TypeError, ValueError):
            raise HTTPException(400, "max_jobs must be an integer") from None
        if not 1 <= max_jobs <= 32:
            raise HTTPException(400, "max_jobs must be between 1 and 32")
        body["max_jobs"] = max_jobs
    if worker_id == service().config.local_worker_id and not service().has_command_agent(worker_id):
        raise HTTPException(503, "local Worker Agent is offline")
    result = await _run_worker_command(worker_id, "update_config", body, timeout=15)
    return {"success": True, **result}


@router.post("/workers/{worker_id}/restart-vnc")
async def restart_worker_vnc(
    worker_id: str,
    _admin: CurrentUser | None = Depends(require_role_when_auth_required("admin")),
):
    """Restart x11vnc/websockify on a worker to recover from zombie VNC processes."""
    worker = service().repository.get_worker(worker_id)
    if worker is None:
        raise HTTPException(404, "worker not found")
    if worker.get("status") not in {"online", "busy"}:
        raise HTTPException(409, "worker is not online")
    if worker_id == service().config.local_worker_id and not service().has_command_agent(worker_id):
        raise HTTPException(503, "local Worker Agent is offline")
    try:
        result = await _run_worker_command(worker_id, "restart_vnc", {}, timeout=20)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502, f"restart_vnc failed: {exc}") from exc
    return {"success": result.get("rfb_ok", False), "result": result}
