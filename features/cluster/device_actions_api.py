"""Lease-protected device actions for local and remote Cluster Workers."""

from __future__ import annotations

import asyncio
import uuid

from fastapi import APIRouter, HTTPException, Request

from features.auth import (
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
from .operation_claims import device_action_claim_payload


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
    # Agent Service Token（ADR 0006）：额外要求 devices.use_leased scope
    # 与 allowed_devices/allowed_workers ACL。人类普通用户不做“先租后用”
    # 限制：常规操作可直接作用于空闲设备，冲突由下方 claim/fencing 的
    # 409 兜底；高危操作已在入口挂 require_elevated_admin_when_auth_required。
    if user.role == "agent_service":
        from features.auth import ensure_agent_device_allowed, ensure_agent_worker_allowed

        ensure_agent_worker_allowed(request, body.worker_id)
        if not user.has_permission("devices.use_leased"):
            raise HTTPException(
                403,
                "Agent token lacks devices.use_leased scope",
            )
        for item in body.devices:
            ensure_agent_device_allowed(request, item)
    operation_id = f"device-action-{uuid.uuid4().hex}"
    claim_source = f"operation:{operation_id}"
    if not is_read_only:
        action_payload.update(device_action_claim_payload(
            repository, body.worker_id, requested, operation_id,
            owner_id, username=user.username,
        ))
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
            if action_payload.get("release_claim_on_terminal"):
                repository.claims.release(claim_source)
    try:
        command = repository.create_command({
            "worker_id": body.worker_id,
            "command_type": "device_action",
            "operation_id": operation_id,
            "payload": {**action_payload, "devices": requested},
        })
    except Exception:
        if action_payload.get("release_claim_on_terminal"):
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
