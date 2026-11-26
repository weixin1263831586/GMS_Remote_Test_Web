"""Lease fencing, request binding and execution receipts for device operations."""
from __future__ import annotations

import hashlib
import json
import logging

from fastapi import HTTPException

from foundation.error_model import ApiError


logger = logging.getLogger(__name__)


def device_action_claim_payload(
    repository, worker_id: str, device_ids: list[str], operation_id: str,
    owner_id: str, *, username: str = "",
) -> dict:
    """Fence every target while preserving the lifetime of borrowed claims.

    Only reservation claims may be borrowed (the holder's flash→test
    workflow umbrella). cluster-job claims are NOT borrowable: mutating a
    device under the owner's own running job must 409 instead of silently
    borrowing the job's claim (workflow-level reentrancy; see ADR 0011 on
    claim-consistency semantics).
    """
    source_id = f"operation:{operation_id}"
    owned = {
        str(claim.get("device_key") or ""): claim
        for claim in repository.claims.list_active(owner_id=owner_id)
        if claim.get("source_type") == "cluster-reservation"
        or str(claim.get("source_id") or "").startswith("reservation:")
    }
    records = [owned[key] for key in device_ids if key in owned]
    missing = [key for key in device_ids if key not in owned]
    if missing:
        try:
            records.extend(repository.acquire_device_operation_claim(
                worker_id, missing, owner_id=owner_id,
                source_type="cluster-device-action", source_id=source_id,
                ttl_seconds=3600, username=username,
            ))
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
    return {
        "owner_id": owner_id,
        "claim_source_id": source_id,
        "release_claim_on_terminal": bool(missing),
        "lease_tokens": repository.claim_fencing_tokens(
            records, operation_id, device_ids=set(device_ids)
        ),
    }


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
        "lease_tokens": repository.claim_fencing_tokens(
            records, operation_id, device_ids={device_id}
        ),
    }


def request_digest(worker_id: str, devices: list[str], payload: dict, owner_id: str) -> str:
    canonical = json.dumps({"worker_id": worker_id, "devices": devices,
                            "payload": payload, "owner_id": owner_id},
                           sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def require_same_request(command: dict, owner_id: str, digest: str) -> None:
    payload = command.get("payload") or {}
    if payload.get("owner_id") != owner_id:
        raise ApiError.not_found("device action operation not found")
    if payload.get("request_sha256") != digest:
        raise ApiError.conflict("operation key is already bound to a different request")


def fenced_local_payload(repository, payload: dict) -> dict:
    """Revalidate local AOA claims throughout calibration and playback."""
    tokens = tuple(dict(token) for token in payload.get("lease_tokens") or [])

    def validate_claims():
        if not tokens:
            raise RuntimeError("AOA requires an active device claim")
        for token in tokens:
            current = repository.claims.active_claim(token["device_id"])
            if (not current or current["id"] != token["lease_id"]
                    or current["generation"] != token["generation"]):
                raise RuntimeError("device claim changed during AOA; reconcile required")

    validate_claims()
    return {**payload, "_validate_fencing": validate_claims}


def _local_aoa_progress_reporter(repository, command: dict):
    """本机 durable_local ``aoa_sequence`` 的逐条进度上报。

    远端 Worker 经 HTTP ``POST /workers/{id}/commands/{id}/events`` 上报
    （worker/app.py 的 CommandEventUploader）；本机执行与 repository
    同进程，直接 append（同 UNIQUE(command_id,sequence) 幂等去重），事件
    字段与远端路径完全一致，保证浏览器对两种执行路径的高亮行为相同。
    进度上报失败绝不打断回放：命令终态仍由 ack 收敛兜底。
    """
    worker_id = command["worker_id"]
    command_id = command["id"]

    sequence = 0

    def report_event(event: dict) -> None:
        nonlocal sequence
        sequence += 1
        index, total = event.get("index", 0), event.get("total", 0)
        text = str(event.get("command") or "")
        try:
            repository.append_command_events(worker_id, command_id, [{
                "sequence": sequence, "event_type": "progress", "source": "aoa", "level": "info",
                "message": f"{index}/{total} {event.get('phase', '')} {text}"[:500],
                "payload": {**event, "command": text[:1000]},
            }])
        except Exception:
            logger.warning("local AOA progress write failed for %s", command_id, exc_info=True)

    def report(index: int, total: int, text: str) -> None:
        report_event({"phase": "completed", "index": index, "total": total, "command": text})

    return report, report_event


def execute_local_operation(repository, command: dict, execute) -> None:
    """The running receipt is committed before the first physical input.

    A lost response only follows this receipt. An interrupted process must
    never restart a running HID sequence from the first command.
    """
    payload = command["payload"]
    try:
        payload = fenced_local_payload(repository, payload)
        if payload.get("aoa_debug_session_id"):
            from .aoa_playback import debug_command_source

            payload = {**payload, "_aoa_debug_source": debug_command_source(repository, command["id"])}
        report, report_event = (_local_aoa_progress_reporter(repository, command)
                                if payload.get("action") == "aoa_sequence" else (None, None))
        if payload.get("playback_control_id"):
            def control_source():
                current = repository.get_command(command["id"])
                if not current:
                    raise RuntimeError("AOA operation disappeared; stop playback")
                state = current.get("payload") or {}
                return {"action": state.get("aoa_control_action"),
                        "revision": state.get("aoa_control_revision", 0)}

            payload = {**payload, "_aoa_control_source": control_source}
        result = execute(payload["action"], payload["devices"], payload,
                         progress_callback=report, progress_event_callback=report_event)
        repository.ack_command(command["worker_id"], command["id"], {
            "status": "completed", "result": result,
        })
    except Exception as exc:
        repository.ack_command(command["worker_id"], command["id"], {
            "status": "cancelled" if getattr(exc, "cancelled", False) else "failed", "error": str(exc),
            "result": {**getattr(exc, "aoa_result", {}),
                       "success": False, "reconcile_required": bool(getattr(exc, "cleanup_errors", []))
                       or not getattr(exc, "cancelled", False), "cleanup_errors": getattr(exc, "cleanup_errors", [])},
        })
    finally:
        if payload.get("release_claim_on_terminal"):
            repository.claims.release(payload["claim_source_id"])
