"""Windows USB/IP identity collection helpers."""

from __future__ import annotations

import json
import re


def query_windows_usb_identities(
    ssh_manager,
    ssh,
    vendor_ids: set[str] | None = None,
) -> dict[str, dict[str, str]]:
    """Return PnP/container/location identities keyed by VID:PID and instance."""
    ps = (
        "Get-PnpDevice -PresentOnly -Class USB | ForEach-Object { "
        "$l=(Get-PnpDeviceProperty -InstanceId $_.InstanceId "
        "-KeyName 'DEVPKEY_Device_LocationPaths' -ErrorAction SilentlyContinue).Data; "
        "$c=(Get-PnpDeviceProperty -InstanceId $_.InstanceId "
        "-KeyName 'DEVPKEY_Device_ContainerId' -ErrorAction SilentlyContinue).Data; "
        "Write-Output ($_.InstanceId + '|' + ($l -join ',') + '|' + $c) }"
    )
    try:
        stdout, _stderr, code = ssh_manager.execute_command(
            ssh, f'powershell -NoProfile -Command "{ps}"', timeout=20
        )
    except Exception:
        return {}
    if code != 0 or not stdout:
        return {}
    grouped: dict[str, list[dict[str, str]]] = {}
    by_instance: dict[str, dict[str, str]] = {}
    for raw in stdout.splitlines():
        parts = raw.strip().split("|", 2)
        instance_id = parts[0]
        location = parts[1] if len(parts) > 1 else ""
        container_id = parts[2] if len(parts) > 2 else ""
        match = re.search(
            r"USB\\VID_([0-9A-Fa-f]{4})&PID_([0-9A-Fa-f]{4})"
            r"([^\\]*)\\(.+)",
            instance_id,
        )
        if not match:
            continue
        vid, pid, interface, tail = match.groups()
        vid = vid.lower()
        if vendor_ids and vid not in vendor_ids:
            continue
        if "&MI_" in interface.upper():
            continue
        identity = {
            "usb_serial": tail.strip().split("&")[0].strip(),
            "pnp_instance_id": instance_id,
            "location_path": location.strip(),
            "container_id": container_id.strip(),
        }
        grouped.setdefault(f"{vid}:{pid.lower()}", []).append(identity)
        by_instance[f"pnp:{instance_id.casefold()}"] = identity
    unambiguous = {
        key: values[0]
        for key, values in grouped.items()
        if len({item["pnp_instance_id"] for item in values}) == 1
    }
    return {**unambiguous, **by_instance}


def query_usbipd_busid_instance_ids(ssh_manager, ssh) -> dict[str, str]:
    """Map current BUSIDs to PnP InstanceIds using usbipd's JSON state."""
    try:
        stdout, _stderr, code = ssh_manager.execute_command(
            ssh, "usbipd state", timeout=15
        )
        payload = json.loads(stdout or "{}") if code == 0 else {}
    except (TypeError, ValueError):
        return {}
    if not isinstance(payload, dict):
        return {}
    devices = next(
        (
            value
            for key, value in payload.items()
            if str(key).casefold() == "devices"
        ),
        [],
    )
    if isinstance(devices, dict):
        devices = list(devices.values())
    if not isinstance(devices, list):
        return {}
    result: dict[str, str] = {}
    for item in devices:
        if not isinstance(item, dict):
            continue
        normalized = {str(key).casefold(): value for key, value in item.items()}
        busid = str(normalized.get("busid") or "").strip()
        instance_id = str(normalized.get("instanceid") or "").strip()
        if re.fullmatch(r"[A-Za-z0-9._-]{1,64}", busid) and instance_id:
            result[busid] = instance_id
    return result
