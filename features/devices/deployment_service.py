"""Owner, Worker and device fencing for a single reviewed APK deployment."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import shutil
import threading
import time
import uuid
import zipfile
from pathlib import Path

import paramiko
from fastapi import HTTPException

from features.auth import (
    auth_service,
    ensure_agent_device_allowed,
    ensure_agent_worker_allowed,
    get_authenticated_user,
)
from foundation.cluster_port import get_cluster_service
from foundation.error_model import ApiError, record_internal_error
from foundation.security import is_safe_device_id

from . import deployment_execution as execution
from . import runtime
from .debug_identity import inspect_apk, installed_identity, invalidate_installed_identity
from .deployment_store import DeploymentStore


logger = logging.getLogger(__name__)
_store_lock = threading.Lock()
_stores: dict[Path, DeploymentStore] = {}


def store():
    root = (get_cluster_service().repository.db_path.parent / "apk-deployments").resolve()
    with _store_lock:
        if root not in _stores or not _stores[root].db.exists():
            _stores[root] = DeploymentStore(root)
        return _stores[root]


def authorize(request, permission: str, *, human_permission: str | None = None):
    user = get_authenticated_user(request)
    if user is None:
        raise ApiError.unauthenticated()
    # Human operators review their own requests using their existing device
    # operation permission. Agent readers must retain the explicit read scope.
    if human_permission and getattr(request.state, "auth_method", "") == "session":
        permission = human_permission
    if not user.has_permission(permission):
        raise ApiError.forbidden("缺少权限：" + permission)
    if getattr(request.state, "auth_method", "") == "machine_authority":
        raise ApiError.forbidden("自动化机器凭据不能创建或批准 APK 部署")
    return user


def check_acl(request, worker_id, serial):
    try:
        ensure_agent_worker_allowed(request, worker_id)
        ensure_agent_device_allowed(request, serial)
    except HTTPException as exc:
        raise ApiError.forbidden(str(exc.detail)) from exc


def target(request, worker_id: str, device: str, permission: str):
    user = authorize(request, permission)
    prefix = worker_id + ":"
    serial = device[len(prefix):] if device.startswith(prefix) else device
    if not is_safe_device_id(serial):
        raise ApiError.malformed_request("设备序列号无效")
    check_acl(request, worker_id, serial)
    cluster = get_cluster_service()
    worker = cluster.repository.get_worker(worker_id)
    if not worker or worker.get("status") not in {"online", "busy", "draining"}:
        raise ApiError.dependency_unavailable("指定 Worker 不在线")
    key = prefix + serial
    device_row = next((d for d in cluster.repository.list_devices(worker_id) if d["id"] == key), None)
    if not device_row:
        raise ApiError.conflict("设备不属于指定 Worker")
    if device_row.get("state") in {"offline", "unknown"}:
        raise ApiError.conflict("设备离线")
    if permission == "devices.use_leased" and device_row.get("state") == "external_busy":
        raise ApiError.conflict("设备正在执行人工测试")
    return user, serial, key


def ssh_config(worker_id: str):
    cluster = get_cluster_service()
    config = runtime.config_manager.load_config()
    if worker_id == cluster.config.local_worker_id:
        return config
    worker = cluster.repository.get_worker(worker_id) or {}
    host = str(worker.get("address") or "")
    configured = runtime.config_manager.get_device_host_config(host, config)
    if not configured:
        raise ApiError.dependency_unavailable("该 Worker 未配置 SSH 主机凭据")
    return {**configured, "host": host,
            "password": runtime.config_manager.find_device_host_password(host, config) or ""}


@contextlib.contextmanager
def connection(worker_id: str, approved_config=None):
    config = ssh_config(worker_id) if approved_config is None else approved_config
    try:
        ssh = runtime.ssh_manager.get_connection(config)
    except (OSError, EOFError, paramiko.SSHException) as exc:
        raise ApiError.upstream_failure("Worker SSH 连接失败", service="ssh") from exc
    if ssh is None:
        raise ApiError.upstream_failure("Worker SSH 连接失败", service="ssh")
    try:
        yield ssh
    except (OSError, EOFError, paramiko.SSHException) as exc:
        raise ApiError.upstream_failure("Worker SSH/ADB 连接中断", service="ssh/adb") from exc
    finally:
        runtime.ssh_manager.return_connection(ssh)


async def prepare(request, worker_id: str, device: str, replace_existing: bool, upload):
    user, serial, _ = target(request, worker_id, device, "devices.use_leased")
    ssh_config(worker_id)
    repository = store()
    for expired in repository.expire_pending():
        shutil.rmtree(repository.root / expired, ignore_errors=True)
    request_id = uuid.uuid4().hex
    directory = repository.root / request_id
    directory.mkdir(mode=0o700)
    path = directory / "artifact.apk"
    digest = hashlib.sha256()
    size = 0
    try:
        with path.open("xb") as output:
            while chunk := await upload.read(1024 * 1024):
                size += len(chunk)
                if size > 256 * 1024 * 1024:
                    raise ApiError.invalid_semantics("APK 超过 256 MiB 上限")
                digest.update(chunk)
                output.write(chunk)
        try:
            with zipfile.ZipFile(path) as archive:
                if "AndroidManifest.xml" not in archive.namelist():
                    raise zipfile.BadZipFile("manifest missing")
        except zipfile.BadZipFile as exc:
            raise ApiError.invalid_semantics("文件不是有效的 APK 制品") from exc
        path.chmod(0o400)
        identity = await asyncio.to_thread(inspect_apk, path, digest.hexdigest())

        return repository.create({
            "id": request_id, "owner_id": user.resource_owner_id,
            "actor_id": user.actor_id, "worker_id": worker_id, "serial": serial,
            "filename": Path(upload.filename or "artifact.apk").name[:200],
            "sha256": digest.hexdigest(), "size_bytes": size,
            **identity, "installed_identity": None, "installed_signers_match": None,
            "installed_identity_precheck": "deferred_until_approval",
            "replace_existing": replace_existing, "expires_at": time.time() + 300,
        })
    except Exception:
        shutil.rmtree(directory, ignore_errors=True)
        raise


def bound_claim_tokens(repository, records, attempt_id):
    """Bind execution to a claim's purpose as well as its lease (ADR 0017/0018)."""
    by_id = {record["id"]: record for record in records}
    return [
        {**token, "source_id": by_id[token["lease_id"]]["source_id"],
         "source_type": by_id[token["lease_id"]]["source_type"]}
        for token in repository.claim_fencing_tokens(records, attempt_id)
    ]


def check_claim_tokens(tokens, owner_id):
    if not tokens:
        raise ApiError.conflict("设备操作缺少租约凭证，请重新申请授权")
    claims = get_cluster_service().repository.claims
    for token in tokens:
        current = claims.active_claim(token["device_id"])
        # Reservation -> job transfer preserves lease id/generation. Missing
        # source bindings in older grants fail closed instead of trusting them.
        if not current or current["owner_id"] != owner_id or any(
            current[field] != token.get(key) for field, key in (
                ("id", "lease_id"), ("generation", "generation"),
                ("source_id", "source_id"), ("source_type", "source_type"),
            )
        ):
            raise ApiError.conflict("设备租约或用途已变化，请重新申请授权")


def claim(request, payload: dict):
    user, _serial, key = target(request, payload["worker_id"], payload["serial"], "devices.use_leased")
    repository = get_cluster_service().repository
    owned = next((c for c in repository.claims.list_active(owner_id=user.resource_owner_id)
                  if c["device_key"] == key and c.get("source_type") == "cluster-reservation"), None)
    source = "operation:apk-deploy:" + payload["id"]
    if owned:
        records = [owned]
        source = ""
    else:
        try:
            records = repository.acquire_device_operation_claim(
                payload["worker_id"], [key], owner_id=user.resource_owner_id,
                source_type="apk-deploy", source_id=source, ttl_seconds=1200,
                username=user.username,
            )
        except ValueError as exc:
            raise ApiError.conflict(str(exc)) from exc
    request.state.device_lease_tokens = [
        {**token, "owner_id": user.resource_owner_id}
        for token in bound_claim_tokens(repository, records, payload["id"])
    ]
    payload["_lease_tokens"] = request.state.device_lease_tokens
    return source


def approve(request, request_id: str):
    user = authorize(request, "devices.use_leased")
    if getattr(request.state, "auth_method", "") != "session":
        raise ApiError.forbidden("部署审批必须由用户本人登录会话操作，Agent 不能自批")
    repository = store()
    payload = repository.get(request_id, user.resource_owner_id)
    if payload["status"] != "pending":
        raise ApiError.conflict("部署请求已审批或已过期")
    execution.verify_artifact(repository.root / request_id / "artifact.apk", payload["sha256"])
    approved_config = ssh_config(payload["worker_id"])
    # Reserve the request BEFORE acquiring a claim: a losing concurrent
    # approval must never release the winning execution's shared source id.
    repository.start(request_id, user.resource_owner_id, user.actor_id)
    source = ""
    try:
        source = claim(request, payload)
        def observe_installed():
            check_claim_tokens(payload.get("_lease_tokens"), payload["owner_id"])
            with connection(payload["worker_id"], approved_config) as ssh:
                result = installed_identity(
                    ssh, payload["serial"], payload["package"],
                    lambda: check_claim_tokens(payload.get("_lease_tokens"), payload["owner_id"]),
                )
            check_claim_tokens(payload.get("_lease_tokens"), payload["owner_id"])
            return result

        with execution_lease(repository, request_id, approval=True):
            installed = observe_installed()
        repository.record_precheck(request_id, {
            "status": "verified", "checked_at": time.time(),
            "installed_identity": installed,
            "signers_match": installed["signers"] == payload["signers"] if installed else None,
        })
        # ADR 0006: server-issued single-use approval, bound to the complete
        # immutable operation. The secret stays in this human request's memory.
        command = json.dumps({key: payload[key] for key in (
            "id", "worker_id", "serial", "sha256", "replace_existing",
        )}, sort_keys=True, separators=(",", ":"))
        approval = auth_service.create_approval_token(
            user=user, tool="gms_rt_apk_deploy", device=payload["serial"], command=command,
        )
        if not auth_service.consume_approval_token(
            approval["token"], tool="gms_rt_apk_deploy", device=payload["serial"], command=command,
        ):
            raise ApiError.forbidden("部署审批无效或已过期")
    except Exception:
        if source:
            get_cluster_service().repository.claims.release(source)
        repository.reset_before_execution(request_id, user.resource_owner_id)
        raise
    # Keep the human request's owner-scoped SSH configuration in memory for
    # the background task. It is never added to the durable/public payload.
    payload["_ssh_config"] = approved_config
    return payload, source


def execute(payload: dict, source: str):
    repository = store()
    execution_token = uuid.uuid4().hex
    owns_task = False
    status = "failed"
    result = {"error": "部署任务未能启动"}

    def check_claim():
        repository.heartbeat(payload["id"], execution_token)
        claims = get_cluster_service().repository.claims
        check_claim_tokens(payload.get("_lease_tokens"), payload["owner_id"])
        if source:
            claims.renew(source, 1200)

    try:
        started = repository.begin_execution(payload["id"], execution_token)
        owns_task = True  # False means this invocation atomically expired the queued task.
        if not started:
            status = "interrupted"
            result = repository.get(payload["id"], payload["owner_id"])["result"]
        else:
            with execution_lease(repository, payload["id"], execution_token):
                check_claim()
                with connection(payload["worker_id"], payload["_ssh_config"]) as ssh:
                    result = execution.install(
                        ssh, payload, repository.root / payload["id"] / "artifact.apk", check_claim,
                    )
            status = "completed"
    except ApiError as exc:
        result = exc.envelope()
    except (OSError, EOFError, paramiko.SSHException):
        result = ApiError.upstream_failure(
            "SSH/制品传输中断", service="sftp",
        ).envelope()
    except Exception:
        message = record_internal_error(logger, "APK 部署", "APK deployment failed")
        result = ApiError.internal(message).envelope()
    finally:
        if not owns_task:
            owns_task = repository.fail_start(payload["id"], execution_token, result)
        if owns_task:
            current = repository.get(payload["id"], payload["owner_id"])
            if current["status"] == "interrupted":
                status = "interrupted"
                result = current["result"]
            try:
                if status != "interrupted":
                    repository.finish(payload["id"], status, result, execution_token)
            finally:
                invalidate_installed_identity(payload["worker_id"], payload["serial"], payload["package"])
                try:
                    if source:
                        get_cluster_service().repository.claims.release(source)
                finally:
                    shutil.rmtree(repository.root / payload["id"], ignore_errors=True)


@contextlib.contextmanager
def execution_lease(repository, request_id, execution_token=None, *, approval=False):
    """One durable queued task wins execution; live work renews its own lease."""
    stop = threading.Event()
    renew = (lambda: repository.approval_heartbeat(request_id)) if approval else (
        lambda: repository.heartbeat(request_id, execution_token)
    )
    renew()

    def heartbeat():
        while not stop.wait(20):
            try:
                renew()
            except Exception:
                return  # The next fenced step must fail closed.

    thread = threading.Thread(target=heartbeat, daemon=True)
    thread.start()
    try:
        yield
        renew()
    finally:
        stop.set()
        thread.join(timeout=2)


def cleanup_expired_queue(payload):
    """Reap an expired approval only if no executor has claimed its queue row."""
    repository = store()
    token = uuid.uuid4().hex
    if not repository.claim_expired_queue_cleanup(payload["id"], payload["owner_id"], token):
        return False
    source = "operation:apk-deploy:" + payload["id"]
    claims = get_cluster_service().repository.claims
    active = claims.active_claim(payload["worker_id"] + ":" + payload["serial"])
    if (active and active.get("owner_id") == payload["owner_id"]
            and active.get("source_id") == source and active.get("source_type") == "apk-deploy"):
        claims.release(source)
    shutil.rmtree(repository.root / payload["id"], ignore_errors=True)
    return True


def reconcile_interrupted(request, payload):
    """Status reads reconcile observed installation; interrupted work is never replayed."""
    target(request, payload["worker_id"], payload["serial"], "devices.use_leased")
    if not payload.get("package") or not payload.get("signers"):
        return payload  # Older approvals cannot establish an installed identity.
    claim = get_cluster_service().repository.claims.active_claim(payload["worker_id"] + ":" + payload["serial"])
    if claim and claim["owner_id"] != payload["owner_id"]:
        raise ApiError.conflict("设备由其他用户占用，无法核实中断安装")
    with connection(payload["worker_id"]) as ssh:
        identity = installed_identity(ssh, payload["serial"], payload["package"])
    matches = bool(identity and identity["sha256"] == payload["sha256"]
                   and identity["signers"] == payload["signers"])
    store().reconcile(payload["id"], payload["owner_id"], {
        "reconciled_at": time.time(), "installed_identity": identity, "artifact_matches": matches,
    }, completed=matches)
    return store().get(payload["id"], payload["owner_id"])
