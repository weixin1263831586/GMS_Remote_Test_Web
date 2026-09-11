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

    # Agent Service Token enforcement (2026-09-08 audit §四/§六): agents must
    # hold tests.execute and may only target workers/devices in their ACL.
    from features.auth import (
        ensure_agent_device_allowed,
        ensure_agent_worker_allowed,
        get_authenticated_user,
    )

    principal = None
    try:
        principal = get_authenticated_user(request)
    except AttributeError:
        principal = None  # stub request without state (unit tests)
    if principal is not None:
        if not principal.has_permission("tests.execute"):
            return error_response("缺少 tests.execute 权限", status_code=403)
        for device in req.devices:
            ensure_agent_device_allowed(request, device)
        if req.worker_id:
            ensure_agent_worker_allowed(request, req.worker_id)

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
    # 默认 Worker（请求未显式给出 worker_id）同样必须通过 Agent ACL；
    # 只在显式 worker_id 上检查会让 Agent 以空 worker_id 越权命中默认 Worker。
    if principal is not None and requested_worker_id != req.worker_id:
        ensure_agent_worker_allowed(request, requested_worker_id)
    if requested_worker_id != local_worker_id and not (
        cluster.effective_enabled and cluster.config.remote_dispatch_enabled
    ):
        return error_response(
            "Remote Worker execution is disabled for this deployment", 409
        )

    # 跨 Worker 校验：通过 inventory 解析设备归属，绝不按 ":" 猜 Worker。
    # Android serial 可能含 ":"（ADB TCP "ip:5555"、ADB Proxy
    # "localhost:port"），字符串前缀切分会把这类设备误判为外 Worker。
    # 解析不到且不以其它已知 Worker ID 开头的值（如裸 serial 或清单
    # 未加载）保持旧行为：交给目标 Worker 的执行层按自身命名空间处理。
    try:
        known_worker_ids = {
            str(worker.get("id") or "")
            for worker in cluster.list_workers()
        }
        foreign = []
        for item in req.devices:
            if cluster.repository.resolve_worker_device(
                requested_worker_id, item
            ) is not None or cluster.repository.resolve_worker_device(
                local_worker_id, item
            ) is not None:
                continue
            separator = item.find(":")
            if separator <= 0:
                continue
            prefix = item[:separator]
            if prefix != requested_worker_id and prefix != local_worker_id \
                    and prefix in known_worker_ids:
                foreign.append(item)
        if foreign:
            return error_response(
                f"Selected devices do not belong to worker {requested_worker_id}",
                400,
                detail={"devices": foreign},
            )
    except (RuntimeError, AttributeError):
        logger.exception("Failed to resolve device ownership")
        return error_response("Device ownership check failed", 503)

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

    # tests.cancel was defined but never enforced server-side.
    # Mirror the start_test pattern: agent principals must hold the scope;
    # human roles carry tests.cancel via ROLE_PERMISSIONS (dev-mode anonymous
    # callers keep working because the principal is None there).
    from features.auth import get_authenticated_user

    principal = None
    try:
        principal = get_authenticated_user(request)
    except AttributeError:
        principal = None  # stub request without state (unit tests)
    if principal is not None and not principal.has_permission("tests.cancel"):
        return error_response("缺少 tests.cancel 权限", status_code=403)

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
