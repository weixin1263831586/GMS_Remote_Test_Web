"""Stable physical-device identity independent from transient USB bus ids."""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class PhysicalDeviceIdentity:
    physical_device_id: str
    identity_source: str
    identity_stable: bool
    logical_android_serial: str
    usb_serial: str
    container_id: str
    pnp_instance_id: str
    location_path: str
    current_usb_busid: str
    current_protocol: str
    source_host: str

    def to_dict(self) -> dict[str, str | bool]:
        return asdict(self)


def resolve_physical_device_identity(
    *,
    source_host: str,
    current_usb_busid: str,
    logical_android_serial: str = "",
    usb_serial: str = "",
    container_id: str = "",
    pnp_instance_id: str = "",
    location_path: str = "",
    vid_pid: str = "",
    current_protocol: str = "usb",
) -> PhysicalDeviceIdentity:
    """Resolve identity using stable evidence before the transient BUSID.

    Android/USB serials and ContainerId survive hub re-enumeration.  PnP IDs
    are host-scoped, while location and BUSID are explicitly marked as less
    stable fallbacks.  The opaque ID avoids exposing a raw hardware serial as
    an internal database/API identifier.
    """
    candidates = (
        ("android_serial", logical_android_serial, True, False),
        ("usb_serial", usb_serial, True, False),
        ("container_id", container_id, True, False),
        ("pnp_instance_id", pnp_instance_id, True, True),
        (
            "location_path_vid",
            f"{location_path}|{vid_pid}" if location_path and vid_pid else "",
            False,
            True,
        ),
        ("usb_busid", current_usb_busid, False, True),
    )
    identity_source = "unknown"
    identity_value = ""
    identity_stable = False
    host_scoped = True
    for source, value, stable, scoped in candidates:
        normalized = str(value or "").strip()
        if normalized:
            identity_source = source
            identity_value = normalized
            identity_stable = stable
            host_scoped = scoped
            break

    canonical = f"{identity_source}:{identity_value.casefold()}"
    if host_scoped:
        canonical = f"{str(source_host).strip().casefold()}|{canonical}"
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]
    return PhysicalDeviceIdentity(
        physical_device_id=f"physical-{digest}",
        identity_source=identity_source,
        identity_stable=identity_stable,
        logical_android_serial=str(logical_android_serial or "").strip(),
        usb_serial=str(usb_serial or "").strip(),
        container_id=str(container_id or "").strip(),
        pnp_instance_id=str(pnp_instance_id or "").strip(),
        location_path=str(location_path or "").strip(),
        current_usb_busid=str(current_usb_busid or "").strip(),
        current_protocol=str(current_protocol or "unknown").strip(),
        source_host=str(source_host or "").strip(),
    )
