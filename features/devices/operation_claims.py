"""Atomic fencing and immutable audit for direct device mutations."""

from __future__ import annotations

import uuid

from fastapi.responses import JSONResponse

from features.auth import require_authenticated_user
from foundation.security import sanitize_device_ids

from .locks import device_lock_manager


def acquire_device_operation_claim(
    request,
    device_ids: list[str],
    operation: str,
    *,
    ttl_seconds: int = 3600,
) -> tuple[str, list[dict], JSONResponse | None]:
    """Atomically fence a dynamic set of devices for one HTTP operation."""

    user = require_authenticated_user(request)
    requested = list(dict.fromkeys(
        str(device_id or "").strip()
        for device_id in device_ids
        if str(device_id or "").strip()
    ))
    devices = list(dict.fromkeys(sanitize_device_ids(requested)))
    if len(devices) != len(requested):
        return "", [], JSONResponse(
            content={"success": False, "error": "Invalid device serial"},
            status_code=400,
        )
    if not devices:
        return "", [], None
    source_id = f"operation:{operation}:{uuid.uuid4().hex}"
    acquired, records = device_lock_manager.lock_devices(
        devices,
        user.id,
        user.username,
        source_id=source_id,
        source_type=f"local-{operation}",
        ttl_seconds=ttl_seconds,
        allow_existing_source=False,
    )
    if acquired:
        request.state.device_lease_tokens = [
            {
                "lease_id": row["id"],
                "device_id": row["device_key"],
                "generation": row["generation"],
                "owner_id": user.id,
            }
            for row in records
        ]
        return source_id, records, None
    conflicts = [
        {
            "device_id": row.get("serial", ""),
            "source_type": row.get("source_type", "operation"),
        }
        for row in records
    ]
    return "", records, JSONResponse(
        content={
            "success": False,
            "error": "Device is reserved by an active operation",
            "conflicts": conflicts,
        },
        status_code=409,
    )


def release_device_operation_claim(source_id: str) -> int:
    if not source_id:
        return 0
    return device_lock_manager.registry.release(source_id)


def audit_device_operation(
    request,
    operation: str,
    records: list[dict],
    status_code: int,
    *,
    error: str = "",
) -> None:
    """Write an immutable, fenced device-mutation audit event."""
    from foundation.security_audit import security_audit_logger

    user = require_authenticated_user(request)
    security_audit_logger.log_event({
        "action_type": "device_mutation",
        "source": "web",
        "operation": operation,
        "method": getattr(request, "method", ""),
        "path": str(getattr(getattr(request, "url", None), "path", "")),
        "status_code": int(status_code),
        "owner_id": user.id,
        "username": user.username,
        "leases": [
            {
                "lease_id": row.get("id", ""),
                "device_id": row.get("device_key", ""),
                "generation": row.get("generation", 0),
                "owner_id": row.get("owner_id", ""),
            }
            for row in records
        ],
        "error": error,
    })


__all__ = [
    "acquire_device_operation_claim",
    "audit_device_operation",
    "release_device_operation_claim",
]
