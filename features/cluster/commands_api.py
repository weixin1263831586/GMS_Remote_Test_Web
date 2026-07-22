"""Authenticated Worker command polling, ACK, and reconnect reconciliation."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Header, HTTPException

from .api import _authenticate, service
from .models import CommandAck


router = APIRouter()


def _require_worker_session(
    worker_id: str, session_id: str = "", generation: int = 0
) -> None:
    if not service().repository.validate_worker_session(
        worker_id, session_id, generation
    ):
        raise HTTPException(409, "stale worker session")


def synchronize_command(command: dict[str, Any]) -> None:
    """Apply a durable ACK to its Job and correlated transfer/report state."""
    service().repository.sync_job_from_command(command)
    if (
        command.get("command_type") == "refresh_suites"
        and command.get("status") == "completed"
    ):
        suites = (command.get("result") or {}).get("suites")
        if isinstance(suites, list):
            service().repository.replace_worker_suites(
                str(command.get("worker_id") or ""), suites
            )
    if command.get("status") in {"completed", "failed", "cancelled"}:
        payload = command.get("payload") or {}
        claim_source = str(payload.get("claim_source_id") or "")
        if claim_source and payload.get("release_claim_on_terminal") is True:
            service().repository.claims.release(
                claim_source,
                status=(
                    "released" if command.get("status") == "completed"
                    else "cancelled" if command.get("status") == "cancelled"
                    else "failed"
                ),
            )
    if command.get("job_id") and command.get("command_type") == "start_test" \
            and command.get("status") in {"completed", "failed", "cancelled"}:
        from .report_index import update_cluster_report_status

        update_cluster_report_status(
            command["job_id"], command["status"], command.get("error", "")
        )
    from .transfers_api import cleanup_staged_firmware

    cleanup_staged_firmware(command)
    if command.get("command_type") in {"suite_export", "device_export"} \
            and command.get("status") in {"failed", "cancelled"}:
        transfer_id = (command.get("payload") or {}).get("transfer_id", "")
        if transfer_id:
            service().repository.update_transfer(
                transfer_id,
                status="failed",
                error=command.get("error") or "worker export failed",
            )


@router.post("/workers/{worker_id}/commands/poll")
async def poll_commands(
    worker_id: str,
    authorization: str | None = Header(default=None),
    worker_session: str = Header(default="", alias="X-GMS-Worker-Session"),
    worker_generation: int = Header(default=0, alias="X-GMS-Worker-Generation"),
):
    _authenticate(worker_id, authorization)
    if service().repository.get_worker(worker_id) is None:
        raise HTTPException(404, "worker is not registered")
    _require_worker_session(worker_id, worker_session, worker_generation)
    for _ in range(20):
        commands = service().repository.poll_commands(worker_id)
        if commands:
            return {"success": True, "commands": commands}
        await asyncio.sleep(0.5)
    return {"success": True, "commands": []}


@router.post("/workers/{worker_id}/commands/{command_id}/ack")
def ack_command(
    worker_id: str,
    command_id: str,
    body: CommandAck,
    authorization: str | None = Header(default=None),
    worker_session: str = Header(default="", alias="X-GMS-Worker-Session"),
    worker_generation: int = Header(default=0, alias="X-GMS-Worker-Generation"),
):
    _authenticate(worker_id, authorization)
    _require_worker_session(worker_id, worker_session, worker_generation)
    command = service().repository.ack_command(
        worker_id, command_id, body.model_dump()
    )
    if command is None:
        raise HTTPException(404, "command not found")
    synchronize_command(command)
    return {"success": True, "command": command}
