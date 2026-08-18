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
    if command.get("command_type") in {"usbip_attach", "usbip_detach"}:
        from features.devices import (
            reconcile_cluster_usbip_command,
        )

        reconcile_cluster_usbip_command(command, service().repository)
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
    if command.get("command_type") == "suite_action" \
            and command.get("status") in {"completed", "failed", "cancelled"}:
        _notify_suite_action_result(command)


def _notify_suite_action_result(command: dict[str, Any]) -> None:
    """套件下发/解压命令到达终态时，向发起人发送通知中心消息。"""
    # 跨 feature 只允许走 features.system 公开包边界。
    from features.system import queue_notification

    payload = command.get("payload") or {}
    owner_id = str(payload.get("owner_id") or "")
    if not owner_id:
        return
    action = str(payload.get("action") or "")
    action_label = {"download_url": "下发", "extract": "解压"}.get(action, "处理")
    status = str(command.get("status") or "")
    subject = str(
        payload.get("filename") or payload.get("target_dir_name") or ""
    ).strip() or "测试套件"
    worker_id = str(command.get("worker_id") or "")
    title = f"测试套件{action_label}{'完成' if status == 'completed' else '失败' if status == 'failed' else '已取消'}"
    parts = [f"{subject}（Worker: {worker_id}）"] if worker_id else [subject]
    error = str(command.get("error") or "").strip()
    if error:
        parts.append(error[:300])
    level = {"completed": "success", "failed": "error"}.get(status, "warning")
    queue_notification(
        owner_id,
        title,
        "；".join(parts),
        level,
        "cluster",
        {
            "command_id": str(command.get("id") or ""),
            "worker_id": worker_id,
            "action": action,
            "status": status,
        },
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
