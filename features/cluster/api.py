from __future__ import annotations

import asyncio
import base64
import ipaddress
import logging
import re
from pathlib import Path

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, Response

from features.auth import (
    CurrentUser,
    authentication_required,
    get_authenticated_user,
    require_authenticated_user,
    require_authenticated_user_when_auth_required,
    require_elevated_admin_when_auth_required,
    require_role,
)

from .config import ClusterConfig
from .models import (
    ClusterSuiteDownload,
    ClusterSuiteExtract,
    CommandCreate,
    WorkerHeartbeat,
    WorkerRegistration,
)
from .repository import ClusterRepository
from .service import ClusterService
from .worker_auth import (
    authenticate_worker as _authenticate,
)
from .worker_auth import (
    worker_tokens as _worker_tokens,
)
from .worker_auth import (
    write_worker_tokens as _write_worker_tokens,
)


router = APIRouter(prefix="/api/cluster", tags=["cluster"])
page_router = APIRouter()
cluster_service: ClusterService | None = None
logger = logging.getLogger(__name__)


def configure_cluster(data_root: str | Path) -> ClusterService:
    global cluster_service
    if cluster_service is not None:
        cluster_service.stop_watchdog()
    config = ClusterConfig.load()
    repository = ClusterRepository(
        Path(data_root) / "cluster/cluster.sqlite3",
        claim_lease_ttl_seconds=config.lease_ttl_seconds,
    )
    from features.devices import device_lock_manager

    device_lock_manager.configure_local_worker(config.local_worker_id)
    cluster_service = ClusterService(repository, config=config)
    cluster_service.start_watchdog()
    from .local_bridge import start_local_bridge
    start_local_bridge(repository, config)
    return cluster_service


def service() -> ClusterService:
    if cluster_service is None:
        raise RuntimeError("cluster service is not configured")
    return cluster_service


@page_router.get("/cluster", response_class=HTMLResponse)
def cluster_page():
    ui_dir = Path(__file__).with_name("ui")
    html = (ui_dir / "page.html").read_text(encoding="utf-8")
    html = html.replace("{{CLUSTER_CSS}}", (ui_dir / "page.css").read_text(encoding="utf-8"))
    html = html.replace("{{CLUSTER_JS}}", (ui_dir / "page.js").read_text(encoding="utf-8"))
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


@router.post("/workers/register")
def register_worker(body: WorkerRegistration, request: Request,
                    authorization: str | None = Header(default=None)):
    _authenticate(body.worker_id, authorization)
    # Hostnames reported by Workers are often only resolvable on their own
    # LAN. The connection address must be reachable from this Controller.
    try:
        ipaddress.ip_address(body.address)
    except ValueError:
        source = request.client.host if request.client else ""
        try:
            ipaddress.ip_address(source)
        except ValueError:
            pass
        else:
            body = body.model_copy(update={"address": source})
    worker = service().repository.register_worker(body.model_dump())
    if not service().repository.list_suites(body.worker_id):
        service().repository.create_command({
            "worker_id": body.worker_id,
            "command_type": "refresh_suites",
            "operation_id": (
                f"inventory-refresh:{body.worker_id}:"
                f"{worker.get('connection_generation', 0)}"
            ),
            "payload": {},
        })
    return {"success": True, "worker": worker, "heartbeat_interval_seconds": 15,
            "device_report_interval_seconds": 10, "suite_report_interval_seconds": 300,
            "session_id": worker.get("session_id", ""),
            "connection_generation": worker.get("connection_generation", 0)}


@router.post("/workers/{worker_id}/heartbeat")
def heartbeat(worker_id: str, body: WorkerHeartbeat, authorization: str | None = Header(default=None)):
    _authenticate(worker_id, authorization)
    try:
        worker = service().repository.heartbeat(worker_id, body.model_dump())
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    if worker is None:
        raise HTTPException(404, "worker is not registered")
    try:
        from features.devices.integrations_api import (
            reconcile_cluster_usbip_heartbeat,
        )

        reconcile_cluster_usbip_heartbeat(
            worker_id,
            [item.model_dump() for item in body.devices],
        )
    except Exception:
        logger.warning(
            "Failed to reconcile USB/IP inventory for Worker %s",
            worker_id,
            exc_info=True,
        )
    reconciled = []
    from .commands_api import synchronize_command

    for state in body.command_states:
        command = service().repository.ack_command(
            worker_id, state.id, state.model_dump(exclude={"id"})
        )
        if command is None:
            continue
        try:
            synchronize_command(command)
        except Exception:
            logger.warning(
                "Failed to reconcile Worker command %s during heartbeat",
                state.id,
                exc_info=True,
                extra={
                    "worker_id": worker_id,
                    "command_id": state.id,
                    "session_id": body.session_id,
                },
            )
            continue
        reconciled.append(state.id)
    return {
        "success": True,
        "worker": worker,
        "reconciled_command_ids": reconciled,
        "revoked_attempt_ids": worker.get("revoked_attempt_ids", []),
    }


@router.get("/workers")
def list_workers():
    return {"success": True, "workers": service().list_workers()}


@router.get("/worker-tests")
def list_worker_tests(
    request: Request,
    worker_id: str = Query(default=""),
):
    if (worker_id and not service().effective_enabled
            and worker_id != service().config.local_worker_id):
        raise HTTPException(409, "cluster mode is disabled")
    tests = service().repository.list_worker_tests(worker_id)
    if not service().effective_enabled:
        tests = [item for item in tests
                 if item["worker_id"] == service().config.local_worker_id]
    user = get_authenticated_user(request)
    if user is None and authentication_required():
        user = require_authenticated_user(request)
    if user and user.role != "admin":
        visible = []
        for item in tests:
            job_id = str(item.get("job_id") or "")
            job = service().repository.get_job(job_id) if job_id else None
            if job and job.get("owner_id") == user.id:
                visible.append(item)
        tests = visible
    return {"success": True, "tests": tests,
            "retention": {"automatic_cleanup": False,
                          "policy": "artifacts and test history are retained until explicitly deleted"}}


@router.delete("/workers/{worker_id}")
async def delete_worker(
    worker_id: str,
    _admin: CurrentUser | None = Depends(require_elevated_admin_when_auth_required),
):
    svc = service()
    if worker_id == svc.config.local_worker_id:
        raise HTTPException(409, "local Worker cannot be deleted")
    worker = svc.repository.get_worker(worker_id)
    if worker is None:
        raise HTTPException(404, "worker not found")
    if int(worker.get("running_jobs") or 0) > 0:
        raise HTTPException(
            409,
            "Worker has a running platform or external test and cannot be deleted",
        )
    # Remove the remote Agent first.  This prevents an online host from
    # reconnecting and recreating its Worker registration after the row is
    # deleted.  The Agent ACKs before stopping its own systemd unit.
    await _run_worker_command(worker_id, "uninstall_agent", {}, timeout=15)
    if not svc.repository.delete_worker(worker_id):
        raise HTTPException(409, "Worker has an active job and cannot be deleted")
    tokens = _worker_tokens()
    if worker_id in tokens:
        tokens.pop(worker_id)
        _write_worker_tokens(tokens)
    return {"success": True, "deleted": worker_id}


@router.get("/status")
def cluster_status():
    svc = service()
    config = svc.config
    # The local bridge remains online in single-host mode, but it must not
    # override the explicit runtime mode selected by the user.
    workers = svc.list_workers()
    local_online = any(
        w['id'] == config.local_worker_id and w.get('status') in {'online', 'busy'}
        for w in workers
    )
    enabled = svc.effective_enabled
    return {"success": True, "enabled": enabled,
            "remote_dispatch_enabled": config.remote_dispatch_enabled and enabled,
            "global_device_pool_enabled": config.global_device_pool_enabled and enabled,
            "lease_enforcement_enabled": config.lease_enforcement_enabled and enabled,
            "local_worker_id": config.local_worker_id,
            "local_worker_online": local_online,
            "worker_count": len(workers)}



def _require_cluster_enabled(remote: bool = False) -> None:
    svc = service()
    config = svc.config
    # Local-worker operations always work — even when cluster mode is off.
    # This keeps single-host mode fully functional.
    if not remote:
        return
    if not svc.effective_enabled:
        raise HTTPException(409, "cluster mode is disabled")
    if not config.remote_dispatch_enabled:
        raise HTTPException(409, "remote dispatch is disabled")


@router.get("/hosts")
def list_hosts():
    """Return the stable host directory used by host-scoped UI pages."""
    hosts = []
    workers = service().list_workers()
    if not service().effective_enabled:
        workers = [worker for worker in workers if worker["id"] == service().config.local_worker_id]
    for worker in workers:
        capabilities = worker.get("capabilities") or {}
        address = worker.get("address") or worker.get("hostname") or ""
        ssh_user = capabilities.get("ssh_user", "")
        hosts.append({
            "worker_id": worker["id"],
            "name": worker.get("name") or worker["id"],
            "hostname": worker.get("hostname", ""),
            "address": address,
            "ssh_user": ssh_user,
            "ssh_connection": f"{ssh_user}@{address}" if ssh_user and address else "",
            "status": worker.get("status", "offline"),
            "capabilities": capabilities,
        })
    return {"success": True, "hosts": hosts}


def _annotate_adb_proxy_source(devices: list[dict]) -> list[dict]:
    """Stamp ADB Proxy devices with their source worker name/address."""
    from features.devices.adb_proxy_service import adb_proxy_service

    source_by_serial: dict[str, str] = {}
    for assignment in adb_proxy_service.assignments().values():
        target = str(assignment.get("target_worker_id") or "")
        for serial in assignment.get("devices") or []:
            serial = str(serial or "").strip()
            if serial:
                source_by_serial[serial] = target
    if not source_by_serial:
        return devices
    for device in devices:
        if str(device.get("transport") or "") != "adb_proxy":
            continue
        serial = str(device.get("serial") or "")
        target_worker = source_by_serial.get(serial)
        if not target_worker:
            continue
        for assignment in adb_proxy_service.assignments().values():
            if serial in (assignment.get("devices") or []) \
                    and str(assignment.get("target_worker_id") or "") == target_worker:
                properties = device.get("properties") or {}
                properties.setdefault("adb_proxy_source_worker_id", str(
                    assignment.get("source_worker_id") or ""))
                properties.setdefault("adb_proxy_source_name", str(
                    assignment.get("source_name") or ""))
                properties.setdefault("adb_proxy_source_address", str(
                    assignment.get("source_address") or ""))
                device["properties"] = properties
                break
    return devices


@router.get("/devices")
def list_devices(worker_id: str = Query(default="")):
    svc = service()
    if not svc.effective_enabled:
        if worker_id and worker_id != svc.config.local_worker_id:
            raise HTTPException(409, "cluster mode is disabled")
        worker_id = svc.config.local_worker_id
    devices = svc.repository.list_devices(worker_id)
    try:
        from features.users.clients import resolve_client_display_id

        for device in devices:
            owner_id = str(
                device.get("claim_owner_id")
                or device.get("claim_username")
                or ""
            ).strip()
            if owner_id:
                device["claimed_by"] = resolve_client_display_id(owner_id)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        logger.warning(
            "failed to annotate device claim owners for %s: %s",
            worker_id or "all workers",
            exc,
        )
    for device in devices:
        device.pop("claim_owner_id", None)
        device.pop("claim_username", None)
    try:
        from features.devices.integrations_api import (
            annotate_cluster_usbip_devices,
        )

        devices = annotate_cluster_usbip_devices(devices, worker_id)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        logger.warning(
            "failed to annotate USB/IP inventory for %s: %s",
            worker_id or "all workers",
            exc,
        )
    try:
        _annotate_adb_proxy_source(devices)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        logger.warning(
            "failed to annotate ADB Proxy source for %s: %s",
            worker_id or "all workers",
            exc,
        )
    return {"success": True, "devices": devices}


@router.get("/commands/{command_id}")
def get_command(command_id: str, request: Request):
    command = service().repository.get_command(command_id)
    if not command:
        raise HTTPException(404, "command not found")
    user = get_authenticated_user(request)
    if user is None and authentication_required():
        user = require_authenticated_user(request)
    if user and user.role != "admin":
        job_id = str(command.get("job_id") or "")
        job = service().repository.get_job(job_id) if job_id else None
        owner_id = str((command.get("payload") or {}).get("owner_id") or "")
        if not (
            (job and job.get("owner_id") == user.id)
            or owner_id == user.id
        ):
            raise HTTPException(404, "command not found")
    return {"success": True, "command": command}


@router.get("/suites")
def list_suites(worker_id: str = Query(default="")):
    raw_suites = service().repository.list_suites(worker_id)
    local_worker_id = service().config.local_worker_id
    local_present = any(item.get("worker_id") == local_worker_id for item in raw_suites)
    if (not local_present and (not worker_id or worker_id == local_worker_id)):
        # The bridge normally refreshes this inventory. Keep the single-host
        # page usable during the first heartbeat or after a stale DB refresh.
        from .local_bridge import _scan_suites, _suite_roots

        raw_suites = list(raw_suites)
        raw_suites.extend(
            {**item, "worker_id": local_worker_id}
            for item in _scan_suites(_suite_roots())
        )
    suites = []
    for item in raw_suites:
        test_type = str(item.get("suite_type") or "").lower()
        version = str(item.get("suite_version") or "")
        full_version = f"android-{test_type}-{version}" if version and not version.startswith("android-") else version
        suites.append({
            "worker_id": item.get("worker_id", ""),
            "suite_type": item.get("suite_type", ""),
            "suite_version": version,
            "test_type": test_type,
            "version": full_version,
            "tools_path": item.get("tools_path", ""),
            "full_path": item.get("tools_path", ""),
            "suite_key": item.get("suite_key", ""),
            "available": bool(item.get("available", 1)),
        })
    return {"success": True, "suites": suites}


def _local_execute(command_type: str, payload: dict) -> dict:
    """Execute a command directly on the Controller host (worker-local)."""
    from worker_agent.config import WorkerConfig
    from worker_agent.inventory import execute_suite_action as _exec_suite

    from .local_bridge import _suite_roots

    if command_type == "suite_action":
        config = WorkerConfig.__new__(WorkerConfig)
        config.suite_roots = _suite_roots()
        return _exec_suite(config, payload)
    if command_type == "get_config":
        return _read_local_worker_config()
    if command_type == "update_config":
        return _update_local_worker_config(payload)
    if command_type == "adb_proxy":
        from features.devices.adb_proxy_security import pair_code_for_worker
        from worker_agent.adb_proxy import execute_adb_proxy_action

        action = str(payload.get("action") or "")
        pair_code = ""
        if action in {"source_start", "target_connect"}:
            source_worker_id = str(payload.get("source_worker_id") or "")
            pair_code = pair_code_for_worker(
                source_worker_id,
                service().config.local_worker_id,
                str(payload.get("access_token") or ""),
            )
        return execute_adb_proxy_action(action, payload, pair_code=pair_code)
    raise HTTPException(400, f"local execution not supported for {command_type}")


def _worker_config_path() -> Path:
    import os
    return Path(os.getenv("GMS_WORKER_CONFIG",
                          Path.home() / ".config/gms-worker/config.json"))


_CONFIG_FIELDS = {"max_jobs": int}


def _read_local_worker_config() -> dict:
    import json
    path = _worker_config_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raw = {}
    return {key: raw.get(key) for key in _CONFIG_FIELDS}


def _update_local_worker_config(updates: dict) -> dict:
    import json
    import subprocess
    path = _worker_config_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raw = {}
    changed = {}
    for key, caster in _CONFIG_FIELDS.items():
        if key in updates:
            try:
                raw[key] = caster(updates[key])
                changed[key] = raw[key]
            except (TypeError, ValueError):
                raise HTTPException(400, f"invalid value for {key}") from None
    if changed:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(raw, indent=2, ensure_ascii=False), encoding="utf-8")
        subprocess.Popen(["systemctl", "--user", "restart", "gms-worker-agent"])
    return {"updated": changed, "restarted": bool(changed)}


async def _run_worker_command(worker_id: str, command_type: str, payload: dict, timeout: float = 10):
    worker = service().repository.get_worker(worker_id)
    if worker is None:
        # 等待运行中的 Worker 在数据清理后重新注册。
        for _ in range(60):
            await asyncio.sleep(0.1)
            worker = service().repository.get_worker(worker_id)
            if worker is not None:
                break
    if not worker or worker.get("status") not in {"online", "busy"}:
        raise HTTPException(409, "worker is not online")
    # Controller 本机命令直接执行。
    if worker_id == service().config.local_worker_id:
        return await asyncio.to_thread(_local_execute, command_type, payload)
    command = service().repository.create_command({
        "worker_id": worker_id, "command_type": command_type, "payload": payload,
    })
    for _ in range(max(1, int(timeout * 10))):
        await asyncio.sleep(0.1)
        current = service().repository.get_command(command["id"])
        if current and current["status"] in {"completed", "failed", "cancelled"}:
            if current["status"] != "completed":
                raise HTTPException(502, current.get("error") or "worker command failed")
            return current.get("result") or {}
    raise HTTPException(504, "worker command timed out")


@router.get("/suites/files")
async def cluster_suite_files(worker_id: str = Query(...), suite_path: str = Query(...),
                              path: str = Query(default="")):
    _require_cluster_enabled(remote=worker_id != service().config.local_worker_id)
    result = await _run_worker_command(worker_id, "suite_action", {
        "action": "list", "suite_path": suite_path, "path": path,
    })
    return {"success": True, "data": result}


@router.get("/suites/search")
async def cluster_suite_search(worker_id: str = Query(...), suite_path: str = Query(...),
                               query: str = Query(..., min_length=1),
                               limit: int = Query(default=30, ge=1, le=200)):
    _require_cluster_enabled(remote=worker_id != service().config.local_worker_id)
    result = await _run_worker_command(worker_id, "suite_action", {
        "action": "search", "suite_path": suite_path, "query": query, "limit": limit,
    }, timeout=30)
    return {"success": True, "data": result}


@router.post("/suites/results")
async def cluster_suite_results(
    worker_id: str = Query(...),
    suite_path: str = Query(...),
    _user: CurrentUser | None = Depends(require_authenticated_user_when_auth_required),
):
    from features.test_execution import parse_tradefed_list_results

    _require_cluster_enabled(remote=worker_id != service().config.local_worker_id)
    result = await _run_worker_command(worker_id, "suite_action", {
        "action": "list_results", "suite_path": suite_path,
    }, timeout=100)
    parsed = parse_tradefed_list_results(result.get("raw_output") or "")
    return {
        "success": True,
        "columns": parsed.get("columns", []),
        "results": parsed.get("results", []),
        "count": len(parsed.get("results", [])),
        "worker_id": worker_id,
        "launcher": result.get("launcher", ""),
    }


@router.post("/suites/download")
def cluster_suite_download(
    body: ClusterSuiteDownload,
    _admin: CurrentUser = Depends(require_role("admin")),
):
    _require_cluster_enabled(remote=body.worker_id != service().config.local_worker_id)
    worker = service().repository.get_worker(body.worker_id)
    if not worker or worker.get("status") not in {"online", "busy"}:
        raise HTTPException(409, "worker is not online")
    command = service().repository.create_command({
        "worker_id": body.worker_id, "command_type": "suite_action",
        "payload": {"action": "download_url", "url": body.url,
                    "filename": body.filename, "size_bytes": body.size_bytes},
    })
    return {"success": True, "accepted": True, "command_id": command["id"]}


@router.get("/suites/archives")
async def cluster_suite_archives(
    worker_id: str = Query(...),
    _admin: CurrentUser = Depends(require_role("admin")),
):
    _require_cluster_enabled(remote=worker_id != service().config.local_worker_id)
    result = await _run_worker_command(worker_id, "suite_action", {"action": "list_archives"})
    return {"success": True, **result}


@router.post("/suites/extract")
def cluster_suite_extract(
    body: ClusterSuiteExtract,
    _admin: CurrentUser = Depends(require_role("admin")),
):
    _require_cluster_enabled(remote=body.worker_id != service().config.local_worker_id)
    worker = service().repository.get_worker(body.worker_id)
    if not worker or worker.get("status") not in {"online", "busy"}:
        raise HTTPException(409, "worker is not online")
    command = service().repository.create_command({
        "worker_id": body.worker_id, "command_type": "suite_action",
        "payload": {"action": "extract", "archive_path": body.archive_path,
                    "target_dir_name": body.target_dir_name},
    })
    return {"success": True, "accepted": True, "command_id": command["id"]}


@router.get("/suites/download")
async def cluster_suite_download_file(worker_id: str = Query(...), suite_path: str = Query(...),
                                      path: str = Query(...), inline: bool = Query(default=False)):
    _require_cluster_enabled(remote=worker_id != service().config.local_worker_id)
    result = await _run_worker_command(worker_id, "suite_action", {
        "action": "read_file", "suite_path": suite_path, "path": path,
    }, timeout=40)
    encoded = result.get("content_base64") or ""
    try:
        content = base64.b64decode(encoded, validate=True)
    except ValueError as exc:
        raise HTTPException(502, "worker returned invalid file data") from exc
    disposition = "inline" if inline else "attachment"
    filename = re.sub(r"[^A-Za-z0-9._-]", "_", result.get("filename") or "download")
    return Response(content=content, media_type=result.get("content_type") or "application/octet-stream",
                    headers={"Content-Disposition": f'{disposition}; filename="{filename}"'})


@router.post("/commands")
def create_command(
    body: CommandCreate,
    _admin: CurrentUser = Depends(require_role("admin")),
):
    if service().repository.get_worker(body.worker_id) is None:
        raise HTTPException(404, "worker not found")
    return {"success": True, "command": service().repository.create_command(body.model_dump())}


def _mount_subrouters() -> None:
    """Mount split routers after this module's shared dependencies exist."""
    global device_action
    from .commands_api import router as commands_router
    from .deployment_api import router as deployment_router
    from .device_actions_api import device_action as device_action
    from .device_actions_api import router as device_actions_router
    from .job_control_api import router as job_control_router
    from .jobs_api import router as jobs_router
    from .suite_library_api import router as suite_library_router
    from .timeline_api import router as timeline_router
    from .transfers_api import router as transfers_router
    from .worker_settings_api import router as worker_settings_router

    router.include_router(deployment_router)
    router.include_router(device_actions_router)
    router.include_router(commands_router)
    router.include_router(transfers_router)
    router.include_router(jobs_router)
    router.include_router(job_control_router)
    router.include_router(timeline_router)
    router.include_router(suite_library_router)
    router.include_router(worker_settings_router)


_mount_subrouters()
