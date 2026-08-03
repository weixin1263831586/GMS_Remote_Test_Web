"""Central transport compatibility policy for test and device workflows."""

from __future__ import annotations

import re
from typing import Any


_PHYSICAL_USB_PATTERNS = tuple(re.compile(pattern, re.IGNORECASE) for pattern in (
    r"(?:^|[^a-z])fastboot(?:[^a-z]|$)",
    r"(?:^|[^a-z])flash(?:ing)?(?:[^a-z]|$)",
    r"(?:^|[^a-z])sideload(?:[^a-z]|$)",
    r"cts.*usb",
    r"usb.*(?:accessory|host|device|descriptor|role|audio|camera|hid|mtp|ptp)",
))


def test_transport_requirement(
    argv: list[str],
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Classify a test command without coupling the policy to HTTP or Worker."""
    explicit = str((env or {}).get("GMS_TRANSPORT_REQUIREMENT") or "").lower()
    command = " ".join(str(item or "") for item in argv)
    physical = explicit in {"physical_usb", "usbip", "local_usb"} or any(
        pattern.search(command) for pattern in _PHYSICAL_USB_PATTERNS
    )
    return {
        "requirement": "physical_usb" if physical else "adb",
        "allowed_transports": (
            ["local_usb", "usbip"]
            if physical else ["local_usb", "usbip", "adb_proxy"]
        ),
        "reason": (
            "该命令包含Fastboot、刷写或真实USB总线相关操作"
            if physical else "该命令只要求ADB测试通道"
        ),
    }


def incompatible_test_devices(
    devices: list[dict[str, Any]],
    argv: list[str],
    env: dict[str, str] | None = None,
) -> tuple[list[str], dict[str, Any]]:
    policy = test_transport_requirement(argv, env)
    allowed = set(policy["allowed_transports"])
    unsupported = [
        str(item.get("serial") or item.get("id") or "")
        for item in devices
        if str(item.get("transport") or "local_usb").lower() not in allowed
    ]
    return [item for item in unsupported if item], policy
