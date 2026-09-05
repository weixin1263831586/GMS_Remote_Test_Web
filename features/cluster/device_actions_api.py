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
from .models import ClusterDeviceAction


router = APIRouter()


# 只读操作：不申请独占设备 claim（测试运行中的设备仍可查看信息），
# 也不走 exclusive fencing。新增只读 action 时在此登记。
READ_ONLY_DEVICE_ACTIONS = frozenset({
    "screenshot", "layout", "get_properties", "packages_with_path",
    "packages_all", "features", "props", "config_explore",
    "override_status", "bootloader_status",
})

# 需要管理员提权的写操作。
ELEVATED_DEVICE_ACTIONS = frozenset({
    "bootloader_lock", "bootloader_unlock", "override_apply",
    "override_revert", "override_disable_verity",
    "override_enable_verity", "override_reboot",
})


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
            and body.action in {
                "reboot_bootloader", "bootloader_lock", "bootloader_unlock",
            }
        ):
            raise HTTPException(
                409,
                f"ADB Proxy remote device has no local USB/Fastboot channel: {value}",
            )
        requested.append(device_id)

    user = require_authenticated_user(request)
    owner_id = user.id
    operation_id = f"device-action-{uuid.uuid4().hex}"
    claim_source = f"operation:{operation_id}"
    # 只读操作（Device Info/UI 操控等）不申请独占 claim：测试占用的设备
    # 仍可查看信息。写操作保持独占 lease + fencing token 不变。
    if not is_read_only:
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
            if not is_read_only:
                repository.claims.release(claim_source)
    try:
        command = repository.create_command({
            "worker_id": body.worker_id,
            "command_type": "device_action",
            "operation_id": operation_id,
            "payload": {**action_payload, "devices": requested},
        })
    except Exception:
        if not is_read_only:
            repository.claims.release(claim_source, status="failed")
        raise

    wait_steps = 1800 if body.action in {
        "config_explore", "override_apply", "override_revert",
    } else 350 if body.action in {
        "screenshot", "layout", "packages_with_path", "packages_all",
        "features", "props", "override_status",
    } else 100
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
