from __future__ import annotations

import asyncio
import base64
import ipaddress
import re
from pathlib import Path

from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, Response

from .config import ClusterConfig
from .models import (
    ClusterDeviceAction,
    ClusterSuiteDownload,
    ClusterSuiteExtract,
    CommandAck,
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


def configure_cluster(data_root: str | Path) -> ClusterService:
    global cluster_service
    repository = ClusterRepository(Path(data_root) / "cluster/cluster.sqlite3")
    config = ClusterConfig.load()
    cluster_service = ClusterService(repository, config=config)
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
    return {"success": True, "worker": worker, "heartbeat_interval_seconds": 15,
            "device_report_interval_seconds": 10, "suite_report_interval_seconds": 300}


@router.post("/workers/{worker_id}/heartbeat")
def heartbeat(worker_id: str, body: WorkerHeartbeat, authorization: str | None = Header(default=None)):
    _authenticate(worker_id, authorization)
    worker = service().repository.heartbeat(worker_id, body.model_dump())
    if worker is None:
        raise HTTPException(404, "worker is not registered")
    return {"success": True, "worker": worker}


@router.get("/workers")
def list_workers():
    return {"success": True, "workers": service().list_workers()}


@router.get("/worker-tests")
def list_worker_tests(worker_id: str = Query(default="")):
    if (worker_id and not service().effective_enabled
            and worker_id != service().config.local_worker_id):
        raise HTTPException(409, "cluster mode is disabled")
    tests = service().repository.list_worker_tests(worker_id)
    if not service().effective_enabled:
        tests = [item for item in tests
                 if item["worker_id"] == service().config.local_worker_id]
    return {"success": True, "tests": tests,
            "retention": {"automatic_cleanup": False,
                          "policy": "artifacts and test history are retained until explicitly deleted"}}


@router.delete("/workers/{worker_id}")
def delete_worker(worker_id: str):
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



@router.post("/mode")
def set_cluster_mode(body: dict):
    """Toggle between single-host and cluster mode at runtime."""
    from pydantic import BaseModel

    class ModeRequest(BaseModel):
        enabled: bool

    req = ModeRequest(**body)
    svc = service()
    svc.set_runtime_enabled(req.enabled)
    workers = svc.list_workers()
    local_online = any(
        w['id'] == svc.config.local_worker_id and w.get('status') in {'online', 'busy'}
        for w in workers
    )
    effective = svc.effective_enabled
    return {"success": True, "enabled": effective,
            "runtime_enabled": svc._runtime_enabled,
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


@router.get("/devices")
def list_devices(worker_id: str = Query(default="")):
    svc = service()
    if not svc.effective_enabled:
        if worker_id and worker_id != svc.config.local_worker_id:
            raise HTTPException(409, "cluster mode is disabled")
        worker_id = svc.config.local_worker_id
    return {"success": True, "devices": svc.repository.list_devices(worker_id)}


@router.post("/devices/actions")
async def device_action(body: ClusterDeviceAction):
    is_local = body.worker_id == service().config.local_worker_id
    _require_cluster_enabled(remote=not is_local)
    worker = service().repository.get_worker(body.worker_id)
    if not worker or worker.get("status") not in {"online", "busy", "draining"}:
        raise HTTPException(409, "worker is not online")
    known = {item["id"]: item for item in service().repository.list_devices(body.worker_id)}
    # Read-only inspection actions are safe even on external_busy devices.
    read_only_actions = {
        "screenshot", "layout", "get_properties", "packages_with_path",
        "packages_all", "features", "props", "config_explore", "override_status",
    }
    is_read_only = body.action in read_only_actions
    requested = []
    for value in body.devices:
        device_id = value if value.startswith(f"{body.worker_id}:") else f"{body.worker_id}:{value}"
        device = known.get(device_id)
        if not device:
            raise HTTPException(409, f"device is not available on worker: {value}")
        device_state = device.get("state")
        if device_state in {"offline", "unknown"}:
            raise HTTPException(409, f"device is offline: {value}")
        if device_state == "external_busy" and not is_read_only:
            raise HTTPException(409, f"device is busy with a manual Tradefed test: {value}")
        requested.append(device_id)
    # When targeting the local Controller host, execute device actions
    # directly instead of queuing a command that nobody polls.
    if is_local:
        from worker_agent.inventory import execute_device_action as _exec_action
        result = _exec_action(body.action, requested, body.model_dump())
        return {"success": True, **result}
    command = service().repository.create_command({
        "worker_id": body.worker_id,
        "command_type": "device_action",
        "payload": {**body.model_dump(exclude={"worker_id", "devices"}), "devices": requested},
    })
    # Device actions are short. Waiting here preserves the existing device API's
    # completed-result semantics while still using the outbound Worker channel.
    wait_steps = 1800 if body.action in {
        "config_explore", "override_apply", "override_revert",
    } else 350 if body.action in {
        "screenshot", "layout", "packages_with_path", "packages_all",
        "features", "props", "override_status",
    } else 100
    for _ in range(wait_steps):
        await asyncio.sleep(0.1)
        current = service().repository.get_command(command["id"])
        if current and current["status"] in {"completed", "failed", "cancelled"}:
            if current["status"] != "completed":
                raise HTTPException(502, current.get("error") or "worker device action failed")
            result = current.get("result") or {}
            if body.action == "screenshot" and result.get("image"):
                service().repository.compact_command_result(command["id"], {
                    "serial": result.get("serial", ""), "image_bytes": len(result["image"]),
                    "transient_result": True,
                })
            return {"success": True, **result, "command_id": command["id"]}
    return {"success": True, "accepted": True, "command_id": command["id"]}


@router.get("/commands/{command_id}")
def get_command(command_id: str):
    command = service().repository.get_command(command_id)
    if not command:
        raise HTTPException(404, "command not found")
    return {"success": True, "command": command}


@router.get("/suites")
def list_suites(worker_id: str = Query(default="")):
    return {"success": True, "suites": service().repository.list_suites(worker_id)}


def _local_execute(command_type: str, payload: dict) -> dict:
    """Execute a command directly on the Controller host (worker-local)."""
    from worker_agent.config import WorkerConfig
    from worker_agent.inventory import execute_suite_action as _exec_suite

    from .local_bridge import _suite_roots

    if command_type == "suite_action":
        config = WorkerConfig.__new__(WorkerConfig)
        config.suite_roots = _suite_roots()
        return _exec_suite(config, payload)
    raise HTTPException(400, f"local execution not supported for {command_type}")


async def _run_worker_command(worker_id: str, command_type: str, payload: dict, timeout: float = 10):
    worker = service().repository.get_worker(worker_id)
    if not worker or worker.get("status") not in {"online", "busy"}:
        raise HTTPException(409, "worker is not online")
    # Local Controller host: execute directly (no agent polls commands here).
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
async def cluster_suite_results(worker_id: str = Query(...), suite_path: str = Query(...)):
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
def cluster_suite_download(body: ClusterSuiteDownload):
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
async def cluster_suite_archives(worker_id: str = Query(...)):
    _require_cluster_enabled(remote=worker_id != service().config.local_worker_id)
    result = await _run_worker_command(worker_id, "suite_action", {"action": "list_archives"})
    return {"success": True, **result}


def _controller_suite_archives() -> list[Path]:
    """Return archives directly inside configured Controller suite roots."""
    from .local_bridge import _suite_roots
    extensions = (".zip", ".tar", ".tar.gz", ".tgz", ".tar.bz2")
    archives: list[Path] = []
    for root in _suite_roots():
        resolved = root.expanduser().resolve()
        if resolved.is_dir():
            archives.extend(path for path in resolved.iterdir()
                            if path.is_file() and path.name.lower().endswith(extensions))
    return archives


@router.get("/suite-library")
def controller_suite_library():
    archives = []
    for path in _controller_suite_archives():
        stat = path.stat()
        archives.append({"name": path.name, "size": stat.st_size,
                         "modified": int(stat.st_mtime)})
    archives.sort(key=lambda item: item["modified"], reverse=True)
    return {"success": True, "archives": archives}


@router.get("/suite-library/{filename}")
def download_controller_suite_archive(filename: str):
    if Path(filename).name != filename:
        raise HTTPException(400, "invalid archive filename")
    path = next((item for item in _controller_suite_archives() if item.name == filename), None)
    if path is None:
        raise HTTPException(404, "suite archive not found")
    return FileResponse(path, filename=path.name, media_type="application/octet-stream")


@router.get("/suite-library-download/{safe_filename}")
def download_controller_suite_archive_compat(safe_filename: str, filename: str = Query(...)):
    """Serve an original archive through an old-Worker-compatible URL name."""
    if not re.fullmatch(r"[A-Za-z0-9._+-]+", safe_filename) or Path(filename).name != filename:
        raise HTTPException(400, "invalid archive filename")
    path = next((item for item in _controller_suite_archives() if item.name == filename), None)
    if path is None:
        raise HTTPException(404, "suite archive not found")
    return FileResponse(path, filename=path.name, media_type="application/octet-stream")


@router.get("/suite-library-download/{safe_filename}/{filename}")
def download_controller_suite_archive_named(safe_filename: str, filename: str):
    """Keep the original name as the URL's final segment for older Workers."""
    if (not re.fullmatch(r"[A-Za-z0-9._+-]+", safe_filename)
            or Path(filename).name != filename):
        raise HTTPException(400, "invalid archive filename")
    path = next((item for item in _controller_suite_archives() if item.name == filename), None)
    if path is None:
        raise HTTPException(404, "suite archive not found")
    return FileResponse(path, filename=path.name, media_type="application/octet-stream")


@router.post("/suites/extract")
def cluster_suite_extract(body: ClusterSuiteExtract):
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
def create_command(body: CommandCreate):
    if service().repository.get_worker(body.worker_id) is None:
        raise HTTPException(404, "worker not found")
    return {"success": True, "command": service().repository.create_command(body.model_dump())}


@router.post("/workers/{worker_id}/restart-vnc")
async def restart_worker_vnc(worker_id: str):
    """Restart x11vnc/websockify on a worker to recover from zombie VNC processes."""
    worker = service().repository.get_worker(worker_id)
    if worker is None:
        raise HTTPException(404, "worker not found")
    if worker.get("status") not in {"online", "busy"}:
        raise HTTPException(409, "worker is not online")
    if worker_id == service().config.local_worker_id:
        from worker_agent.app import restart_local_vnc
        result = await asyncio.to_thread(restart_local_vnc)
        return {"success": result.get("rfb_ok", False), "result": result}
    try:
        result = await _run_worker_command(worker_id, "restart_vnc", {}, timeout=20)
        return {"success": result.get("rfb_ok", False), "result": result}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502, f"restart_vnc failed: {exc}") from exc


@router.post("/workers/{worker_id}/commands/poll")
async def poll_commands(worker_id: str, authorization: str | None = Header(default=None)):
    _authenticate(worker_id, authorization)
    if service().repository.get_worker(worker_id) is None:
        raise HTTPException(404, "worker is not registered")
    # Short long-poll. Keeping DB reads bounded makes shutdown/reload responsive.
    for _ in range(20):
        commands = service().repository.poll_commands(worker_id)
        if commands:
            return {"success": True, "commands": commands}
        await asyncio.sleep(0.5)
    return {"success": True, "commands": []}


@router.post("/workers/{worker_id}/commands/{command_id}/ack")
def ack_command(worker_id: str, command_id: str, body: CommandAck,
                authorization: str | None = Header(default=None)):
    _authenticate(worker_id, authorization)
    command = service().repository.ack_command(worker_id, command_id, body.model_dump())
    if command is None:
        raise HTTPException(404, "command not found")
    service().repository.sync_job_from_command(command)
    if command.get("job_id") and command.get("command_type") == "start_test" \
            and command.get("status") in {"completed", "failed", "cancelled"}:
        from .jobs_api import update_cluster_report_status

        update_cluster_report_status(
            command["job_id"], command["status"], command.get("error", "")
        )
    from .transfers_api import cleanup_staged_firmware

    cleanup_staged_firmware(command)
    if command.get("command_type") in {"suite_export", "device_export"} \
            and command.get("status") in {"failed", "cancelled"}:
        transfer_id = (command.get("payload") or {}).get("transfer_id", "")
        if transfer_id:
            service().repository.update_transfer(transfer_id, status="failed",
                error=command.get("error") or "worker export failed")
    return {"success": True, "command": command}


def _mount_subrouters() -> None:
    """Mount split routers after this module's shared dependencies exist."""
    from .deployment_api import router as deployment_router
    from .jobs_api import router as jobs_router
    from .transfers_api import router as transfers_router

    router.include_router(deployment_router)
    router.include_router(transfers_router)
    router.include_router(jobs_router)


_mount_subrouters()
