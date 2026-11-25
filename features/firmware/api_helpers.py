"""Small request-independent helpers shared by firmware API routes."""

from __future__ import annotations

import logging
import shlex

from fastapi.responses import JSONResponse

from features.devices import (
    broadcast_device_lock_update,
    device_lock_manager,
    release_device_locks,
)
from foundation.error_model import ApiError
from foundation.security import sanitize_device_ids

from . import runtime


logger = logging.getLogger(__name__)


_REMOTE_FILE_FOUND_MARKER = "__GMS_REMOTE_FILE_FOUND__"
_REMOTE_FILE_MISSING_MARKER = "__GMS_REMOTE_FILE_MISSING__"


def adb_proxy_devices(devices: list[str]) -> list[str]:
    try:
        from worker.adb_proxy import imported_device_for_serial

        return [
            device_id for device_id in devices
            if imported_device_for_serial(device_id) is not None
        ]
    except (ImportError, OSError, RuntimeError, ValueError):
        return []


def remote_file_exists(ssh, path: str) -> bool:
    """Return whether ``path`` is a regular file on the connected host.

    A transport failure is not "missing". Callers must not fall through to a
    Controller-local file of the same path when the probe itself failed.
    """
    command = (
        f"test -f {shlex.quote(path)} && echo {_REMOTE_FILE_FOUND_MARKER} "
        f"|| echo {_REMOTE_FILE_MISSING_MARKER}"
    )
    result = runtime.ssh_manager.execute_command(ssh, command, timeout=5)
    if getattr(result, "timed_out", False):
        raise ApiError.dependency_timeout("探测远端固件文件超时")
    lines = {line.strip() for line in (result.stdout or "").splitlines()}
    if _REMOTE_FILE_FOUND_MARKER in lines:
        return True
    if _REMOTE_FILE_MISSING_MARKER in lines:
        return False
    logger.warning("remote file probe failed code=%s", result.code)
    raise ApiError.upstream_failure("探测远端固件文件失败", service="ssh")


def normalize_firmware_devices(
    values: list[str],
) -> tuple[list[str], list[str]]:
    requested = list(dict.fromkeys(
        str(value or "").strip() for value in values
        if str(value or "").strip()
    ))
    devices = sanitize_device_ids(requested)
    return devices, [value for value in requested if value not in devices]


def resolve_gsi_remote_image(
    ssh, gms_suite_dir: str, image_path: str, label: str,
) -> tuple[str | None, str | None]:
    image_path = str(image_path or "").strip()
    if not image_path:
        return "", None
    if image_path.startswith("/") or image_path.startswith("./"):
        if remote_file_exists(ssh, image_path):
            return image_path, None
        return None, f"{label} not found: {image_path}"
    remote_candidate = f"{gms_suite_dir.rstrip('/')}/{image_path.lstrip('/')}"
    if remote_file_exists(ssh, remote_candidate):
        return remote_candidate, None
    return None, f"{label} not found: {remote_candidate}"


async def lock_firmware_devices(
    *,
    client_id: str,
    username: str,
    devices: list[str],
    error_prefix: str = "Devices occupied",
):
    source_id = f"firmware:{client_id}"
    locked, records = device_lock_manager.lock_devices(
        devices,
        client_id,
        username,
        source_id=source_id,
        source_type="firmware",
        allow_existing_source=False,
    )
    if not locked:
        failed_devices = [
            {
                "device_id": item.get("serial") or item.get("device_key"),
                "error": (
                    f"设备已被 {item.get('username') or item.get('owner_id')} "
                    f"的 {item.get('source_type') or 'operation'} 占用"
                ),
            }
            for item in records
        ]
        error_message = f"{error_prefix}:\n" + "\n".join(
            f"- {item['device_id']} ({item['error']})"
            for item in failed_devices
        )
        return [], JSONResponse(
            content={
                "success": False,
                "error": error_message,
                "failed_devices": failed_devices,
            },
            status_code=409,
        )

    await broadcast_device_lock_update(devices)
    return list(dict.fromkeys(devices)), None


async def release_firmware_devices(
    client_id: str,
    devices: list[str],
) -> None:
    await release_device_locks(
        client_id,
        devices,
        source_id=f"firmware:{client_id}",
    )
