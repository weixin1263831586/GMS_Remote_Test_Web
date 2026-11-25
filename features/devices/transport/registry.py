"""Read-only normalized projection of all remote device transports.

Assignments remain owned by their adapters.  This module provides one stable
shape for UI, scheduling and diagnostics without creating a second mutable
source of truth.
"""

from __future__ import annotations

import re
from typing import Any


def build_transport_records(
    adb_proxy_assignments: list[dict[str, Any]] | None = None,
    usbip_assignments: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for assignment in adb_proxy_assignments or []:
        for serial in assignment.get("devices") or [""]:
            protocol_state = _health_protocol(assignment)
            records.append({
                "device_id": str(serial or ""),
                "owner_worker_id": str(assignment.get("source_worker_id") or ""),
                "consumer_worker_id": str(assignment.get("target_worker_id") or ""),
                "transport": "adb_proxy",
                "transport_state": str(assignment.get("status") or "unknown"),
                "protocol_state": protocol_state,
                "readiness": _readiness_for_state(
                    str(assignment.get("status") or "unknown"), protocol_state
                ),
                "generation": int(assignment.get("generation") or 0),
                "operation_id": str(assignment.get("operation_id") or ""),
                "source_identity": {
                    "host": str(assignment.get("source_address") or ""),
                    "busid": "",
                    "usb_serial": str(serial or ""),
                },
            })
    for assignment in usbip_assignments or []:
        serials = assignment.get("device_serials") or [""]
        for serial in serials:
            state = str(assignment.get("status") or "unknown")
            protocol_state = str(assignment.get("protocol_state") or "unknown")
            records.append({
                "device_id": str(serial or ""),
                "owner_worker_id": str(assignment.get("device_host") or ""),
                "consumer_worker_id": str(assignment.get("worker_id") or ""),
                "transport": "usbip",
                "transport_state": state,
                "protocol_state": protocol_state,
                "readiness": _readiness_for_state(state, protocol_state),
                "generation": int(assignment.get("generation") or 0),
                "operation_id": str(assignment.get("operation_id") or ""),
                "source_identity": {
                    "host": str(assignment.get("source_host") or ""),
                    "busid": str(assignment.get("busid") or ""),
                    "usb_serial": str(serial or ""),
                },
            })
    return records


def _health_protocol(assignment: dict[str, Any]) -> str:
    health = assignment.get("health") or {}
    target = health.get("target") or {}
    return str(target.get("protocol_state") or "unknown")


def _readiness_for_state(state: str, protocol_state: str = "unknown") -> str:
    if state in {"connected", "attached"}:
        return (
            "test_ready"
            if protocol_state in {"adb", "fastboot", "recovery"}
            else "transport_ready"
        )
    if state in {"connecting", "attaching", "recovering"}:
        return "transport_pending"
    if state in {"degraded_source", "degraded_target", "device_missing", "unknown"}:
        return "degraded"
    return "not_ready"


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
