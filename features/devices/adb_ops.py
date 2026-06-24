from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass


AdbRunner = Callable[[str | None, str, int], tuple[str, int]]


@dataclass
class RemountResult:
    success: bool
    root_output: str = ''
    remount_output: str = ''
    needs_reboot: bool = False
    overlayfs_enabled: bool = False
    code: int = 0


@dataclass
class RebootResult:
    success: bool
    output: str = ''
    back_online: bool = False
    wait_time: float = 0.0


def remount_needs_reboot(output: str) -> bool:
    return 'Now reboot your device' in output


def remount_overlayfs_enabled(output: str) -> bool:
    return 'Overlayfs enabled' in output or 'overlayfs' in output.lower()


def remount_succeeded(output: str, code: int) -> bool:
    return (
        code == 0
        or 'Remount succeeded' in output
        or 'remount succeeded' in output.lower()
        or 'already' in output.lower()
    )


def root_and_remount(
    run_adb: AdbRunner,
    device_id: str | None,
    partition: str | None = None,
    *,
    root_timeout: int = 15,
    remount_timeout: int = 20,
    settle_seconds: float = 2.0,
) -> RemountResult:
    root_output, _root_code = run_adb(device_id, 'root', root_timeout)
    if settle_seconds > 0:
        time.sleep(settle_seconds)

    remount_args = f'remount {partition}' if partition else 'remount'
    remount_output, remount_code = run_adb(device_id, remount_args, remount_timeout)
    overlayfs_enabled = remount_overlayfs_enabled(remount_output)
    needs_reboot = remount_needs_reboot(remount_output)
    if overlayfs_enabled:
        needs_reboot = False
    return RemountResult(
        success=remount_succeeded(remount_output, remount_code),
        root_output=root_output,
        remount_output=remount_output,
        needs_reboot=needs_reboot,
        overlayfs_enabled=overlayfs_enabled,
        code=remount_code,
    )


def reboot_with_runner(
    run_adb: AdbRunner,
    device_id: str | None,
    *,
    wait_for_online: bool = False,
    wait_timeout: float = 60.0,
    poll_interval: float = 2.0,
) -> RebootResult:
    output, code = run_adb(device_id, 'reboot', 5)
    if code != 0:
        return RebootResult(False, output=output)
    if not wait_for_online:
        return RebootResult(True, output=output)

    start_time = time.time()
    while time.time() - start_time < wait_timeout:
        state_output, _state_code = run_adb(device_id, 'get-state', 10)
        if 'device' in state_output.lower():
            return RebootResult(
                True,
                output=output,
                back_online=True,
                wait_time=round(time.time() - start_time, 1),
            )
        time.sleep(poll_interval)
    return RebootResult(
        True,
        output=output,
        back_online=False,
        wait_time=round(wait_timeout, 1),
    )


def mount_point_is_rw(mount_output: str, mount_point: str) -> bool | None:
    escaped_mount = re.escape(mount_point)
    pattern = re.compile(rf'\son\s+{escaped_mount}\s+type\s+\S+\s+\(([^)]*)\)')
    for line in mount_output.splitlines():
        match = pattern.search(line)
        if not match:
            continue
        options = {part.strip() for part in match.group(1).split(',')}
        if 'rw' in options:
            return True
        if 'ro' in options:
            return False
    return None
