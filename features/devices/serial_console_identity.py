"""Stable USB-serial identity helpers.

Kernel tty names are allocation order, not hardware identity.  Persistent
bindings therefore prefer udev's by-id symlink, then a physical USB path.  A
bare tty name remains usable for the current process but is explicitly marked
unstable so callers never silently restore a binding onto different hardware.
"""

from __future__ import annotations

import hashlib
from pathlib import Path


def serial_port_identity(
    *, by_id: str, devname: str, usb_path: str
) -> dict[str, str | bool]:
    if by_id:
        return {
            "port_key": Path(by_id).name,
            "identity_source": "by-id",
            "identity_stable": True,
        }
    if usb_path:
        digest = hashlib.sha256(usb_path.encode("utf-8")).hexdigest()[:20]
        return {
            "port_key": f"usb-path-{digest}",
            "identity_source": "usb-path",
            "identity_stable": True,
        }
    return {
        "port_key": Path(devname).name,
        "identity_source": "kernel-node",
        "identity_stable": False,
    }


__all__ = ["serial_port_identity"]
