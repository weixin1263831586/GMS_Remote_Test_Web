from __future__ import annotations

import os
import shlex

import scp

from worker_agent.fastboot_workflow import (
    CommandResult,
    FastbootPreparer,
    vendor_partition,
)


def upload_gsi_assets(
    *,
    ssh,
    ssh_manager,
    project_root: str,
    suite_dir: str,
) -> tuple[str | None, str | None, str | None]:
    local_script = os.path.join(project_root, "scripts", "run_GSI_Burn.sh")
    local_misc = os.path.join(project_root, "tools", "misc.img")
    if not os.path.exists(local_script):
        return None, None, f"GSI burn script not found: {local_script}"
    if not os.path.exists(local_misc):
        return None, None, f"Misc image not found: {local_misc}"

    remote_script = os.path.join(suite_dir, "run_GSI_Burn.sh")
    remote_misc = os.path.join(suite_dir, "misc.img")
    with scp.SCPClient(ssh.get_transport()) as client:
        client.put(local_script, remote_script)
        client.put(local_misc, remote_misc)
    _output, error, code = ssh_manager.execute_command(
        ssh,
        f"chmod +x {shlex.quote(remote_script)}",
    )
    if code != 0:
        return None, None, error or "Failed to make GSI runner executable"
    return remote_script, remote_misc, None


def prepare_gsi_command(
    *,
    ssh,
    ssh_manager,
    remote_script: str,
    device: str,
    system_img: str,
    misc_img: str,
    vendor_img: str,
) -> str:
    def remote_runner(argv: list[str], timeout: int) -> CommandResult:
        output, error, code = ssh_manager.execute_command(
            ssh,
            shlex.join(argv),
            timeout=timeout,
        )
        return CommandResult(output, error, code)

    prepared = FastbootPreparer(remote_runner).prepare_bootloader(device)
    argv = [
        remote_script,
        device,
        prepared.oem_argument("unlock"),
        system_img,
        misc_img,
    ]
    if vendor_img:
        argv.extend([vendor_partition(vendor_img), vendor_img])
    return shlex.join(argv)
