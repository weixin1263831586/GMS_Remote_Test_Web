"""Atomic fencing and immutable audit for direct device mutations."""

from __future__ import annotations

import uuid

from fastapi.responses import JSONResponse

from features.auth import require_authenticated_user
from foundation.security import sanitize_device_ids

from .locks import device_lock_manager


def _owned_local_device_keys(owner_id: str, device_keys: list[str]) -> dict[str, dict]:
    """Return the caller's active claims for the requested devices."""
    owned: dict[str, dict] = {}
    try:
        active = device_lock_manager.registry.list_active(worker_id=None)
    except TypeError:
        active = device_lock_manager.registry.list_active()
    wanted = set(device_keys)
    for claim in active:
        key = str(claim.get("device_key") or "")
        if claim.get("owner_id") == owner_id and key in wanted:
            owned[key] = claim
    return owned


# Only reservation claims are workflow-reentrant. A live cluster-job claim is
# exclusive even for the same account: allowing an unrelated reboot/remount/
# flash operation to borrow it would mutate hardware under a running xTS job.
_BORROWABLE_SOURCE_TYPES = {"cluster-reservation"}


def _borrowable(claim: dict) -> bool:
    source_type = str(claim.get("source_type") or "")
    if source_type in _BORROWABLE_SOURCE_TYPES:
        return True
    source_id = str(claim.get("source_id") or "")
    return source_id.startswith("reservation:")


def _has_permission(user, permission: str) -> bool:
    try:
        return user.has_permission(permission)
    except Exception:
        return False


def acquire_device_operation_claim(
    request,
    device_ids: list[str],
    operation: str,
    *,
    ttl_seconds: int = 3600,
) -> tuple[str, list[dict], JSONResponse | None]:
    """Atomically fence a dynamic set of devices for one HTTP operation.

    Claim ownership follows ADR 0010's resource-owner account. The acting
    principal remains available as ``user.actor_id`` for audit attribution,
    but Agent token rotation and ATS machine execution must see/borrow claims
    created for the same human account.
    """

    user = require_authenticated_user(request)
    owner_id = user.resource_owner_id
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
    device_keys = [device_lock_manager._device(item)["device_key"] for item in devices]

    owned = _owned_local_device_keys(owner_id, device_keys)
    if user.role == "agent_service" and not _has_permission(user, "devices.use_leased"):
        return "", [], JSONResponse(
            content={
                "success": False,
                "error": "Agent token lacks devices.use_leased scope",
            },
            status_code=403,
        )

    borrowed = [
        owned[key] for key in device_keys
        if key in owned and _borrowable(owned[key])
    ]
    missing = [
        item for item, key in zip(devices, device_keys)
        if not (key in owned and _borrowable(owned[key]))
    ]
    source_id = f"operation:{operation}:{uuid.uuid4().hex}"
    records = list(borrowed)
    if missing:
        acquired, new_records = device_lock_manager.lock_devices(
            missing,
            owner_id,
            user.username,
            source_id=source_id,
            source_type=f"local-{operation}",
            ttl_seconds=ttl_seconds,
            allow_existing_source=False,
        )
        if not acquired:
            conflicts = [
                {
                    "device_id": row.get("serial", ""),
                    "source_type": row.get("source_type", "operation"),
                }
                for row in new_records
            ]
            return "", new_records, JSONResponse(
                content={
                    "success": False,
                    "error": "Device is reserved by an active operation",
                    "conflicts": conflicts,
                },
                status_code=409,
            )
        records.extend(new_records)
    request.state.device_lease_tokens = [
        {
            "lease_id": row["id"],
            "device_id": row["device_key"],
            "generation": row["generation"],
            "owner_id": owner_id,
        }
        for row in records
    ]
    if not missing:
        return "", records, None
    return source_id, records, None


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
        "owner_id": user.resource_owner_id,
        "actor_id": user.actor_id,
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
