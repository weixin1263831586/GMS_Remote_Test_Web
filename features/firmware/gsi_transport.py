from __future__ import annotations

import logging
import os
import shlex
from collections.abc import Callable

import scp

from foundation.error_model import ApiError, record_internal_error
from worker_agent.fastboot_workflow import (
    CommandResult,
    FastbootPreparer,
    vendor_partition,
)


logger = logging.getLogger(__name__)


def upload_gsi_assets(
    *,
    ssh,
    ssh_manager,
    project_root: str,
    suite_dir: str,
) -> tuple[str, str]:
    """Upload run_GSI_Burn.sh + misc.img to the test host.

    Failures raise typed ApiError so the route maps them by semantics:
    missing local assets are a deployment dependency problem (503), SCP and
    chmod failures are remote-infrastructure failures (502).
    """
    local_script = os.path.join(project_root, "scripts", "run_GSI_Burn.sh")
    local_misc = os.path.join(project_root, "tools", "misc.img")
    if not os.path.exists(local_script):
        raise ApiError.dependency_unavailable(
            f"GSI burn script not found: {local_script}"
        )
    if not os.path.exists(local_misc):
        raise ApiError.dependency_unavailable(
            f"Misc image not found: {local_misc}"
        )

    remote_script = os.path.join(suite_dir, "run_GSI_Burn.sh")
    remote_misc = os.path.join(suite_dir, "misc.img")
    try:
        with scp.SCPClient(ssh.get_transport()) as client:
            client.put(local_script, remote_script)
            client.put(local_misc, remote_misc)
    except Exception as exc:
        message = record_internal_error(logger, "GSI asset upload", "GSI asset upload failed")
        raise ApiError.upstream_failure(
            message, service="scp"
        ) from exc
    chmod_result = ssh_manager.execute_command(
        ssh,
        f"chmod +x {shlex.quote(remote_script)}",
    )
    if not chmod_result.ok:
        raise ApiError.upstream_failure(
            chmod_result.stderr or "Failed to make GSI runner executable",
            service="ssh",
        )
    return remote_script, remote_misc


def prepare_gsi_command(
    *,
    ssh,
    ssh_manager,
    remote_script: str,
    device: str,
    system_img: str,
    misc_img: str,
    vendor_img: str,
    on_transport_reset: Callable[[str, str], None] | None = None,
) -> str:
    def remote_runner(argv: list[str], timeout: int) -> CommandResult:
        return ssh_manager.execute_command(
            ssh,
            shlex.join(argv),
            timeout=timeout,
        )

    prepared = FastbootPreparer(
        remote_runner,
        on_transport_reset=on_transport_reset,
    ).prepare_gsi_fastbootd(device)
    argv = [
        "env",
        "GMS_GSI_DEFER_REBOOT=1",
        remote_script,
        device,
        prepared.oem_argument("unlock"),
        system_img,
        misc_img,
    ]
    if vendor_img:
        argv.extend([vendor_partition(vendor_img), vendor_img])
    return shlex.join(argv)
