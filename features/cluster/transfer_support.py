from __future__ import annotations

from fastapi import HTTPException, Request

from features.auth import principal_owner_id

from .api import service
from .operation_claims import operation_claim_payload


def worker_device(
    worker_id: str,
    devices: str,
    operation: str,
    reservation_id: str = "",
    automation_run_id: str = "",
) -> str:
    requested = [item.strip() for item in devices.split(",") if item.strip()]
    if len(requested) != 1:
        raise HTTPException(400, f"cluster {operation} requires exactly one device")
    device_id = requested[0] if requested[0].startswith(f"{worker_id}:") else f"{worker_id}:{requested[0]}"
    device = next(
        (item for item in service().repository.list_devices(worker_id) if item["id"] == device_id),
        None,
    )
    if not device:
        raise HTTPException(409, "device is not available on worker")
    reservation = service().repository.get_reservation(reservation_id) if reservation_id else None
    reserved_for_request = bool(
        reservation
        and reservation.get("status") == "active"
        and reservation.get("worker_id") == worker_id
        and (not automation_run_id or reservation.get("source_id") == automation_run_id)
        and device_id in {item["id"] for item in reservation.get("devices") or []}
    )
    if device.get("state") != "available" and not reserved_for_request:
        raise HTTPException(409, "device is not available on worker")
    return device_id


def operation_claim_for_request(
    request: Request,
    worker_id: str,
    device_id: str,
    operation_id: str,
    *,
    reservation_id: str = "",
) -> dict:
    owner_id = principal_owner_id(request)
    payload = operation_claim_payload(
        service().repository,
        worker_id,
        device_id,
        operation_id,
        owner_id,
        reservation_id=reservation_id,
    )
    request.state.device_lease_tokens = [
        {**token, "owner_id": owner_id}
        for token in payload.get("lease_tokens") or []
    ]
    return payload
