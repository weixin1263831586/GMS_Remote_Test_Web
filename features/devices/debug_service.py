"""Human-approved, app-scoped debug grants; execution stays on audited SSH."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import shutil
import threading
import time
import uuid

import paramiko

from foundation.cluster_port import get_cluster_service
from foundation.error_model import ApiError, record_internal_error

from . import debug_execution, debug_identity, deployment_execution
from . import deployment_service as deployment
from .debug_identity import inspect_apk, require_installed
from .debug_store import DebugStore


logger = logging.getLogger(__name__)
OPERATIONS = frozenset({"install", "launch", "stop", "tap", "swipe", "text", "keyevent", "database_read"})
_store_lock = threading.Lock()
_stores = {}


def store():
    root = deployment.store().root
    with _store_lock:
        if root not in _stores or not _stores[root].db.exists():
            _stores[root] = DebugStore(root)
        return _stores[root]


def check_identity(session):
    record = deployment.auth_service.active_agent_metadata(session["actor_id"])
    if not record or record["owner_user_id"] != session["owner_id"]:
        raise ApiError.forbidden("调试会话的 Agent 身份已失效")
    if "devices.use_leased" not in str(record["scopes"]).split(","):
        raise ApiError.forbidden("Agent 已失去 devices.use_leased 权限")
    for kind, value in (("workers", session["worker_id"]), ("devices", session["serial"])):
        if not deployment.auth_service.agent_acl_allows(record, kind, value):
            raise ApiError.forbidden("Agent 已失去调试目标的访问权限")


def read(request, session_id, permission="devices.read"):
    user = deployment.authorize(
        request, permission, human_permission="devices.use_leased" if permission == "devices.read" else None,
    )
    session = store().get(session_id, user.resource_owner_id)
    if getattr(request.state, "auth_method", "") != "session" and user.actor_id != session["actor_id"]:
        raise ApiError.forbidden("调试会话绑定到另一个 Agent")
    deployment.check_acl(request, session["worker_id"], session["serial"])
    return session


async def prepare(request, worker_id, device, package, minutes, operations, upload):
    user = deployment.authorize(request, "devices.use_leased")
    if getattr(request.state, "auth_method", "") != "agent_token" or not user.actor_id.startswith("agent:"):
        raise ApiError.forbidden("请由需要调试的 Agent 身份申请会话，再由用户批准")
    item = await deployment.prepare(request, worker_id, device, True, upload)
    directory = deployment.store().root / item["id"]
    try:
        identity = await asyncio.to_thread(inspect_apk, directory / "artifact.apk", item["sha256"])
        if identity["package"] != package:
            raise ApiError.invalid_semantics("提交的 APK 包名与申请调试的应用不一致")
        payload = {"id": uuid.uuid4().hex, "owner_id": user.resource_owner_id, "actor_id": user.actor_id,
                   "worker_id": worker_id, "serial": item["serial"], **identity,
                   "duration_minutes": minutes, "operations": sorted(operations),
                   "baseline_deployment_id": item["id"], "baseline_sha256": item["sha256"],
                   "created_at": time.time(), "expires_at": time.time() + 300}
        check_identity(payload)
        return store().create(payload)
    except Exception:
        deployment.store().discard_before_execution(item["id"], user.resource_owner_id)
        shutil.rmtree(directory, ignore_errors=True)
        raise


def approve(request, session_id):
    user = deployment.authorize(request, "devices.use_leased")
    if getattr(request.state, "auth_method", "") != "session":
        raise ApiError.forbidden("调试会话必须由用户登录后批准，Agent 不能自批")
    session = read(request, session_id, "devices.use_leased")
    check_identity(session)
    deployment.target(request, session["worker_id"], session["serial"], "devices.use_leased")
    baseline = deployment.store().get(session["baseline_deployment_id"], session["owner_id"])
    check_apk(session, baseline)
    repository = get_cluster_service().repository
    config = deployment.ssh_config(session["worker_id"])
    grants = store()
    grants.reserve_approval(session_id, session["owner_id"])
    source = ""
    try:
        key = session["worker_id"] + ":" + session["serial"]
        owned = next((c for c in repository.claims.list_active(owner_id=session["owner_id"])
                      if c["device_key"] == key and c.get("source_type") == "cluster-reservation"), None)
        if owned:
            records = [owned]
        else:
            source = "operation:debug-session:" + session_id
            records = repository.acquire_device_operation_claim(
                session["worker_id"], [key], owner_id=session["owner_id"], source_type="debug-session",
                source_id=source, ttl_seconds=session["duration_minutes"] * 60, username=user.username,
            )
        # Sessions without a baseline install must already target the approved
        # signer. Install sessions remain busy until the installed APK is verified.
        if "install" not in session["operations"]:
            with deployment.execution_lease(grants, session_id, approval=True), \
                    deployment.connection(session["worker_id"], config) as ssh:
                require_installed(ssh, session, worker_id=session["worker_id"])
        session = grants.activate(session_id, session["owner_id"], {
            "approved_by": user.actor_id, "approved_at": time.time(),
            "expires_at": time.time() + session["duration_minutes"] * 60,
            "_claim_source": source, "_lease_tokens": deployment.bound_claim_tokens(repository, records, session_id),
        })
    except ValueError as exc:
        if source:
            repository.claims.release(source)
        grants.reset_approval(session_id, session["owner_id"])
        raise ApiError.conflict(str(exc)) from exc
    except Exception:
        if source:
            repository.claims.release(source)
        grants.reset_approval(session_id, session["owner_id"])
        raise
    # Approval grants the finite session, and its first installation consumes
    # an operation slot exactly like every subsequent installation (ADR 0018).
    work = None
    if "install" in session["operations"]:
        try:
            work = start_install(session, baseline, config)
        except ApiError:
            revoke(request, session_id)
            raise
    return session, work


def check_apk(session, payload):
    if (payload["worker_id"], payload["serial"], payload["owner_id"]) != (
        session["worker_id"], session["serial"], session["owner_id"],
    ) or not payload["replace_existing"]:
        raise ApiError.forbidden("APK 部署不属于已授权目标或覆盖安装模式")
    path = deployment.store().root / payload["id"] / "artifact.apk"
    identity = inspect_apk(path, payload["sha256"])
    if any(identity[key] != session[key] for key in ("package", "signers")):
        raise ApiError.forbidden("APK 包名或签名证书超出调试授权范围，请单独提交部署审批")


def check_live(session, *, require_baseline=False):
    current = store().get(session["id"], session["owner_id"])
    if current["status"] != "active":
        debug_identity.invalidate_installed_identity(
            current["worker_id"], current["serial"], current["package"],
        )
        raise ApiError.forbidden("调试会话未批准、已到期或已撤销",
                                 next_actions=({"action": "重新申请并由用户批准调试会话"},))
    if "install" in current["operations"]:
        baseline = deployment.store().get(current["baseline_deployment_id"], current["owner_id"])
        if baseline["status"] in {"failed", "interrupted", "expired"}:
            if (baseline["status"] == "interrupted"
                    and baseline.get("result", {}).get("execution_phase") == "queued"):
                deployment.cleanup_expired_queue(baseline)
            store().revoke(current["id"], current["owner_id"])
            debug_identity.invalidate_installed_identity(
                current["worker_id"], current["serial"], current["package"],
            )
            if current["busy"]:
                finish(current, current["busy"], "interrupted", {"error": "基线安装失败或中断"})
            elif current.get("_claim_source"):
                get_cluster_service().repository.claims.release(current["_claim_source"])
            raise ApiError.forbidden("基线安装失败或中断，调试授权已撤销")
        if require_baseline and baseline["status"] != "completed":
            raise ApiError.conflict("基线 APK 尚未完成安装和身份校验，请先查询部署状态")
    try:
        check_identity(current)
    except ApiError:
        debug_identity.invalidate_installed_identity(
            current["worker_id"], current["serial"], current["package"],
        )
        raise
    repository = get_cluster_service().repository
    worker = repository.get_worker(session["worker_id"])
    if not worker or worker.get("status") not in {"online", "busy", "draining"}:
        raise ApiError.dependency_unavailable("调试 Worker 不在线")
    key = session["worker_id"] + ":" + session["serial"]
    row = next((r for r in repository.list_devices(session["worker_id"]) if r["id"] == key), None)
    if not row or row.get("state") in {"offline", "unknown", "external_busy"}:
        raise ApiError.conflict("调试设备已离线、迁移或正在执行人工测试")
    deployment.check_claim_tokens(current.get("_lease_tokens"), session["owner_id"])


def start_install(session, payload, config):
    check_live(session, require_baseline=payload["id"] != session["baseline_deployment_id"])
    check_apk(session, payload)
    operation = store().begin(session["id"], session["owner_id"], session["actor_id"], "install",
                              {"deployment_id": payload["id"], "sha256": payload["sha256"]})
    try:
        deployment.store().start(payload["id"], session["owner_id"], session["approved_by"])
    except Exception:
        finish(session, operation, "failed", {"error": "部署请求无法启动"})
        raise
    payload.update({"_debug_session": session, "_debug_operation": operation, "_ssh_config": config})
    return payload


async def prepare_install(request, session_id, worker_id, device, replace_existing, upload):
    session = read(request, session_id, "devices.use_leased")
    user, serial, _ = deployment.target(request, worker_id, device, "devices.use_leased")
    if user.actor_id != session["actor_id"]:
        raise ApiError.forbidden("请由申请该会话的 Agent 上传调试 APK")
    if (worker_id, serial) != (session["worker_id"], session["serial"]) or not replace_existing:
        raise ApiError.forbidden("部署目标或安装模式超出调试范围")
    check_live(session, require_baseline=True)
    payload = await deployment.prepare(request, worker_id, device, True, upload)
    try:
        config = deployment.ssh_config(worker_id)
        work = await asyncio.to_thread(start_install, session, payload, config)
        return payload, work
    except Exception:
        deployment.store().discard_before_execution(payload["id"], user.resource_owner_id)
        shutil.rmtree(deployment.store().root / payload["id"], ignore_errors=True)
        raise


def finish(session, operation, status, result):
    store().finish(session["id"], operation, status, result)
    current = store().get(session["id"], session["owner_id"])
    if current["status"] != "active" and current.get("_claim_source") and not current["busy"]:
        get_cluster_service().repository.claims.release(current["_claim_source"])


def execute_install(payload):
    session = payload["_debug_session"]
    repository = deployment.store()
    execution_token = uuid.uuid4().hex
    owns_task = False
    status = "failed"
    result = {"error": "安装中断"}
    try:
        started = repository.begin_execution(payload["id"], execution_token)
        owns_task = True  # False means this invocation atomically expired the queued task.
        if not started:
            status = "interrupted"
            result = repository.get(payload["id"], payload["owner_id"])["result"]
            return

        def check():
            repository.heartbeat(payload["id"], execution_token)
            check_live(session)

        with deployment.execution_lease(repository, payload["id"], execution_token):
            check()
            check_apk(session, payload)
            with deployment.connection(payload["worker_id"], payload["_ssh_config"]) as ssh:
                result = deployment_execution.install(ssh, payload, repository.root / payload["id"] / "artifact.apk", check)
                debug_identity.invalidate_installed_identity(
                    session["worker_id"], session["serial"], session["package"],
                )
                require_installed(ssh, session, check, worker_id=session["worker_id"])
        status = "completed"
    except ApiError as exc:
        result = exc.envelope()
    except (OSError, EOFError, paramiko.SSHException):
        result = ApiError.upstream_failure("调试 APK 传输中断", service="ssh/sftp").envelope()
    except Exception:
        result = ApiError.internal(record_internal_error(logger, "调试安装", "Debug installation failed")).envelope()
    finally:
        if not owns_task:
            owns_task = repository.fail_start(payload["id"], execution_token, result)
        if owns_task:
            current_deployment = repository.get(payload["id"], payload["owner_id"])
            if current_deployment["status"] == "interrupted":
                status = "interrupted"
                result = current_deployment["result"]
            try:
                if status != "interrupted":
                    repository.finish(payload["id"], status, result, execution_token)
            finally:
                try:
                    if status != "completed":
                        store().revoke(session["id"], session["owner_id"])
                finally:
                    debug_identity.invalidate_installed_identity(
                        session["worker_id"], session["serial"], session["package"],
                    )
                    try:
                        finish(session, payload["_debug_operation"], status, result)
                    finally:
                        shutil.rmtree(repository.root / payload["id"], ignore_errors=True)


def action(request, session_id, body):
    session = read(request, session_id, "devices.use_leased")
    user, _, _ = deployment.target(request, session["worker_id"], session["serial"], "devices.use_leased")
    if user.actor_id != session["actor_id"]:
        raise ApiError.forbidden("请由申请该会话的 Agent 执行调试操作")
    debug_execution.validate(body)
    check_live(session, require_baseline=True)
    details = body.model_dump(exclude_none=True)
    if "text" in details:
        details["text_sha256"] = hashlib.sha256(details.pop("text").encode()).hexdigest()
    operation = store().begin(session_id, session["owner_id"], user.actor_id, body.action, details)
    try:
        with deployment.connection(session["worker_id"]) as ssh:
            require_installed(
                ssh, session, lambda: check_live(session), worker_id=session["worker_id"],
            )
            result = debug_execution.perform(ssh, session, body, lambda: check_live(session))
        finish(session, operation, "completed", result)
        return {"operation_id": operation, **result}
    except ApiError as exc:
        finish(session, operation, "failed", exc.envelope())
        raise
    except (OSError, EOFError, paramiko.SSHException):
        error = ApiError.upstream_failure("调试设备 SSH/ADB 连接中断", service="ssh/adb")
        finish(session, operation, "failed", error.envelope())
        raise error from None
    except Exception:
        error = ApiError.internal(record_internal_error(logger, "调试操作", "Debug action failed"))
        finish(session, operation, "failed", error.envelope())
        raise error from None


def revoke(request, session_id):
    session = read(request, session_id, "devices.use_leased")
    if getattr(request.state, "auth_method", "") != "session":
        user = deployment.authorize(request, "devices.use_leased")
        if user.actor_id != session["actor_id"]:
            raise ApiError.forbidden("不能撤销其他 Agent 的调试会话")
    current = store().revoke(session_id, session["owner_id"])
    debug_identity.invalidate_installed_identity(
        session["worker_id"], session["serial"], session["package"],
    )
    if current.get("_claim_source") and not current["busy"]:
        get_cluster_service().repository.claims.release(current["_claim_source"])
    return current


def database_query(request, body):
    deployment.authorize(request, "devices.database.read")
    if not body.debug_session_id:
        raise ApiError.forbidden("数据库读取需要包含 database_read 的人工批准调试会话")
    session = read(request, body.debug_session_id, "devices.use_leased")
    user, serial, _ = deployment.target(request, body.worker_id, body.device, "devices.use_leased")
    if user.actor_id != session["actor_id"] or (body.worker_id, serial, body.package) != (
        session["worker_id"], session["serial"], session["package"],
    ):
        raise ApiError.forbidden("数据库读取超出获批 Agent、设备或应用范围")
    check_live(session, require_baseline=True)
    operation = store().begin(session["id"], session["owner_id"], user.actor_id, "database_read", {
        "actor_id": user.actor_id, "package": body.package, "database": body.database, "table": body.table,
    })
    try:
        with deployment.connection(body.worker_id) as ssh:
            require_installed(
                ssh, session, lambda: check_live(session), worker_id=session["worker_id"],
            )
            check_live(session)
            result = deployment_execution.query_database(ssh, serial, body)
        finish(session, operation, "completed", {"read": True})
        return result
    except Exception:
        # Device output and database rows never enter the access audit.
        finish(session, operation, "failed", {"error": "数据库读取失败"})
        raise
