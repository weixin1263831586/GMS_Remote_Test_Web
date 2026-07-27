"""Remote Worker deployment endpoint."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import stat
import subprocess
import tarfile
import tempfile
import threading
import uuid
from pathlib import Path

import paramiko
from fastapi import APIRouter, Depends, HTTPException

from features.auth import (
    CurrentUser,
    require_elevated_admin_when_auth_required,
    require_role_when_auth_required,
)
from foundation.config import config_manager
from foundation.networking import split_host_port
from foundation.ssh_security import (
    scan_ssh_host_keys,
    trust_scanned_ssh_host_keys,
)

from .api import service
from .repository import utc_now
from .worker_auth import persist_worker_token, worker_tokens, write_worker_tokens


router = APIRouter()
_LOCAL_SOFTWARE_LOCK = threading.Lock()
_LOCAL_SOFTWARE_TASKS: dict[str, dict] = {}
_LOCAL_SOFTWARE_ACTIVE_TASK = ""


def _local_software_service_name() -> str:
    base = os.getenv("GMS_SERVICE_NAME", "gms-web-app").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.@-]+", base):
        raise RuntimeError("invalid GMS service name")
    return f"{base}-local-software.service"


def _reconfigure_local_software() -> str:
    unit = _local_software_service_name()
    systemctl = "/usr/bin/systemctl"
    if not Path(systemctl).is_file():
        systemctl = "/bin/systemctl"
    unit_state = subprocess.run(
        [systemctl, "show", unit, "--property=LoadState", "--value"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if unit_state.returncode == 0 and unit_state.stdout.strip() == "loaded":
        command = ["sudo", systemctl, "start", unit]
    else:
        project_root = Path(__file__).resolve().parents[2]
        script = project_root / "scripts/configure_local_worker_software.sh"
        if not script.is_file():
            raise RuntimeError(f"local Software script is missing: {script}")
        command = ["/bin/bash", str(script), str(project_root), str(Path.home())]
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=900,
        check=False,
    )
    output = (completed.stdout + completed.stderr).strip()
    if completed.returncode:
        raise RuntimeError(output or f"Software setup exited with {completed.returncode}")
    return output


def _local_worker_has_active_tests(worker: dict) -> bool:
    if int(worker.get("running_jobs") or 0) > 0:
        return True
    from worker_agent.process_inventory import discover_tradefed_processes

    return bool(discover_tradefed_processes())


def _run_local_software_task(task_id: str) -> None:
    global _LOCAL_SOFTWARE_ACTIVE_TASK
    with _LOCAL_SOFTWARE_LOCK:
        task = _LOCAL_SOFTWARE_TASKS[task_id]
        task.update(status="running", started_at=utc_now())
    try:
        output = _reconfigure_local_software()
    except Exception as exc:
        with _LOCAL_SOFTWARE_LOCK:
            task.update(status="failed", error=str(exc), finished_at=utc_now())
    else:
        with _LOCAL_SOFTWARE_LOCK:
            task.update(status="completed", output=output, finished_at=utc_now())
    finally:
        with _LOCAL_SOFTWARE_LOCK:
            if task_id == _LOCAL_SOFTWARE_ACTIVE_TASK:
                _LOCAL_SOFTWARE_ACTIVE_TASK = ""


@router.post("/workers/local/software/reconfigure")
async def reconfigure_local_worker_software(
    _admin: CurrentUser | None = Depends(require_role_when_auth_required("admin")),
):
    """Reinstall bundled tools used by the Controller Local Worker."""
    svc = service()
    worker = svc.repository.get_worker(svc.config.local_worker_id) or {}
    if _local_worker_has_active_tests(worker):
        raise HTTPException(409, "Local Worker has running jobs")
    global _LOCAL_SOFTWARE_ACTIVE_TASK
    with _LOCAL_SOFTWARE_LOCK:
        if _LOCAL_SOFTWARE_ACTIVE_TASK:
            task = dict(_LOCAL_SOFTWARE_TASKS[_LOCAL_SOFTWARE_ACTIVE_TASK])
            return {"success": True, "already_running": True, "task": task}
        task_id = f"local-software-{uuid.uuid4().hex}"
        task = {
            "id": task_id,
            "worker_id": svc.config.local_worker_id,
            "status": "queued",
            "created_at": utc_now(),
            "started_at": "",
            "finished_at": "",
            "output": "",
            "error": "",
        }
        _LOCAL_SOFTWARE_TASKS[task_id] = task
        _LOCAL_SOFTWARE_ACTIVE_TASK = task_id
    threading.Thread(
        target=_run_local_software_task,
        args=(task_id,),
        name="LocalSoftwareReconfigure",
        daemon=True,
    ).start()
    return {"success": True, "accepted": True, "task": dict(task)}


@router.get("/workers/local/software/reconfigure/{task_id}")
def local_worker_software_reconfiguration_status(
    task_id: str,
    _admin: CurrentUser | None = Depends(require_role_when_auth_required("admin")),
):
    with _LOCAL_SOFTWARE_LOCK:
        task = _LOCAL_SOFTWARE_TASKS.get(task_id)
        if task is None:
            raise HTTPException(404, "local Software task not found")
        return {"success": True, "task": dict(task)}


def _validate_gts_credential(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise ValueError("GMS_GTS_CREDENTIAL_FILE must reference a file")
    if stat.S_IMODE(resolved.stat().st_mode) & 0o077:
        raise ValueError("GTS credential file permissions must be 0600")
    try:
        payload = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("GTS credential must be valid JSON") from exc
    private_key = str(payload.get("private_key") or "")
    if (
        payload.get("type") != "service_account"
        or not str(payload.get("client_email") or "").strip()
        or "-----BEGIN PRIVATE KEY-----" not in private_key
        or "-----END PRIVATE KEY-----" not in private_key
    ):
        raise ValueError("GTS credential must be a service-account document")
    return resolved


def _resolve_gts_credential(project_root: Path | None = None) -> Path:
    """Resolve the GTS service-account file, falling back to the bundled copy.

    Operators normally point GMS_GTS_CREDENTIAL_FILE at a repository-external
    0600 JSON. When that variable is unset, fall back to the bundled
    tools/GMS-Host-Tools/gts-rockchip.json so single-host deployments work out
    of the box. The bundled file is never modified by deployments and is
    already written mode 0600 by the release packaging step.
    """

    configured = os.getenv("GMS_GTS_CREDENTIAL_FILE", "").strip()
    if configured:
        return _validate_gts_credential(Path(configured))
    root = project_root or Path(__file__).resolve().parents[2]
    bundled = root / "tools" / "GMS-Host-Tools" / "gts-rockchip.json"
    if not bundled.is_file():
        raise ValueError(
            "GMS_GTS_CREDENTIAL_FILE is not set and no bundled credential exists"
        )
    return _validate_gts_credential(bundled)


def _deployment_host(connection: str) -> tuple[str, str, int]:
    if "@" not in connection:
        raise HTTPException(400, "SSH host must use user@host format")
    username, raw_host = connection.split("@", 1)
    hostname, port = split_host_port(raw_host)
    if (
        not username
        or not re.fullmatch(r"[A-Za-z0-9._-]+", username)
        or not hostname
        or not (1 <= port <= 65535)
    ):
        raise HTTPException(400, "invalid SSH host")
    return username, hostname, port


@router.post("/workers/ssh-host-key/scan")
async def scan_worker_ssh_host_key(
    body: dict,
    _admin: CurrentUser | None = Depends(
        require_elevated_admin_when_auth_required
    ),
):
    """Return untrusted fingerprints for explicit administrator verification."""
    connection = str(body.get("ssh_host") or "").strip()
    _username, hostname, port = _deployment_host(connection)
    try:
        keys = await asyncio.to_thread(scan_ssh_host_keys, hostname, port)
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(502, str(exc)) from exc
    return {"success": True, "host": hostname, "port": port, "keys": keys}


@router.post("/workers/ssh-host-key/trust")
async def trust_worker_ssh_host_key(
    body: dict,
    _admin: CurrentUser | None = Depends(
        require_elevated_admin_when_auth_required
    ),
):
    """Persist only fingerprints that match a fresh server-side key scan."""
    connection = str(body.get("ssh_host") or "").strip()
    _username, hostname, port = _deployment_host(connection)
    keys = body.get("keys")
    if not isinstance(keys, list) or not keys:
        raise HTTPException(400, "verified SSH host keys are required")
    try:
        trusted = await asyncio.to_thread(
            trust_scanned_ssh_host_keys,
            hostname,
            port,
            keys,
            replace=bool(body.get("replace", False)),
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(502, str(exc)) from exc
    return {"success": True, "host": hostname, "port": port, "keys": trusted}


@router.post("/workers/deploy")
async def deploy_worker(
    body: dict,
    _admin: CurrentUser | None = Depends(
        require_elevated_admin_when_auth_required
    ),
):
    """Upload and install a Worker using explicitly supplied SSH credentials."""
    worker_id = str(body.get("worker_id") or "").strip()
    connection = str(body.get("ssh_host") or "").strip()
    password = str(body.get("password") or "")
    token = str(body.get("token") or "").strip()
    controller_url = str(body.get("controller_url") or "").strip().rstrip("/")
    suite_root = str(body.get("suite_root") or "~/GMS-Suite").strip()
    save_password = body.get("save_password") is True
    if not re.fullmatch(r"[A-Za-z0-9._-]+", worker_id):
        raise HTTPException(400, "invalid worker ID")
    if (
        not re.fullmatch(r"[A-Za-z0-9._~-]{8,256}", token)
        or not controller_url.startswith("https://")
    ):
        raise HTTPException(
            400,
            "SSH host, Worker Token and Controller URL are required",
        )
    username, hostname, ssh_port = _deployment_host(connection)
    try:
        gts_credential = _resolve_gts_credential()
    except ValueError as exc:
        raise HTTPException(503, str(exc)) from exc

    def _deploy() -> dict:
        from features.system import ssh_manager

        try:
            ssh = ssh_manager.create_connection(
                {
                    "host": hostname,
                    "port": ssh_port,
                    "username": username,
                    "password": password,
                },
                raise_on_error=True,
            )
        except paramiko.AuthenticationException as exc:
            raise RuntimeError(
                f"SSH authentication failed for {username}@{hostname}; "
                "please verify the SSH username and password"
            ) from exc
        except (TimeoutError, OSError) as exc:
            raise RuntimeError(
                f"cannot connect to {hostname}:{ssh_port}: {exc}"
            ) from exc
        except paramiko.SSHException as exc:
            raise RuntimeError(
                f"SSH negotiation failed for {username}@{hostname}: {exc}"
            ) from exc
        if ssh is None:
            raise RuntimeError("SSH connection failed")
        archive_path = None
        try:
            if save_password and password and not config_manager.upsert_device_host_password(
                connection, password
            ):
                raise RuntimeError("saving the host credential failed")
            project_root = Path(__file__).resolve().parents[2]
            with tempfile.NamedTemporaryFile(
                suffix=".tar.gz", delete=False
            ) as temporary:
                archive_path = Path(temporary.name)
            with tarfile.open(archive_path, "w:gz") as bundle:
                bundle.add(project_root / "worker_agent", arcname="worker_agent")
                bundle.add(
                    project_root / "scripts/install_cluster_worker.sh",
                    arcname="scripts/install_cluster_worker.sh",
                )
                bundle.add(
                    project_root / "scripts/gms_worker_usbip.sh",
                    arcname="scripts/gms_worker_usbip.sh",
                )
                bundle.add(
                    project_root / "scripts/configure_gms_host_tools.py",
                    arcname="scripts/configure_gms_host_tools.py",
                )
                bundle.add(
                    project_root / "scripts/extract_zip_preserve_mode.py",
                    arcname="scripts/extract_zip_preserve_mode.py",
                )
                bundle.add(
                    project_root / "scripts/run_GSI_Burn.sh",
                    arcname="scripts/run_GSI_Burn.sh",
                )
                bundle.add(
                    project_root / "scripts/run_GMS_Test_Auto.sh",
                    arcname="scripts/run_GMS_Test_Auto.sh",
                )
                bundle.add(
                    project_root / "tools/upgrade_tool",
                    arcname="tools/upgrade_tool",
                )
                bundle.add(
                    project_root / "tools/scrcpy-linux-x86_64-v3.3.4",
                    arcname="tools/scrcpy-linux-x86_64-v3.3.4",
                )
                host_tools = project_root / "tools/GMS-Host-Tools"
                required_host_tools = (
                    "platform-tools-gms-linux.zip",
                    "env.sh",
                    "verify.sh",
                    "README.md",
                )
                missing = [
                    name for name in required_host_tools
                    if not (host_tools / name).is_file()
                ]
                if missing:
                    raise RuntimeError(
                        "GMS Host Tools bundle is incomplete: "
                        + ", ".join(missing)
                    )
                jdk_root = host_tools / "jdk-11"
                module_parts = sorted((jdk_root / "lib").glob("modules.part.*"))
                if not (jdk_root / "bin/java").is_file() or not module_parts:
                    raise RuntimeError(
                        "GMS Host Tools bundle is incomplete: jdk-11 directory "
                        "or lib/modules.part.*"
                    )
                for name in required_host_tools:
                    bundle.add(
                        host_tools / name,
                        arcname=f"tools/GMS-Host-Tools/{name}",
                    )
                bundle.add(
                    jdk_root,
                    arcname="tools/GMS-Host-Tools/jdk-11",
                )
                controller_certificate = Path(
                    os.getenv(
                        "GMS_CERT_CRT",
                        str(project_root / "configs/certs/gms-local.crt"),
                    )
                )
                if controller_certificate.is_file():
                    bundle.add(
                        controller_certificate,
                        arcname="controller-ca.crt",
                    )
            sftp = ssh.open_sftp()
            remote_archive = "/tmp/gms-worker-setup.tar.gz"
            remote_credential = f"/tmp/gms-worker-gts-{worker_id}.json"
            try:
                sftp.put(str(archive_path), remote_archive)
                sftp.put(str(gts_credential), remote_credential)
                sftp.chmod(remote_credential, 0o600)
            finally:
                sftp.close()
            controller_ca_arg = (
                "controller-ca.crt"
                if controller_certificate.is_file()
                else "-"
            )
            install = (
                "set -e; "
                f"cleanup() {{ rm -f {shlex.quote(remote_archive)} "
                f"{shlex.quote(remote_credential)}; }}; trap cleanup EXIT; "
                "rm -rf ~/gms-worker-setup && mkdir -p ~/gms-worker-setup && "
                f"tar -xzf {shlex.quote(remote_archive)} -C ~/gms-worker-setup && "
                "cd ~/gms-worker-setup && "
                f"bash scripts/install_cluster_worker.sh {shlex.quote(worker_id)} "
                f"{shlex.quote(controller_url)} {shlex.quote(token)} "
                f"{shlex.quote(controller_ca_arg)} "
                f"{shlex.quote(suite_root)} {shlex.quote(hostname)} "
                f"{shlex.quote(remote_credential)}"
            )
            if password:
                install = "sudo -S -p '' -v && " + install
            previous_tokens = worker_tokens()
            persist_worker_token(worker_id, token)
            try:
                stdin, stdout, stderr = ssh.exec_command(
                    install, timeout=900, get_pty=True
                )
                if password:
                    stdin.write(password + "\n")
                    stdin.flush()
                exit_code = stdout.channel.recv_exit_status()
                output = (stdout.read() + stderr.read()).decode(
                    "utf-8", errors="replace"
                )[-12000:]
                if exit_code != 0:
                    raise RuntimeError(output or f"installer exited with {exit_code}")
            except Exception:
                write_worker_tokens(previous_tokens)
                raise
            return {"success": True, "worker_id": worker_id, "output": output}
        finally:
            ssh.close()
            if archive_path:
                archive_path.unlink(missing_ok=True)

    try:
        result = await asyncio.to_thread(_deploy)
        # An installer exiting successfully only proves that files and units
        # were created. An old row with the same Worker ID is not proof that
        # this deployment started; require a registration/heartbeat newer than
        # the completed installation.
        installed_at = utc_now()
        timeout = service().config.worker_registration_timeout_seconds
        for _ in range(timeout):
            worker = service().repository.get_worker(worker_id)
            heartbeat_at = str((worker or {}).get("last_heartbeat_at") or "")
            if heartbeat_at > installed_at:
                return {
                    **result,
                    "registered": True,
                    "last_heartbeat_at": heartbeat_at,
                }
            await asyncio.sleep(1)
        raise HTTPException(
            502,
            f"安装脚本已完成，但 Worker 在 {timeout} 秒内未产生新心跳。请在目标主机执行 "
            "systemctl --user status gms-worker-agent 和 "
            "journalctl --user -u gms-worker-agent -n 50 --no-pager",
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502, f"automatic deployment failed: {exc}") from exc
