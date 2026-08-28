"""Small request-independent helpers shared by firmware API routes."""

from __future__ import annotations

import shlex

from foundation.security import sanitize_device_ids

from . import runtime


_REMOTE_FILE_FOUND_MARKER = "__GMS_REMOTE_FILE_FOUND__"
_REMOTE_FILE_MISSING_MARKER = "__GMS_REMOTE_FILE_MISSING__"


def adb_proxy_devices(devices: list[str]) -> list[str]:
    try:
        from worker_agent.adb_proxy import imported_device_for_serial

        return [
            device_id for device_id in devices
            if imported_device_for_serial(device_id) is not None
        ]
    except (ImportError, OSError, RuntimeError, ValueError):
        return []


def remote_file_exists(ssh, path: str) -> bool:
    command = (
        f"test -f {shlex.quote(path)} && echo {_REMOTE_FILE_FOUND_MARKER} "
        f"|| echo {_REMOTE_FILE_MISSING_MARKER}"
    )
    output, _, _ = runtime.ssh_manager.execute_command(ssh, command, timeout=5)
    return _REMOTE_FILE_FOUND_MARKER in {
        line.strip() for line in output.splitlines()
    }


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
