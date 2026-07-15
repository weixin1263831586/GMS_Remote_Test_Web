"""Remote Worker deployment endpoint."""

from __future__ import annotations

import asyncio
import re
import shlex
import tarfile
import tempfile
from pathlib import Path

import paramiko
from fastapi import APIRouter, HTTPException

from foundation.config import config_manager

from .api import service
from .repository import utc_now
from .worker_auth import persist_worker_token


router = APIRouter()


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
    if (
        "@" not in connection
        or not token
        or any(character in token for character in (",", "\r", "\n"))
        or not controller_url.startswith(("http://", "https://"))
    ):
        raise HTTPException(
            400,
            "SSH host, Worker Token and Controller URL are required",
        )
    username, hostname = connection.split("@", 1)

    def _deploy() -> dict:
        from features.system import ssh_manager

        try:
            ssh = ssh_manager.create_connection(
                {
                    "host": hostname,
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
                f"cannot connect to {hostname}:22: {exc}"
            ) from exc
        except paramiko.SSHException as exc:
            raise RuntimeError(
                f"SSH negotiation failed for {username}@{hostname}: {exc}"
            ) from exc
        if ssh is None:
            raise RuntimeError("SSH connection failed")
        archive_path = None
        try:
            # The same credential is required by the host-terminal workspace.
            # Persist it only after SSH authentication has actually succeeded.
            if password and not config_manager.upsert_device_host_password(
                connection, password
            ):
                raise RuntimeError(
                    "SSH connected, but saving the host credential failed"
                )
            persist_worker_token(worker_id, token)
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
                    "gts-rockchip.json",
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
                bundle.add(host_tools, arcname="tools/GMS-Host-Tools")
            sftp = ssh.open_sftp()
            try:
                sftp.put(str(archive_path), "/tmp/gms-worker-setup.tar.gz")
            finally:
                sftp.close()
            install = (
                "rm -rf ~/gms-worker-setup && mkdir -p ~/gms-worker-setup && "
                "tar -xzf /tmp/gms-worker-setup.tar.gz -C ~/gms-worker-setup && "
                "cd ~/gms-worker-setup && "
                f"bash scripts/install_cluster_worker.sh {shlex.quote(worker_id)} "
                f"{shlex.quote(controller_url)} {shlex.quote(token)} - "
                f"{shlex.quote(suite_root)} {shlex.quote(hostname)}"
            )
            if password:
                install = "sudo -S -p '' -v && " + install
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
