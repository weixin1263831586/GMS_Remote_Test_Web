"""Fencing payloads for lease-protected non-test Worker operations."""

from __future__ import annotations

from fastapi import HTTPException


def operation_claim_payload(
    repository,
    worker_id: str,
    device_id: str,
    operation_id: str,
    owner_id: str,
    *,
    reservation_id: str = "",
    username: str = "",
) -> dict:
    if reservation_id:
        reservation = repository.get_reservation(reservation_id)
        if not reservation or reservation.get("status") != "active":
            raise HTTPException(409, "device reservation is missing or expired")
        if reservation.get("owner_id") != owner_id:
            raise HTTPException(404, "device reservation not found")
        claim = repository.claims.active_claim(device_id)
        expected_source = f"reservation:{reservation_id}"
        if not claim or claim.get("source_id") != expected_source:
            raise HTTPException(409, "device reservation claim is missing or expired")
        records = [claim]
        claim_source = expected_source
        release_on_terminal = False
    else:
        claim_source = f"operation:{operation_id}"
        try:
            records = repository.acquire_device_operation_claim(
                worker_id,
                [device_id],
                owner_id=owner_id,
                source_type="cluster-firmware",
                source_id=claim_source,
                ttl_seconds=6 * 60 * 60,
                username=username,
            )
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        release_on_terminal = True
    return {
        "owner_id": owner_id,
        "claim_source_id": claim_source,
        "release_claim_on_terminal": release_on_terminal,
        "lease_tokens": repository.claim_fencing_tokens(records, operation_id),
    }
