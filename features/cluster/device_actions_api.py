"""Lease-protected device actions for local and remote Cluster Workers."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from features.auth import (
    authentication_required,
    require_authenticated_user,
    require_elevated_admin_when_auth_required,
)
from foundation.config import config_manager

from .api import _require_cluster_enabled, service
from .device_action_spec import (
    adb_proxy_forbidden_device_actions,
    device_action_wait_steps,
    elevated_device_actions,
    read_only_device_actions,
)
from .models import ClusterDeviceAction


router = APIRouter()


# 派生集合：真值在 device_action_spec.DeviceActionSpec 中，这里只为
# 保持既有模块级引用兼容。
READ_ONLY_DEVICE_ACTIONS = read_only_device_actions()
ELEVATED_DEVICE_ACTIONS = elevated_device_actions()
# 派生集合：真值在 device_action_spec.DeviceActionSpec 中（forbidden_on_adb_proxy）。
ADB_PROXY_FORBIDDEN_DEVICE_ACTIONS = adb_proxy_forbidden_device_actions()


@router.post("/devices/actions")
async def device_action(body: ClusterDeviceAction, request: Request):
    if body.action in ELEVATED_DEVICE_ACTIONS:
        require_elevated_admin_when_auth_required(request)
    repository = service().repository
    is_local = body.worker_id == service().config.local_worker_id
    _require_cluster_enabled(remote=not is_local)
    worker = repository.get_worker(body.worker_id)
    if not worker or worker.get("status") not in {"online", "busy", "draining"}:
        raise HTTPException(409, "worker is not online")
    known = {item["id"]: item for item in repository.list_devices(body.worker_id)}
    is_read_only = body.action in READ_ONLY_DEVICE_ACTIONS
    action_payload = body.model_dump(exclude={"worker_id", "devices"})
    if body.action == "wifi":
        wifi = config_manager.load_config().get("wifi") or {}
        action_payload["ssid"] = body.ssid or str(wifi.get("ssid") or "")
        action_payload["password"] = body.password or str(wifi.get("password") or "")

    requested = []
    for value in body.devices:
        device_id = (
            value if value.startswith(f"{body.worker_id}:")
            else f"{body.worker_id}:{value}"
        )
        device = known.get(device_id)
        if not device:
            raise HTTPException(409, f"device is not available on worker: {value}")
        state = device.get("state")
        if state in {"offline", "unknown"}:
            raise HTTPException(409, f"device is offline: {value}")
        if state == "external_busy" and not is_read_only:
            raise HTTPException(409, f"device is busy with a manual Tradefed test: {value}")
        if (
            device.get("transport") == "adb_proxy"
            and body.action in ADB_PROXY_FORBIDDEN_DEVICE_ACTIONS
        ):
            raise HTTPException(
                409,
                f"ADB Proxy remote device has no local USB/Fastboot channel: {value}",
            )
        requested.append(device_id)

    user = require_authenticated_user(request)
    owner_id = user.id
    # devices.use_leased 语义落地：普通 user（无 devices.lease 权限）对
    # 非只读操作只能作用于自己已通过 claim/reservation 占有的设备；
    # device_operator/admin 才可抢占任意空闲设备。否则权限名的安全承诺
    # 与实际行为不一致。
    if (
        not is_read_only
        and authentication_required()
        and not user.has_permission("devices.lease")
    ):
        owned = {
            str(claim.get("device_key") or "")
            for claim in repository.claims.list_active(owner_id=owner_id)
        }
        owned |= repository.owned_reservation_device_ids(owner_id)
        foreign = [item for item in requested if item not in owned]
        if foreign:
            raise HTTPException(
                403,
                "devices.use_leased only permits actions on devices you "
                "already lease or reserve; ask a device operator for access",
            )
    operation_id = f"device-action-{uuid.uuid4().hex}"
    claim_source = f"operation:{operation_id}"
    # 只读操作（Device Info/UI 操控等）不申请独占 claim：测试占用的设备
    # 仍可查看信息。写操作保持独占 lease + fencing token 不变。
    # borrowed: 设备已被同一 user 的 reservation/job claim 占有时，直接
    # 复用该 claim 的 fencing token，不再二次 acquire——否则
    # acquire 会因 source_id 不同而与用户自己的 reservation 冲突，
    # 形成"没 claim 403、有 claim 409"的悖论。
    borrowed_claims: list[dict[str, Any]] = []
    if not is_read_only:
        if not user.has_permission("devices.lease"):
            existing_by_key = {
                str(claim.get("device_key") or ""): claim
                for claim in repository.claims.list_active(owner_id=owner_id)
            }
            borrowed_claims = [
                existing_by_key[item]
                for item in requested
                if item in existing_by_key
            ]
        if borrowed_claims:
            action_payload.update({
                "owner_id": owner_id,
                "lease_tokens": repository.claim_fencing_tokens(
                    borrowed_claims, operation_id),
                # 借用：终态绝不 release——claim 生命周期归 reservation/job。
                "release_claim_on_terminal": False,
            })
            request.state.device_lease_tokens = [
                {**token, "owner_id": owner_id}
                for token in action_payload["lease_tokens"]
            ]
        else:
            try:
                records = repository.acquire_device_operation_claim(
                    body.worker_id,
                    requested,
                    owner_id=owner_id,
                    source_type="cluster-device-action",
                    source_id=claim_source,
                    ttl_seconds=3600,
                    username=user.username,
                )
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from exc
            action_payload.update({
                "owner_id": owner_id,
                "claim_source_id": claim_source,
                "release_claim_on_terminal": True,
                "lease_tokens": repository.claim_fencing_tokens(records, operation_id),
            })
            request.state.device_lease_tokens = [
                {**token, "owner_id": owner_id}
                for token in action_payload["lease_tokens"]
            ]
    else:
        action_payload.update({"owner_id": owner_id, "read_only": True})

    if is_local:
        try:
            from worker_agent.inventory import execute_device_action

            # ADB/aapt2/screenshot actions are blocking subprocess work. Keep
            # them off the FastAPI event loop so unrelated UI controls and
            # WebSockets remain responsive while Device Info is loading.
            result = await asyncio.to_thread(
                execute_device_action, body.action, requested, action_payload
            )
            return {"success": True, **result}
        finally:
            # 借用 claim（reservation/job 所有）不在此 release——
            # release(claim_source) 对借用的 source_id 也无匹配行。
            if not is_read_only and not borrowed_claims:
                repository.claims.release(claim_source)
    try:
        command = repository.create_command({
            "worker_id": body.worker_id,
            "command_type": "device_action",
            "operation_id": operation_id,
            "payload": {**action_payload, "devices": requested},
        })
    except Exception:
        if not is_read_only and not borrowed_claims:
            repository.claims.release(claim_source, status="failed")
        raise

    wait_steps = device_action_wait_steps(body.action)
    for _ in range(wait_steps):
        await asyncio.sleep(0.1)
        current = repository.get_command(command["id"])
        if current and current["status"] in {"completed", "failed", "cancelled"}:
            if current["status"] != "completed":
                raise HTTPException(
                    502, current.get("error") or "worker device action failed"
                )
            result = current.get("result") or {}
            if body.action == "screenshot" and result.get("image"):
                repository.compact_command_result(command["id"], {
                    "serial": result.get("serial", ""),
                    "image_bytes": len(result["image"]),
                    "transient_result": True,
                })
            return {"success": True, **result, "command_id": command["id"]}
    return {"success": True, "accepted": True, "command_id": command["id"]}
