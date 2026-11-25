"""Windows USB/IP identity collection helpers."""

from __future__ import annotations

import json
import re


_USBIPD_BUSID_RE = re.compile(r"[A-Za-z0-9._-]{1,64}")
_USB_VID_PID_RE = re.compile(
    r"VID[_:]?([0-9A-Fa-f]{4}).*?PID[_:]?([0-9A-Fa-f]{4})",
    re.IGNORECASE,
)


def _usbipd_devices(payload: object) -> list[dict]:
    if not isinstance(payload, dict):
        return []
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
        return []
    return [item for item in devices if isinstance(item, dict)]


def _optional_bool(values: dict[str, object], key: str) -> bool | None:
    if key not in values:
        return None
    value = values[key]
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().casefold()
        if lowered in {"true", "1", "yes"}:
            return True
        if lowered in {"false", "0", "no", ""}:
            return False
    return bool(value)


def parse_usbipd_state(output: str) -> dict[str, dict[str, object]]:
    """Normalize connected devices from ``usbipd state`` JSON by BUSID.

    usbipd's JSON schema exposes primitive fields such as ``BusId``,
    ``InstanceId``, ``PersistedGuid`` and ``ClientIPAddress``.  Newer builds
    may additionally expose derived ``Is*`` fields.  Keep both forms usable so
    firmware automation does not depend on the human-readable ``usbipd list``
    table or a specific usbipd-win minor version.
    """
    try:
        payload = json.loads(output or "{}")
    except (TypeError, ValueError):
        return {}

    result: dict[str, dict[str, object]] = {}
    for item in _usbipd_devices(payload):
        values = {str(key).casefold(): value for key, value in item.items()}
        busid = str(values.get("busid") or "").strip()
        if not _USBIPD_BUSID_RE.fullmatch(busid):
            continue

        instance_id = str(values.get("instanceid") or "").strip()
        raw_hardware_id = values.get("hardwareid")
        if isinstance(raw_hardware_id, list):
            hardware_id = ",".join(str(value) for value in raw_hardware_id)
        else:
            hardware_id = str(raw_hardware_id or "").strip()
        identity_text = " ".join((instance_id, hardware_id))
        match = _USB_VID_PID_RE.search(identity_text)
        vid_pid = (
            f"{match.group(1).lower()}:{match.group(2).lower()}"
            if match else ""
        )

        persisted_guid = str(values.get("persistedguid") or "").strip()
        client_ip = str(values.get("clientipaddress") or "").strip()
        stub_instance_id = str(values.get("stubinstanceid") or "").strip()
        is_forced = bool(_optional_bool(values, "isforced"))
        is_connected_value = _optional_bool(values, "isconnected")
        is_bound_value = _optional_bool(values, "isbound")
        is_attached_value = _optional_bool(values, "isattached")
        is_connected = (
            is_connected_value if is_connected_value is not None else bool(busid)
        )
        is_bound = (
            is_bound_value
            if is_bound_value is not None
            else bool(persisted_guid or stub_instance_id)
        )
        is_attached = (
            is_attached_value
            if is_attached_value is not None
            else bool(client_ip)
        )
        if not is_connected:
            continue
        if is_attached:
            state = "Attached"
        elif is_bound:
            state = "Shared (forced)" if is_forced else "Shared"
        else:
            state = "Not shared"

        result[busid] = {
            "busid": busid,
            "instance_id": instance_id,
            "hardware_id": hardware_id,
            "vid_pid": vid_pid,
            "device": str(values.get("description") or "").strip(),
            "state": state,
            "is_connected": is_connected,
            "is_bound": is_bound,
            "is_attached": is_attached,
            "is_forced": is_forced,
            "client_ip": client_ip,
            "persisted_guid": persisted_guid,
        }
    return result


def query_usbipd_device_states(
    ssh_manager, ssh,
) -> dict[str, dict[str, object]]:
    """Return normalized connected USB/IP state keyed by physical BUSID."""
    try:
        result = ssh_manager.execute_command(
            ssh, "usbipd state", timeout=15
        )
    except Exception:
        return {}
    return parse_usbipd_state(result.stdout or "") if result.ok else {}


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
        result = ssh_manager.execute_command(
            ssh, f'powershell -NoProfile -Command "{ps}"', timeout=20
        )
    except Exception:
        return {}
    if not result.ok or not result.stdout:
        return {}
    grouped: dict[str, list[dict[str, str]]] = {}
    by_instance: dict[str, dict[str, str]] = {}
    for raw in result.stdout.splitlines():
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
    return {
        busid: str(state.get("instance_id") or "")
        for busid, state in query_usbipd_device_states(
            ssh_manager, ssh,
        ).items()
        if str(state.get("instance_id") or "")
    }
