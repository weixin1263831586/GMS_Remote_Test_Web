"""Authenticated Worker command polling, ACK, and reconnect reconciliation."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Query, Request

from features.auth import (
    authentication_required,
    get_authenticated_user,
    is_elevated,
    require_authenticated_user,
)

from .api import _authenticate, service
from .models import CommandAck, CommandEventBatch


router = APIRouter()


# 需要终态通知的 device_action 集合：改变设备状态/可能触发重启的高风险
# 操作。读操作（screenshot/props/scrcpy 等）即时返回，不进通知中心。
NOTIFIED_DEVICE_ACTIONS = frozenset({
    "override_apply", "override_revert",
    "override_disable_verity", "override_enable_verity", "override_reboot",
    "bootloader_lock", "bootloader_unlock",
    "reboot", "reboot_bootloader", "remount",
})


def _require_worker_session(
    worker_id: str, session_id: str = "", generation: int = 0
) -> None:
    if not service().repository.validate_worker_session(
        worker_id, session_id, generation
    ):
        raise HTTPException(409, "stale worker session")


def _require_command_access(request: Request, command: dict[str, Any]) -> None:
    """浏览器访问单条 command 的归属校验。

    command 顶层没有 owner_id：job 类命令从 job 反查，device action 等
    从 payload.owner_id 判断（与 GET /commands/{id} 的规则一致）。
    """
    user = get_authenticated_user(request)
    if user is None:
        if authentication_required():
            require_authenticated_user(request)
        return
    if user.role == "admin" or is_elevated(request):
        return
    payload = command.get("payload") or {}
    owner_id = str(payload.get("owner_id") or "")
    job = None
    job_id = str(command.get("job_id") or "")
    if job_id or not owner_id:
        job = service().repository.get_job(job_id) if job_id else None
        owner_id = owner_id or str((job or {}).get("owner_id") or "")
    if owner_id != user.id:
        raise HTTPException(404, "command not found")


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
        # 用户切走 Worker/页面后不再轮询该 job，后台测试的
        # 完成/失败只能靠持久通知触达。
        command_id = str(command.get("id") or "")
        if command_id and service().repository.claim_terminal_notification(command_id):
            _notify_start_test_result(command)
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
        command_id = str(command.get("id") or "")
        if command_id and service().repository.claim_terminal_notification(command_id):
            _notify_suite_action_result(command)
    if command.get("command_type") in {"flash_firmware", "flash_gsi"} \
            and command.get("status") in {"completed", "failed", "cancelled"}:
        command_id = str(command.get("id") or "")
        if command_id and service().repository.claim_terminal_notification(command_id):
            _notify_flash_result(command)
    # 长耗时/改变设备状态的 device_action 也要终态通知（集合见
    # NOTIFIED_DEVICE_ACTIONS）。
    if command.get("command_type") == "device_action" \
            and command.get("status") in {"completed", "failed", "cancelled"}:
        action = str((command.get("payload") or {}).get("action") or "")
        if action in NOTIFIED_DEVICE_ACTIONS:
            command_id = str(command.get("id") or "")
            if command_id and service().repository.claim_terminal_notification(command_id):
                _notify_device_action_result(command)
    if command.get("command_type") == "file_transfer" \
            and command.get("status") in {"failed", "cancelled"}:
        # 拉取成功无需通知（后续烧写命令会发）；失败必须让发起人知道，
        # 否则页面关闭后跨 Worker 镜像传输失败将无任何记录。
        command_id = str(command.get("id") or "")
        if command_id and service().repository.claim_terminal_notification(command_id):
            _notify_file_transfer_failed(command)


def _notify_file_transfer_failed(command: dict[str, Any]) -> None:
    """Worker 镜像拉取命令失败时，向发起人发送通知中心消息。"""
    from features.system import queue_notification

    payload = command.get("payload") or {}
    owner_id = str(payload.get("owner_id") or "")
    if not owner_id:
        return
    worker_id = str(command.get("worker_id") or "")
    source_path = str(payload.get("source_path") or "")
    error = str(command.get("error") or "拉取失败").strip()
    queue_notification(
        owner_id,
        "Worker 镜像拉取失败",
        f"从 {worker_id} 拉取 {source_path} 失败：{error[:300]}",
        "error",
        "cluster",
        {
            "command_id": str(command.get("id") or ""),
            "worker_id": worker_id,
            "transfer_id": str(payload.get("transfer_id") or ""),
            "status": str(command.get("status") or ""),
        },
    )


def _notify_device_action_result(command: dict[str, Any]) -> None:
    """长耗时/改变设备状态的 device_action 终态通知。

    Controller 等待超时转 accepted 后浏览器不再轮询，Worker 后台完成的
    结果只能靠通知中心触达。owner 从 payload 取（device_action payload
    带 owner_id；无 owner 时静默跳过）。
    """
    # 跨 feature 只允许走 features.system 公开包边界。
    from features.system import queue_notification

    payload = command.get("payload") or {}
    owner_id = str(payload.get("owner_id") or "")
    if not owner_id:
        return
    status = str(command.get("status") or "")
    action = str(payload.get("action") or "device_action")
    action_labels = {
        "override_apply": "RRO 配置应用",
        "override_revert": "RRO 配置还原",
        "override_disable_verity": "关闭 dm-verity",
        "override_enable_verity": "开启 dm-verity",
        "override_reboot": "Override 后重启",
        "bootloader_lock": "Bootloader 锁定",
        "bootloader_unlock": "Bootloader 解锁",
        "reboot": "设备重启",
        "reboot_bootloader": "进入 Bootloader",
        "remount": "Remount",
    }
    subject = action_labels.get(action, action)
    worker_id = str(command.get("worker_id") or "")
    devices = [str(item).split(":", 1)[-1] for item in payload.get("devices") or []]
    device_text = ", ".join(devices) or "-"
    title = f"{subject}{'完成' if status == 'completed' else '失败' if status == 'failed' else '已取消'}"
    parts = [f"设备 {device_text}（Worker: {worker_id}）"]
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
            "command_type": "device_action",
            "action": action,
            "devices": devices,
            "status": status,
        },
    )


def _notify_start_test_result(command: dict[str, Any]) -> None:
    """Worker 测试命令到达终态时，向发起人发送通知中心消息。

    用户切到其他 Worker/页面后不再轮询该 job，后台测试的
    完成/失败必须持久通知。owner 从 job 记录反查（start_test 的
    payload 不带 owner_id）。
    """
    # 跨 feature 只允许走 features.system 公开包边界。
    from features.system import queue_notification

    job = service().repository.get_job(str(command.get("job_id") or "")) or {}
    owner_id = str(job.get("owner_id") or "")
    if not owner_id:
        return
    status = str(command.get("status") or "")
    job_id = str(command.get("job_id") or "")
    worker_id = str(command.get("worker_id") or job.get("assigned_worker_id") or "")
    request = job.get("request") or {}
    suite_key = str(request.get("suite_key") or "").strip() or "-"
    devices = [
        str(item).split(":", 1)[-1]
        for item in (
            request.get("devices")
            or [lease.get("device_id") for lease in job.get("leases") or []]
            or []
        )
    ]
    device_text = ", ".join(dict.fromkeys(devices)) or "-"
    title = f"{suite_key} 测试{'完成' if status == 'completed' else '失败' if status == 'failed' else '已取消'}"
    parts = [f"Worker: {worker_id}", f"设备: {device_text}", f"Job: {job_id}"]
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
            "job_id": job_id,
            "command_type": "start_test",
            "status": status,
        },
    )


def _notify_flash_result(command: dict[str, Any]) -> None:
    """远端固件/GSI 烧写命令到达终态时，向发起人发送通知中心消息。"""
    # 跨 feature 只允许走 features.system 公开包边界。
    from features.system import queue_notification

    payload = command.get("payload") or {}
    owner_id = str(payload.get("owner_id") or "")
    if not owner_id:
        return
    status = str(command.get("status") or "")
    subject = "GSI" if command.get("command_type") == "flash_gsi" else "固件"
    worker_id = str(command.get("worker_id") or "")
    devices = [str(item).split(":", 1)[-1] for item in payload.get("devices") or []]
    device_text = ", ".join(devices) or "-"
    title = f"远端{subject}烧写{'完成' if status == 'completed' else '失败' if status == 'failed' else '已取消'}"
    parts = [f"设备 {device_text}（Worker: {worker_id}）"]
    error = str(command.get("error") or "").strip()
    result = command.get("result") or {}
    output = str(result.get("output") or "").strip() if isinstance(result, dict) else ""
    if output:
        parts.append(output[-300:])
    elif error:
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
            "command_type": str(command.get("command_type") or ""),
            "devices": devices,
            "status": status,
        },
    )


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


@router.post("/workers/{worker_id}/commands/{command_id}/events")
def append_command_events(
    worker_id: str,
    command_id: str,
    body: CommandEventBatch,
    authorization: str | None = Header(default=None),
    worker_session: str = Header(default=""),
    worker_generation: int = Header(default=0, alias="X-GMS-Worker-Generation"),
):
    """Worker 上报命令执行过程日志（实时日志通道）。

    与 job events 平行的 command 维度通道：烧写/device_action 等无 job 的
    长命令把 fastboot/upgrade_tool 逐行输出上报到这里，浏览器按
    command_id 增量拉取，达到与单机模式一致的实时日志体验。
    """
    _authenticate(worker_id, authorization)
    _require_worker_session(worker_id, worker_session, worker_generation)
    inserted = service().repository.append_command_events(
        worker_id, command_id, [item.model_dump() for item in body.events]
    )
    return {"success": True, "inserted": inserted}


@router.get("/commands/{command_id}/events")
def list_command_events(
    command_id: str,
    request: Request,
    after: int = Query(default=-1),
    limit: int = Query(default=500, le=2000),
):
    """浏览器增量拉取命令过程日志（after 为上次收到的最大 sequence）。"""
    command = service().repository.get_command(command_id)
    if command is None:
        raise HTTPException(404, "command not found")
    _require_command_access(request, command)
    events = service().repository.list_command_events(command_id, after, limit)
    return {
        "success": True,
        "command": {"id": command_id, "status": command.get("status", "")},
        "events": events,
    }


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
