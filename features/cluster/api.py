from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import os
import re
import shlex
import tarfile
import tempfile
from pathlib import Path

from fastapi import APIRouter, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, Response
import base64

from .models import (ClusterDeviceAction, ClusterJobCreate, ClusterSuiteDownload, ClusterSuiteExtract, CommandAck, CommandCreate, JobEventBatch, TransferComplete,
                     WorkerHeartbeat, WorkerRegistration)
from .repository import ClusterRepository, utc_now
from .service import ClusterService
from .config import ClusterConfig
from foundation.config import config_manager


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


def _worker_tokens() -> dict[str, str]:
    # worker-246:token,worker-local:token
    result = {}
    for item in os.getenv("GMS_CLUSTER_WORKER_TOKENS", "").split(","):
        worker_id, separator, token = item.partition(":")
        if separator and worker_id.strip() and token.strip():
            result[worker_id.strip()] = token.strip()
    return result


def _write_worker_tokens(tokens: dict[str, str]) -> None:
    value = ",".join(f"{key}:{item}" for key, item in sorted(tokens.items()))
    os.environ["GMS_CLUSTER_WORKER_TOKENS"] = value
    env_path = Path(__file__).resolve().parents[2] / ".env.production"
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    replacement = f"GMS_CLUSTER_WORKER_TOKENS={value}"
    lines = [replacement if line.startswith("GMS_CLUSTER_WORKER_TOKENS=") else line for line in lines]
    if not any(line.startswith("GMS_CLUSTER_WORKER_TOKENS=") for line in lines):
        lines.append(replacement)
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _persist_worker_token(worker_id: str, token: str) -> None:
    tokens = _worker_tokens()
    tokens[worker_id] = token
    _write_worker_tokens(tokens)


def _authenticate(worker_id: str, authorization: str | None) -> None:
    expected = _worker_tokens().get(worker_id)
    if not expected:
        raise HTTPException(503, f"worker token is not configured for {worker_id}")
    supplied = (authorization or "").removeprefix("Bearer ").strip()
    if not hmac.compare_digest(
        hashlib.sha256(supplied.encode()).digest(),
        hashlib.sha256(expected.encode()).digest(),
    ):
        raise HTTPException(401, "invalid worker token")


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


@router.delete("/workers/{worker_id}")
def delete_worker(worker_id: str):
    svc = service()
    if worker_id == svc.config.local_worker_id:
        raise HTTPException(409, "local Worker cannot be deleted")
    if svc.repository.get_worker(worker_id) is None:
        raise HTTPException(404, "worker not found")
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


@router.post("/workers/deploy")
async def deploy_worker(body: dict):
    """Upload and install a Worker using explicitly supplied SSH credentials."""
    worker_id = str(body.get("worker_id") or "").strip()
    connection = str(body.get("ssh_host") or "").strip()
    password = str(body.get("password") or "")
    token = str(body.get("token") or "").strip()
    controller_url = str(body.get("controller_url") or "").strip().rstrip("/")
    suite_root = str(body.get("suite_root") or "~/GMS-Suite").strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]+", worker_id):
        raise HTTPException(400, "invalid worker ID")
    if "@" not in connection or not token or not controller_url.startswith(("http://", "https://")):
        raise HTTPException(400, "SSH host, Worker Token and Controller URL are required")
    username, hostname = connection.split("@", 1)
    _persist_worker_token(worker_id, token)

    def _deploy() -> dict:
        from features.system.ssh import SSHManager
        manager = SSHManager(pool_size=1)
        ssh = manager.create_connection({"host": hostname, "username": username,
                                         "password": password})
        if ssh is None:
            raise RuntimeError("SSH connection failed")
        # The same credential is required by the host-terminal workspace.
        # Persist it only after SSH authentication has actually succeeded.
        if password and not config_manager.upsert_device_host_password(connection, password):
            raise RuntimeError("SSH connected, but saving the host credential failed")
        project_root = Path(__file__).resolve().parents[2]
        archive_path = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as temporary:
                archive_path = Path(temporary.name)
            with tarfile.open(archive_path, "w:gz") as bundle:
                bundle.add(project_root / "worker_agent", arcname="worker_agent")
                bundle.add(project_root / "scripts/install_cluster_worker.sh",
                           arcname="scripts/install_cluster_worker.sh")
            sftp = ssh.open_sftp()
            try:
                sftp.put(str(archive_path), "/tmp/gms-worker-setup.tar.gz")
            finally:
                sftp.close()
            install = ("rm -rf ~/gms-worker-setup && mkdir -p ~/gms-worker-setup && "
                       "tar -xzf /tmp/gms-worker-setup.tar.gz -C ~/gms-worker-setup && "
                       "cd ~/gms-worker-setup && "
                       f"bash scripts/install_cluster_worker.sh {shlex.quote(worker_id)} "
                       f"{shlex.quote(controller_url)} {shlex.quote(token)} - {shlex.quote(suite_root)} "
                       f"{shlex.quote(hostname)}")
            _stdin, stdout, stderr = ssh.exec_command(install, timeout=900, get_pty=True)
            exit_code = stdout.channel.recv_exit_status()
            output = (stdout.read() + stderr.read()).decode("utf-8", errors="replace")[-12000:]
            if exit_code != 0:
                raise RuntimeError(output or f"installer exited with {exit_code}")
            return {"success": True, "worker_id": worker_id, "output": output}
        finally:
            ssh.close()
            if archive_path:
                archive_path.unlink(missing_ok=True)

    try:
        result = await asyncio.to_thread(_deploy)
        # An installer exiting successfully only proves that files and units
        # were created. Do not report deployment success until the Agent has
        # authenticated and registered with this Controller.
        for _ in range(20):
            if service().repository.get_worker(worker_id) is not None:
                return {**result, "registered": True}
            await asyncio.sleep(1)
        raise HTTPException(
            502,
            "安装脚本已完成，但 Worker 在 20 秒内未注册。请在目标主机执行 "
            "systemctl --user status gms-worker-agent 和 "
            "journalctl --user -u gms-worker-agent -n 50 --no-pager",
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502, f"automatic deployment failed: {exc}") from exc

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
    if not worker or worker.get("status") not in {"online", "busy"}:
        raise HTTPException(409, "worker is not online")
    known = {item["id"]: item for item in service().repository.list_devices(body.worker_id)}
    requested = []
    for value in body.devices:
        device_id = value if value.startswith(f"{body.worker_id}:") else f"{body.worker_id}:{value}"
        device = known.get(device_id)
        if not device or device.get("state") in {"offline", "unknown"}:
            raise HTTPException(409, f"device is not available on worker: {value}")
        requested.append(device_id)
    # When targeting the local Controller host, execute device actions
    # directly instead of queuing a command that nobody polls.
    if is_local:
        from .local_bridge import _probe_devices
        from worker_agent.inventory import execute_device_action as _exec_action
        result = _exec_action(body.action, requested,
                              {"x": body.x, "y": body.y, "ssid": body.ssid, "password": body.password})
        return {"success": True, **result}
    command = service().repository.create_command({
        "worker_id": body.worker_id,
        "command_type": "device_action",
        "payload": {"action": body.action, "devices": requested, "x": body.x, "y": body.y,
                    "ssid": body.ssid, "password": body.password},
    })
    # Device actions are short. Waiting here preserves the existing device API's
    # completed-result semantics while still using the outbound Worker channel.
    wait_steps = 350 if body.action in {"screenshot", "layout"} else 100
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
    from worker_agent.inventory import execute_suite_action as _exec_suite
    from worker_agent.config import WorkerConfig
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


def _transfer_root() -> Path:
    return service().repository.db_path.parent / "transfers"


def _firmware_root() -> Path:
    return service().repository.db_path.parent / "firmware"


@router.post("/firmware/stage")
async def stage_worker_firmware(worker_id: str = Form(...), devices: str = Form(...),
                                firmware_file: UploadFile = File(...)):
    _require_cluster_enabled(remote=worker_id != service().config.local_worker_id)
    worker = service().repository.get_worker(worker_id)
    if not worker or worker.get("status") not in {"online", "busy"}:
        raise HTTPException(409, "worker is not online")
    requested = [item.strip() for item in devices.split(",") if item.strip()]
    if len(requested) != 1:
        raise HTTPException(400, "cluster firmware flashing requires exactly one device")
    device_id = requested[0] if requested[0].startswith(f"{worker_id}:") else f"{worker_id}:{requested[0]}"
    if device_id not in {item["id"] for item in service().repository.list_devices(worker_id)}:
        raise HTTPException(409, "device does not belong to worker")
    filename = re.sub(r"[^A-Za-z0-9._+-]", "_", Path(firmware_file.filename or "firmware.img").name)
    stage_id = "fw-" + os.urandom(16).hex()
    directory = _firmware_root() / stage_id
    directory.mkdir(parents=True, exist_ok=False)
    target = directory / filename
    digest, total = hashlib.sha256(), 0
    limit = int(os.getenv("GMS_CLUSTER_FIRMWARE_MAX_BYTES", str(20 * 1024 ** 3)))
    with target.open("wb") as output:
        while chunk := await firmware_file.read(4 * 1024 * 1024):
            total += len(chunk)
            if total > limit:
                target.unlink(missing_ok=True)
                raise HTTPException(413, "firmware image is too large")
            digest.update(chunk)
            output.write(chunk)
    if not total:
        target.unlink(missing_ok=True)
        raise HTTPException(400, "firmware image is empty")
    command = service().repository.create_command({"worker_id": worker_id,
        "command_type": "flash_firmware", "payload": {"stage_id": stage_id,
        "filename": filename, "sha256": digest.hexdigest(), "size_bytes": total,
        "devices": [device_id]}})
    return {"success": True, "stage_id": stage_id, "command_id": command["id"], "size_bytes": total}


@router.get("/workers/{worker_id}/firmware/{stage_id}")
def download_staged_firmware(worker_id: str, stage_id: str, filename: str = Query(...),
                             authorization: str | None = Header(default=None)):
    _authenticate(worker_id, authorization)
    if not re.fullmatch(r"fw-[a-f0-9]{32}", stage_id):
        raise HTTPException(400, "invalid firmware stage")
    safe_name = re.sub(r"[^A-Za-z0-9._+-]", "_", Path(filename).name)
    path = (_firmware_root() / stage_id / safe_name).resolve()
    if not path.is_relative_to(_firmware_root().resolve()) or not path.is_file():
        raise HTTPException(404, "staged firmware not found")
    return FileResponse(path, filename=safe_name)


@router.post("/gsi/stage")
async def stage_worker_gsi(worker_id: str = Form(...), devices: str = Form(...),
                           system_file: UploadFile = File(...),
                           vendor_file: UploadFile | None = File(default=None)):
    _require_cluster_enabled(remote=worker_id != service().config.local_worker_id)
    requested = [item.strip() for item in devices.split(",") if item.strip()]
    if len(requested) != 1:
        raise HTTPException(400, "cluster GSI flashing requires exactly one device")
    device_id = requested[0] if requested[0].startswith(f"{worker_id}:") else f"{worker_id}:{requested[0]}"
    if device_id not in {item["id"] for item in service().repository.list_devices(worker_id)}:
        raise HTTPException(409, "device does not belong to worker")
    stage_id = "fw-" + os.urandom(16).hex()
    directory = _firmware_root() / stage_id
    directory.mkdir(parents=True)
    files = []
    for kind, upload in (("system", system_file), ("vendor", vendor_file)):
        if upload is None:
            continue
        name = f"{kind}.img"
        target, digest, total = directory / name, hashlib.sha256(), 0
        with target.open("wb") as output:
            while chunk := await upload.read(4 * 1024 * 1024):
                total += len(chunk); digest.update(chunk); output.write(chunk)
        if not total:
            raise HTTPException(400, f"{kind} image is empty")
        files.append({"kind": kind, "filename": name, "size_bytes": total, "sha256": digest.hexdigest()})
    command = service().repository.create_command({"worker_id": worker_id,
        "command_type": "flash_gsi", "payload": {"stage_id": stage_id,
        "files": files, "devices": [device_id]}})
    return {"success": True, "stage_id": stage_id, "command_id": command["id"]}


@router.post("/suites/export")
def create_suite_export(worker_id: str = Query(...), suite_path: str = Query(...),
                        path: str = Query(...), directory: bool = Query(default=False)):
    _require_cluster_enabled(remote=worker_id != service().config.local_worker_id)
    worker = service().repository.get_worker(worker_id)
    if not worker or worker.get("status") not in {"online", "busy"}:
        raise HTTPException(409, "worker is not online")
    transfer = service().repository.create_transfer(worker_id)
    command = service().repository.create_command({
        "worker_id": worker_id, "command_type": "suite_export",
        "payload": {"transfer_id": transfer["id"], "suite_path": suite_path,
                    "path": path, "directory": directory},
    })
    return {"success": True, "transfer": transfer, "command_id": command["id"]}


@router.put("/transfers/{transfer_id}/chunks/{index}")
async def upload_transfer_chunk(transfer_id: str, index: int, request: Request,
                                worker_id: str = Header(alias="X-GMS-Worker-ID"),
                                authorization: str | None = Header(default=None)):
    _authenticate(worker_id, authorization)
    transfer = service().repository.get_transfer(transfer_id)
    if not transfer or transfer["worker_id"] != worker_id:
        raise HTTPException(404, "transfer not found for worker")
    if index < 0 or index > 100000:
        raise HTTPException(400, "invalid chunk index")
    body = await request.body()
    max_chunk = int(os.getenv("GMS_CLUSTER_TRANSFER_CHUNK_BYTES", str(8 * 1024 * 1024)))
    if not body or len(body) > max_chunk:
        raise HTTPException(413, "invalid transfer chunk size")
    chunk_dir = _transfer_root() / transfer_id / "chunks"
    chunk_dir.mkdir(parents=True, exist_ok=True)
    (chunk_dir / f"{index:08d}.part").write_bytes(body)
    service().repository.update_transfer(transfer_id, status="uploading")
    return {"success": True, "index": index, "size_bytes": len(body)}


@router.post("/transfers/{transfer_id}/complete")
def complete_transfer(transfer_id: str, body: TransferComplete,
                      worker_id: str = Header(alias="X-GMS-Worker-ID"),
                      authorization: str | None = Header(default=None)):
    _authenticate(worker_id, authorization)
    transfer = service().repository.get_transfer(transfer_id)
    if not transfer or transfer["worker_id"] != worker_id:
        raise HTTPException(404, "transfer not found for worker")
    safe_name = re.sub(r"[^A-Za-z0-9._+-]", "_", Path(body.filename).name)
    root = _transfer_root() / transfer_id
    chunks = [root / "chunks" / f"{index:08d}.part" for index in range(body.chunk_count)]
    if not all(path.is_file() for path in chunks):
        raise HTTPException(409, "transfer chunks are incomplete")
    destination = root / safe_name
    digest = hashlib.sha256()
    total = 0
    with destination.open("wb") as output:
        for chunk in chunks:
            data = chunk.read_bytes()
            total += len(data)
            digest.update(data)
            output.write(data)
    if total != body.size_bytes or digest.hexdigest() != body.sha256:
        destination.unlink(missing_ok=True)
        raise HTTPException(409, "transfer checksum or size mismatch")
    for chunk in chunks:
        chunk.unlink(missing_ok=True)
    transfer = service().repository.update_transfer(transfer_id, status="completed",
        filename=safe_name, relative_path=str(destination.relative_to(_transfer_root())),
        size_bytes=total, sha256=body.sha256, completed_at=utc_now())
    return {"success": True, "transfer": transfer}


@router.get("/transfers/{transfer_id}")
def get_transfer(transfer_id: str):
    transfer = service().repository.get_transfer(transfer_id)
    if not transfer:
        raise HTTPException(404, "transfer not found")
    return {"success": True, "transfer": transfer}


@router.get("/transfers/{transfer_id}/download")
def download_transfer(transfer_id: str):
    transfer = service().repository.get_transfer(transfer_id)
    if not transfer or transfer["status"] != "completed":
        raise HTTPException(409, "transfer is not complete")
    path = (_transfer_root() / transfer["relative_path"]).resolve()
    if not path.is_relative_to(_transfer_root().resolve()) or not path.is_file():
        raise HTTPException(404, "transfer file not found")
    return FileResponse(path, filename=transfer["filename"])


@router.post("/commands")
def create_command(body: CommandCreate):
    if service().repository.get_worker(body.worker_id) is None:
        raise HTTPException(404, "worker not found")
    return {"success": True, "command": service().repository.create_command(body.model_dump())}


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
    if command.get("command_type") == "suite_export" and command.get("status") in {"failed", "cancelled"}:
        transfer_id = (command.get("payload") or {}).get("transfer_id", "")
        if transfer_id:
            service().repository.update_transfer(transfer_id, status="failed",
                error=command.get("error") or "worker export failed")
    return {"success": True, "command": command}


@router.post("/jobs")
def create_job(body: ClusterJobCreate):
    _require_cluster_enabled(remote=body.worker_id not in {"auto", service().config.local_worker_id})
    data = body.model_dump()
    if data["worker_id"] == "auto":
        try:
            data["worker_id"], selected_devices = service().select_worker(
                data["suite_key"], data["device_count"]
            )
            if not data["devices"]:
                data["devices"] = selected_devices
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
    if not data["argv"]:
        suite_path = data["suite_path"]
        suite_type = ""
        if not suite_path:
            suites = [item for item in service().repository.list_suites(data["worker_id"])
                      if item["suite_key"] == data["suite_key"] and item["available"]]
            if not suites:
                raise HTTPException(409, "suite is not available on worker")
            suite_path = suites[0]["tools_path"]
            suite_type = suites[0]["suite_type"].lower()
        # A harmless console listing is the safe default; real runs supply the
        # existing Tradefed arguments selected by the test page.
        executable = str(Path(suite_path) / f"{suite_type}-tradefed") if suite_type else ""
        if not executable and data["worker_id"] == "worker-local":
            executable = next((str(Path(suite_path) / name) for name in
                               ("cts-tradefed", "gts-tradefed", "vts-tradefed", "sts-tradefed")
                               if (Path(suite_path) / name).exists()), "")
        if not executable:
            raise HTTPException(409, "suite executable not found")
        data["argv"] = [executable, "list", "devices"]
    try:
        job = service().repository.create_job_with_leases(data)
        attempt = job["attempt"]
        command = service().repository.create_command({
            "worker_id": data["worker_id"], "command_type": "start_test",
            "job_id": job["id"], "attempt_id": job["current_attempt_id"],
            "payload": {"worker_job_id": f"wj-{job['id']}", "argv": data["argv"],
                        "env": data["env"], "devices": data["devices"]},
        })
        service().repository.attach_command_to_job(job["id"], command)
        return {"success": True, "job": service().repository.get_job(job["id"]),
                "command": command}
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.get("/jobs")
def list_jobs(limit: int = Query(default=100, ge=1, le=500)):
    return {"success": True, "jobs": service().repository.list_jobs(limit)}


@router.get("/jobs/{job_id}")
def get_job(job_id: str):
    job = service().repository.get_job(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    return {"success": True, "job": job}


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: str):
    job = service().repository.get_job(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    if job["status"] in {"completed", "failed", "cancelled"}:
        return {"success": True, "job": job, "already_terminal": True}
    # Worker job ids are deterministic, allowing cancellation to be queued
    # immediately after Start even before the first running ACK arrives.
    worker_job_id = (job.get("attempt") or {}).get("worker_job_id", "") or f"wj-{job_id}"
    command = service().repository.create_command({
        "worker_id": job["assigned_worker_id"], "command_type": "stop_test",
        "job_id": job_id, "attempt_id": job["current_attempt_id"],
        "payload": {"worker_job_id": worker_job_id},
    })
    with service().repository.connect() as conn:
        conn.execute("UPDATE cluster_jobs SET status='stopping',updated_at=? WHERE id=?",
                     (utc_now(), job_id))
    return {"success": True, "job": service().repository.get_job(job_id), "command": command}


@router.delete("/jobs/{job_id}")
def delete_job(job_id: str):
    job = service().repository.get_job(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    if job["status"] not in {"completed", "failed", "cancelled"}:
        raise HTTPException(409, "only completed history can be deleted")
    if not service().repository.delete_job(job_id):
        raise HTTPException(409, "job could not be deleted")
    return {"success": True, "deleted": job_id}


@router.post("/jobs/{job_id}/events")
def add_job_events(job_id: str, body: JobEventBatch, worker_id: str = Header(alias="X-GMS-Worker-ID"),
                   authorization: str | None = Header(default=None)):
    _authenticate(worker_id, authorization)
    job = service().repository.get_job(job_id)
    if not job or job["assigned_worker_id"] != worker_id or job["current_attempt_id"] != body.attempt_id:
        raise HTTPException(404, "job attempt not found for worker")
    inserted = service().repository.add_events(job_id, body.attempt_id,
                                               [item.model_dump() for item in body.events])
    return {"success": True, "inserted": inserted}


@router.get("/jobs/{job_id}/events")
def list_job_events(job_id: str, after: int = Query(default=-1), limit: int = Query(default=500, le=2000)):
    return {"success": True, "events": service().repository.list_events(job_id, after, limit)}


def _artifact_root() -> Path:
    return service().repository.db_path.parent / "artifacts"


@router.put("/jobs/{job_id}/artifacts/{filename}")
async def upload_artifact(job_id: str, filename: str, request: Request,
                          attempt_id: str = Query(...),
                          artifact_type: str = Query(default="file"),
                          worker_id: str = Header(alias="X-GMS-Worker-ID"),
                          authorization: str | None = Header(default=None)):
    _authenticate(worker_id, authorization)
    job = service().repository.get_job(job_id)
    if not job or job["assigned_worker_id"] != worker_id or job["current_attempt_id"] != attempt_id:
        raise HTTPException(404, "job attempt not found for worker")
    safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", Path(filename).name)
    if not safe_name:
        raise HTTPException(400, "invalid filename")
    body = await request.body()
    max_bytes = int(os.getenv("GMS_CLUSTER_ARTIFACT_MAX_BYTES", str(512 * 1024 * 1024)))
    if len(body) > max_bytes:
        raise HTTPException(413, "artifact is too large")
    destination_dir = _artifact_root() / job_id / attempt_id
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination = destination_dir / safe_name
    destination.write_bytes(body)
    artifact = service().repository.record_artifact({
        "job_id": job_id, "attempt_id": attempt_id, "worker_id": worker_id,
        "filename": safe_name, "relative_path": str(destination.relative_to(_artifact_root())),
        "artifact_type": artifact_type, "size_bytes": len(body),
        "sha256": hashlib.sha256(body).hexdigest(),
    })
    if artifact_type.startswith("report"):
        _index_cluster_report(job, destination_dir, artifact)
    return {"success": True, "artifact": artifact}


def _index_cluster_report(job: dict, result_dir: Path, artifact: dict) -> None:
    """Expose completed Worker results through the existing Reports page."""
    from features.reports import test_report_db

    request_data = job.get("request") or {}
    suite_key = job.get("suite_key") or "XTS"
    test_type = suite_key.split(":", 1)[0].upper()
    report_info = {
        "timestamp": f"cluster-{job['id']}",
        "test_type": test_type,
        "test_module": request_data.get("test_module", ""),
        "test_case": request_data.get("test_case", ""),
        "client_id": job.get("owner_id", "cluster"),
        "display_client_id": job.get("owner_id", "cluster"),
        "devices": [item["device_id"] for item in job.get("leases", [])],
        "result_dir": str(result_dir),
        "suite_path": job.get("suite_path", ""),
        "status": "completed",
        "worker_id": job.get("assigned_worker_id", ""),
        "cluster_job_id": job["id"],
        "artifact_id": artifact["id"],
    }
    test_report_db.add_report(report_info)


@router.get("/jobs/{job_id}/artifacts")
def list_artifacts(job_id: str):
    return {"success": True, "artifacts": service().repository.list_artifacts(job_id)}


@router.get("/jobs/{job_id}/artifacts/{artifact_id}/download")
def download_artifact(job_id: str, artifact_id: str):
    artifacts = [item for item in service().repository.list_artifacts(job_id)
                 if item["id"] == artifact_id]
    if not artifacts:
        raise HTTPException(404, "artifact not found")
    artifact = artifacts[0]
    path = (_artifact_root() / artifact["relative_path"]).resolve()
    if not path.is_relative_to(_artifact_root().resolve()) or not path.is_file():
        raise HTTPException(404, "artifact file not found")
    return FileResponse(path, filename=artifact["filename"])
