from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request

from features.auth import (
    CurrentUser,
    authentication_required,
    get_authenticated_user,
    require_authenticated_user,
    require_permission_when_auth_required,
)
from features.users import owner_id_from_request
from foundation.job_env import filter_job_env

from .api import _authenticate, _require_cluster_enabled, service
from .execution_spec import (
    build_argv_from_spec,
    build_default_argv,
    canonicalize_execution_spec,
)
from .models import ClusterJobCreate, JobEventBatch


router = APIRouter()


def _request_owner_id(request: Request) -> str:
    user = get_authenticated_user(request)
    if user:
        return user.id
    if authentication_required():
        return require_authenticated_user(request).id
    return owner_id_from_request(request)


def _request_owner_username(request: Request) -> str:
    user = get_authenticated_user(request)
    if user:
        return user.username
    if authentication_required():
        return require_authenticated_user(request).username
    return ""


def _require_job_access(request: Request, job: dict) -> None:
    # Agent principals must hold jobs.read even for single-job
    # reads; ownership filtering below stays unchanged.
    from features.auth import require_agent_scope

    user = get_authenticated_user(request)
    if user is not None:
        require_agent_scope("jobs.read")(request)
    if user is None:
        if authentication_required():
            require_authenticated_user(request)
        return
    if user.role != "admin" and str(job.get("owner_id") or "") != user.id:
        raise HTTPException(404, "job not found")


def _job_response(job: dict) -> dict:
    """Add a user-facing client identity while preserving access ownership."""
    from features.users import resolve_client_display_id

    item = dict(job or {})
    item["client_display_id"] = resolve_client_display_id(
        str(item.get("owner_id") or "")
    )
    return item


def _mask_serial(serial: str) -> str:
    """Mask a device serial for cross-owner monitoring.

    跨用户监控只需回答"谁在占哪个设备"，完整序列号属于设备敏感标识；
    保留前后缀便于口头比对（如 ``RK3576GMS****23``），中间折叠。
    """
    value = str(serial or "").strip()
    if not value:
        return ""
    if len(value) <= 6:
        return f"{value[:2]}****"
    return f"{value[:9]}****{value[-2:]}"


def _job_monitor_response(job: dict) -> dict:
    """Return the non-sensitive fields needed for cross-owner active monitoring."""
    item = _job_response(job)
    return {
        "id": item.get("id", ""),
        "client_display_id": item.get("client_display_id", ""),
        "source_type": item.get("source_type", ""),
        "assigned_worker_id": item.get("assigned_worker_id", ""),
        "suite_key": item.get("suite_key", ""),
        "status": item.get("status", ""),
        "created_at": item.get("created_at", ""),
        "started_at": item.get("started_at", ""),
        "updated_at": item.get("updated_at", ""),
        "leases": [
            {
                "device_id": lease.get("device_id", ""),
                "worker_id": lease.get("worker_id", ""),
                "serial": _mask_serial(str(lease.get("serial") or "")),
                "status": lease.get("status", ""),
            }
            for lease in item.get("leases") or []
        ],
        "monitor_only": True,
    }


@router.post("/jobs")
def create_job(
    body: ClusterJobCreate,
    request: Request,
    _actor: CurrentUser | None = Depends(
        require_permission_when_auth_required("tests.execute")
    ),
):
    # argv 是服务端从 execution_spec 派生的数据，不是浏览器可提交的输入；
    # 接受 raw argv 会让 ExecutionSpec 校验（test_type/suite/设备绑定）被绕过。
    if body.argv:
        raise HTTPException(
            400,
            "raw argv is not accepted from browsers; supply execution_spec",
        )
    local_worker_id = service().config.local_worker_id
    _require_cluster_enabled(
        remote=body.worker_id not in {"auto", local_worker_id}
    )
    if (
        body.worker_id == local_worker_id
        and not service().has_command_agent(local_worker_id)
    ):
        raise HTTPException(503, "local Worker Agent is offline")
    data = body.model_dump()
    data["trace_id"] = str(getattr(request.state, "trace_id", "") or "")
    # job env 到达 Worker 后会进入 Bash 启动环境，未列入白名单的键
    # （BASH_ENV/ENV/SHELLOPTS/解释器搜索路径等）可扩大 Worker OS 执行能力，
    # 必须在入队前拒绝，而不是等到 Worker 端静默丢弃。
    # 参见 docs/architecture/adr/0001-controller-worker-boundary.md。
    allowed_env, rejected_env_keys = filter_job_env(data.get("env"))
    if rejected_env_keys:
        raise HTTPException(
            400,
            "env contains keys outside the job allowlist: "
            + ", ".join(sorted(rejected_env_keys)),
        )
    data["env"] = allowed_env
    # 所有者必须取自认证账户，不接受浏览器传入值。
    data["owner_id"] = _request_owner_id(request)
    data["owner_username"] = _request_owner_username(request)
    if data["worker_id"] == "auto":
        try:
            data["worker_id"], selected_devices = service().select_worker(
                data["suite_key"],
                data["device_count"],
                require_agent=True,
            )
            if not data["devices"]:
                data["devices"] = selected_devices
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
    # Agent Token 的 Worker/设备 ACL 在最终身份解析完成后统一授权，
    # 覆盖显式 worker、auto 调度与默认 Worker 三条路径。
    from features.auth import ensure_agent_device_allowed, ensure_agent_worker_allowed

    ensure_agent_worker_allowed(request, data["worker_id"])
    worker_prefix = f"{data['worker_id']}:"
    for device_id in data["devices"]:
        ensure_agent_device_allowed(request, device_id)
        serial = str(device_id or "")
        # 设备既可能按裸序列号、也可能按 "worker:serial" 提交；两种形态
        # 都要落在 ACL 内，避免换一种写法绕过设备白名单。
        if serial.startswith(worker_prefix):
            ensure_agent_device_allowed(request, serial[len(worker_prefix):])
    spec = data.get("execution_spec")
    if spec and spec.get("test_type"):
        requested_path = str(spec.get("suite_path") or "")
        if data.get("suite_path") and data["suite_path"] != requested_path:
            raise HTTPException(
                409,
                "execution_spec suite_path does not match the job suite_path",
            )
        suites = [
            item
            for item in service().repository.list_suites(data["worker_id"])
            if item["tools_path"] == requested_path and item["available"]
        ]
        if data.get("suite_key"):
            suites = [
                item for item in suites if item["suite_key"] == data["suite_key"]
            ]
        if not suites:
            raise HTTPException(409, "suite is not available on worker")
        selected_suite = suites[0]
        data["suite_path"] = selected_suite["tools_path"]
        data["suite_key"] = selected_suite["suite_key"]
        data["execution_spec"] = canonicalize_execution_spec(
            spec,
            suite_path=data["suite_path"],
            suite_type=selected_suite["suite_type"],
            devices=data["devices"],
            worker_id=data["worker_id"],
        )
        data["argv"] = build_argv_from_spec(data["execution_spec"])
    else:
        (
            data["argv"],
            data["suite_path"],
            data["suite_key"],
        ) = build_default_argv(
            suite_path=data["suite_path"],
            suite_key=data["suite_key"],
            worker_id=data["worker_id"],
            local_worker_id=local_worker_id,
            available_suites=service().repository.list_suites(data["worker_id"]),
        )
    from features.devices import incompatible_test_devices

    inventory = {
        str(item.get("serial") or ""): item
        for item in service().repository.list_devices(data["worker_id"])
    }
    selected_inventory = []
    for device_id in data["devices"]:
        serial = str(device_id or "")
        prefix = f"{data['worker_id']}:"
        if serial.startswith(prefix):
            serial = serial[len(prefix):]
        if serial in inventory:
            selected_inventory.append(inventory[serial])
    incompatible, policy = incompatible_test_devices(
        selected_inventory,
        data["argv"],
        data.get("env") or {},
    )
    if incompatible:
        raise HTTPException(
            409,
            f"所选测试需要真实USB/Fastboot通道，不能使用ADB Proxy设备: "
            f"{', '.join(incompatible)}；请改用USB/IP或在设备来源Worker本地执行。"
            f"原因：{policy['reason']}",
        )
    try:
        job = service().repository.create_job_with_leases(data)
        request.state.device_lease_tokens = [
            {
                "lease_id": lease["id"],
                "device_id": lease["device_id"],
                "generation": lease["generation"],
                "attempt_id": lease["attempt_id"],
                "owner_id": data["owner_id"],
            }
            for lease in job.get("leases") or []
            if lease.get("status") == "active"
        ]
        # Job creation and command queuing are two separate commits;
        # without compensation a command-write failure used to leave an
        # `assigned` job with zero dispatchable commands and an active
        # claim. Fail the job and release its claims so it can be retried.
        try:
            command = service().repository.dispatch_job_start_command(
                job,
                argv=data["argv"],
                execution_spec=data.get("execution_spec"),
                env=data["env"],
                devices=data["devices"],
            )
        except Exception as dispatch_exc:
            service().repository.compensate_failed_dispatch(
                job["id"], dispatch_exc
            )
            raise HTTPException(
                503,
                "任务已创建但派发命令写入失败，请稍后重试（任务已回滚为失败状态）",
            ) from dispatch_exc
        return {
            "success": True,
            "job": _job_response(service().repository.get_job(job["id"]) or {}),
            "command": command,
        }
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.get("/jobs")
def list_jobs(
    request: Request,
    limit: int = Query(default=100, ge=1, le=500),
    include_active: bool = Query(default=False),
):
    # Enforce the jobs.read scope for agent principals (human
    # roles carry jobs.read via ROLE_PERMISSIONS; dev-mode anonymous passes).
    from features.auth import require_agent_scope

    user = get_authenticated_user(request)
    if user is not None:
        require_agent_scope("jobs.read")(request)
    elif authentication_required():
        user = require_authenticated_user(request)
    owner_id = user.id if user and user.role != "admin" else ""
    can_monitor_cross_owner = bool(
        user and user.role in {"device_operator", "admin"}
    )
    jobs = service().repository.list_jobs(
        limit,
        owner_id=owner_id,
        include_active=include_active and can_monitor_cross_owner,
    )
    return {
        "success": True,
        "jobs": [
            (
                _job_monitor_response(item)
                if owner_id and str(item.get("owner_id") or "") != owner_id
                else _job_response(item)
            )
            for item in jobs
        ],
    }


@router.get("/jobs/{job_id}")
def get_job(job_id: str, request: Request):
    job = service().repository.get_job(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    _require_job_access(request, job)
    return {"success": True, "job": _job_response(job)}


@router.delete("/jobs/{job_id}")
def delete_job(job_id: str, request: Request):
    job = service().repository.get_job(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    _require_job_access(request, job)
    if job["status"] not in {"completed", "failed", "cancelled"}:
        raise HTTPException(409, "only completed history can be deleted")
    if not service().repository.delete_job(job_id):
        raise HTTPException(409, "job could not be deleted")
    return {"success": True, "deleted": job_id}


@router.post("/jobs/{job_id}/events")
def add_job_events(
    job_id: str,
    body: JobEventBatch,
    worker_id: str = Header(alias="X-GMS-Worker-ID"),
    authorization: str | None = Header(default=None),
):
    _authenticate(worker_id, authorization)
    job = service().repository.get_job(job_id)
    if (
        not job
        or job["assigned_worker_id"] != worker_id
        or job["current_attempt_id"] != body.attempt_id
    ):
        raise HTTPException(404, "job attempt not found for worker")
    inserted = service().repository.add_events(
        job_id,
        body.attempt_id,
        [item.model_dump() for item in body.events],
    )
    return {"success": True, "inserted": inserted}


@router.get("/jobs/{job_id}/events")
def list_job_events(
    job_id: str,
    request: Request,
    after: int = Query(default=-1),
    limit: int = Query(default=500, le=2000),
):
    job = service().repository.get_job(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    _require_job_access(request, job)
    return {
        "success": True,
        "events": service().repository.list_events(job_id, after, limit),
    }
