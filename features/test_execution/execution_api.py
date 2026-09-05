"""Durable test start/stop API.

Every local and remote test is represented by a persistent Cluster Job and is
executed by a Worker Agent. There is intentionally no process-local execution
fallback: if the local Agent is unavailable the request fails before allocating
devices or reporting a running test.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Body, Query, Request

from foundation.responses import error_response

from . import runtime
from .models import TestStartRequest


logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/api/test/start")
async def start_test(
    request: Request,
    help: Annotated[bool, Query()] = False,
    req: TestStartRequest | None = Body(default=None),
):
    response = runtime.generate_help_or_continue(
        help, "POST", "/api/test/start"
    )
    if response:
        return response
    if req is None:
        return error_response("Missing request body", 400)
    if not req.devices:
        return error_response("No devices selected", 400)

    owner_id = runtime.get_client_id_from_request(request)
    try:
        from foundation.cluster_port import get_cluster_service

        cluster = get_cluster_service()
    except (AttributeError, RuntimeError):
        logger.exception("Durable test execution service is unavailable")
        return error_response(
            "The durable test execution service is unavailable; no test was started",
            503,
        )

    local_worker_id = cluster.config.local_worker_id
    requested_worker_id = req.worker_id or local_worker_id
    if requested_worker_id != local_worker_id and not (
        cluster.effective_enabled and cluster.config.remote_dispatch_enabled
    ):
        return error_response(
            "Remote Worker execution is disabled for this deployment", 409
        )

    # 跨 Worker 校验：devices 中的 "worker:serial" 前缀必须与目标 Worker
    # 一致，否则 Worker Agent 会因设备不存在而静默失败，用户难以排查。
    foreign = [
        item
        for item in req.devices
        if ":" in item
        and item.split(":", 1)[0]
        not in {requested_worker_id, local_worker_id}
        and not item.startswith(f"{requested_worker_id}:")
    ]
    if foreign:
        return error_response(
            f"Selected devices do not belong to worker {requested_worker_id}",
            400,
            detail={"devices": foreign},
        )

    req = req.model_copy(update={"worker_id": requested_worker_id})
    if requested_worker_id == local_worker_id:
        try:
            worker = cluster.repository.get_worker(local_worker_id)
            selected_serials = {
                (
                    item[len(local_worker_id) + 1 :]
                    if item.startswith(f"{local_worker_id}:")
                    else item
                )
                for item in req.devices
            }
            device_states = {
                item["serial"]: item["state"]
                for item in cluster.repository.list_devices(local_worker_id)
            }
        except (RuntimeError, AttributeError):
            logger.exception("Failed to evaluate local Worker admission")
            return error_response("Local Worker admission check failed", 503)

        if worker and worker.get("status") == "draining":
            return error_response(
                "A manual Tradefed test is running on this host with an unknown device",
                409,
            )
        if any(
            device_states.get(serial) == "external_busy"
            for serial in selected_serials
        ):
            return error_response(
                "The selected device is already used by a manual Tradefed test",
                409,
            )
        if not cluster.has_command_agent(local_worker_id):
            return error_response(
                "The local Worker Agent is offline; no test was started", 503
            )

    return runtime.start_cluster_test(req, owner_id)


@router.post("/api/test/stop")
async def stop_test(
    request: Request,
    help: Annotated[bool, Query()] = False,
    job_id: str | None = Query(default=None),
):
    response = runtime.generate_help_or_continue(
        help, "POST", "/api/test/stop"
    )
    if response:
        return response

    owner_id = runtime.get_client_id_from_request(request)
    try:
        from foundation.cluster_port import (
            cancel_durable_job,
            get_cluster_service,
        )

        repository = get_cluster_service().repository
    except (AttributeError, RuntimeError):
        return error_response(
            "The durable test execution service is unavailable", 503
        )

    active_statuses = {
        "created",
        "queued",
        "leasing",
        "assigned",
        "dispatching",
        "running",
        "stopping",
        "collecting",
        "worker_lost",
    }
    active_jobs = [
        item
        for item in repository.list_jobs(limit=500, owner_id=owner_id)
        if item.get("status") in active_statuses
    ]
    if job_id:
        if not any(item.get("id") == job_id for item in active_jobs):
            return error_response("Active test job not found", 404)
        return cancel_durable_job(job_id, request)
    if not active_jobs:
        return error_response("No test running", 400)
    if len(active_jobs) > 1:
        return error_response(
            "Multiple tests are running; specify job_id",
            409,
            detail={"job_ids": [item["id"] for item in active_jobs]},
        )
    return cancel_durable_job(active_jobs[0]["id"], request)
