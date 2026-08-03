"""Read-only normalized projection of all remote device transports.

Assignments remain owned by their adapters.  This module provides one stable
shape for UI, scheduling and diagnostics without creating a second mutable
source of truth.
"""

from __future__ import annotations

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
