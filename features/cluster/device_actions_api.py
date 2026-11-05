"""Lease-protected device actions for local and remote Cluster Workers."""

from __future__ import annotations

import asyncio
import logging
import threading
import uuid

from fastapi import APIRouter, HTTPException, Request

from features.auth import (
    principal_actor_id,
    require_authenticated_user,
    require_elevated_admin_when_auth_required,
    require_human_principal,
)
from foundation.aoa_profile import MAX_AOA_OPERATION_SECONDS, MAX_AOA_PAUSE_SECONDS
from foundation.config import config_manager
from foundation.device_action_spec import (
    adb_proxy_forbidden_device_actions,
    device_action_wait_steps,
    elevated_device_actions,
    read_only_device_actions,
)
from foundation.error_model import ApiError, record_internal_error

from .api import _require_cluster_enabled, service
from .device_action_operations import (
    execute_local_operation,
    fenced_local_payload,
    request_digest,
    require_same_request,
)
from .models import ClusterDeviceAction
from .operation_claims import device_action_claim_payload


router = APIRouter()
logger = logging.getLogger(__name__)


def execute_local_firmware_aoa(action, devices, options, **kwargs):
    """Same-host execution seam for the fenced firmware continuation."""
    from worker_agent.inventory import execute_device_action

    return execute_device_action(action, devices, {
        **options, "suppress_adb_daemon": _local_aoa_adb_suppression_allowed(),
    }, **kwargs)


def validate_local_firmware_identity(device_id: str, port: str) -> None:
    from worker_agent.inventory import probe_devices

    serial = device_id.split(":", 1)[-1]
    device = next((item for item in probe_devices() if item["serial"] == serial), {})
    if (not device or device.get("transport") == "adb_proxy"
            or (device.get("properties") or {}).get("usb") != port):
        raise ApiError.conflict("烧写前 USB 物理端口已变化，请刷新设备后重试")


def _local_aoa_adb_suppression_allowed() -> bool:
    """本机 AOA 回放可否抑制主机 adb daemon（失败即关闭抑制）。

    抑制会杀掉 adb server 并让 Worker 心跳跳过 adb 枚举（见
    worker_agent.adb_suppression）；本机存在 Tradefed 任务或 ADB Proxy
    导入时会破坏它们的设备通信，此时保持抑制关闭，回放退回与 adb
    daemon 共存的现状。
    """
    try:
        from worker_agent.adb_proxy import imported_devices
        from worker_agent.process_inventory import discover_tradefed_processes

        return (not discover_tradefed_processes()
                and not imported_devices())
    except (OSError, RuntimeError, ValueError):
        return False


# 派生集合：真值在 device_action_spec.DeviceActionSpec 中，这里只为
# 保持既有模块级引用兼容。
READ_ONLY_DEVICE_ACTIONS = read_only_device_actions()
ELEVATED_DEVICE_ACTIONS = elevated_device_actions()
# 派生集合：真值在 device_action_spec.DeviceActionSpec 中（forbidden_on_adb_proxy）。
ADB_PROXY_FORBIDDEN_DEVICE_ACTIONS = adb_proxy_forbidden_device_actions()


def _require_machine_reservation(
    request: Request, worker_id: str, devices: list[str]
) -> dict:
    """Restrict machine principals to devices inside their run reservation.

    A machine capability (ADR 0012) must not widen into "operate any device
    the account could lease": the automation run may only probe devices
    reserved under its own run id.
    """

    principal = getattr(request.state, "current_user", None)
    principal_id = str(getattr(principal, "id", "") or "")
    run_id = principal_id.removeprefix("automation:")
    reservation = (
        service().repository.get_reservation_by_source(run_id) if run_id else None
    )
    reserved = {
        str(item.get("id") or "")
        for item in (reservation or {}).get("devices") or []
    }
    if (
        not reservation
        or str(reservation.get("worker_id") or "") != worker_id
        or any(device_id not in reserved for device_id in devices)
    ):
        raise HTTPException(
            403, "machine principal may only operate devices in its own reservation"
        )
    return reservation


async def _wait_for_command(
    repository, action, command: dict, *, deduplicated: bool = False
) -> dict:
    """短轮询一条 Worker 命令到终态；超时则返回 accepted 交由调用方续查。"""
    flag = {"deduplicated": True} if deduplicated else {}
    if (command.get("payload") or {}).get("aoa_debug_session_id"):
        flag["debug_sequence"] = 1
    if action == "aoa_sequence":
        return {"success": True, "accepted": True, "command_id": command["id"],
                "operation_timeout_seconds": MAX_AOA_OPERATION_SECONDS + (
                    MAX_AOA_PAUSE_SECONDS if command.get("payload", {}).get("playback_control_id") else 0), **flag}
    wait_steps = device_action_wait_steps(action)
    for _ in range(wait_steps):
        await asyncio.sleep(0.1)
        current = repository.get_command(command["id"])
        if current and current["status"] in {"completed", "failed", "cancelled"}:
            if current["status"] != "completed":
                raise HTTPException(
                    502, current.get("error") or "worker device action failed"
                )
            result = current.get("result") or {}
            if action == "screenshot" and result.get("image"):
                repository.compact_command_result(command["id"], {
                    "serial": result.get("serial", ""),
                    "image_bytes": len(result["image"]),
                    "transient_result": True,
                })
            return {"success": True, **result, "command_id": command["id"], **flag}
    return {"success": True, "accepted": True, "command_id": command["id"], **flag}


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
    # automation_run_id 只用于派生幂等 operation_id，不进入 Worker payload。
    action_payload = body.model_dump(
        exclude={"worker_id", "devices", "automation_run_id"})
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
        is_aoa_recovery = str(body.action.value) in {"aoa_probe", "aoa_sequence"}
        if state in {"offline", "unknown"} and not is_aoa_recovery:
            raise HTTPException(409, f"device is offline: {value}")
        if state == "usb_only" and not is_aoa_recovery:
            raise HTTPException(
                409,
                f"device has no ADB connection; only AOA recovery is available: {value}",
            )
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
    # ADR 0010: device-action claims/leases belong to the resource-owner
    # ACCOUNT, so machine principals and agent tokens attribute leases to the
    # creating account rather than a synthetic actor id.
    owner_id = user.resource_owner_id
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
        # Machine principals (ADR 0012) additionally may only touch devices
        # inside their run's active reservation.
        if getattr(request.state, "auth_method", None) == "machine_authority":
            _require_machine_reservation(request, body.worker_id, requested)
    automation_run_id = str(body.automation_run_id or "").strip()
    digest = request_digest(body.worker_id, requested, action_payload, owner_id)
    if body.aoa_debug_session_id:
        require_human_principal(request)
        from .aoa_debug import prepare_debug_request

        operation_id, response = prepare_debug_request(
            repository, body, action_payload, requested, owner_id, is_local=is_local)
        if response is not None:
            return response
    elif body.aoa_debug_request_id or body.aoa_debug_command:
        raise ApiError.invalid_semantics("AOA debug fields require a debug session id")
    elif automation_run_id:
        reservation = repository.get_reservation_by_source(automation_run_id)
        if (not reservation or reservation.get("owner_id") != owner_id
                or reservation.get("worker_id") != body.worker_id
                or set(requested) != {item["id"] for item in reservation.get("devices") or []}):
            raise ApiError.conflict("automation run does not own the requested reservation")
        if (getattr(request.state, "auth_method", None) == "machine_authority"
                and principal_actor_id(request) != f"automation:{automation_run_id}"):
            raise ApiError.conflict("automation operation belongs to a different run")
        # 幂等重放键（ADR 0022）：automation 发起的 device_action 用
        # run 维度确定性 operation_id，配合 repository.create_command 的
        # BEGIN IMMEDIATE 去重，使“派发成功但响应丢失”的重试收敛到
        # 既有命令；并发双发由数据库唯一索引兜底。
        operation_id = f"automation:{automation_run_id}:{body.action.value}"
        existing = repository.get_command_by_operation(body.worker_id, operation_id)
        if existing is not None:
            require_same_request(existing, owner_id, digest)
            # 重放命中：直接跟随既有命令（可能仍在执行或已完成），
            # 不再重复 claim/派发，避免整段重放 AOA 点击序列。
            return await _wait_for_command(
                repository, body.action, existing, deduplicated=True)
    else:
        operation_id = f"device-action-{uuid.uuid4().hex}"
    action_payload["request_sha256"] = digest
    action_payload["skip_if_adb_online"] = bool(automation_run_id) and body.action == "aoa_sequence"
    needs_mapping = (body.aoa_touch_mapping is not None or body.aoa_source_touch_mapping is not None
        or body.aoa_mapping_policy in {"unsupported", "direct_calibrated", "pointer_calibrated"}
        or (body.action == "aoa_record_input" and (body.aoa_record.get("touch_mapping") is not None
            or body.aoa_record.get("action") == "calibrate"))
        or (body.action == "aoa_record" and bool(body.aoa_record.get("calibrate_touch"))))
    if not is_local and needs_mapping and (worker.get("capabilities") or {}).get("aoa_affine_mapping_v2") is not True:
        raise ApiError.conflict("Worker 标定映射协议过旧，请更新 Worker 后重试")
    if (not is_local and body.action == "aoa_sequence" and body.aoa_hid_session_mode == "per_command"
            and (worker.get("capabilities") or {}).get("aoa_per_command_v1") is not True):
        raise ApiError.conflict("Worker 不支持按单条方式回放，请更新 Worker")
    if (not is_local and body.action == "aoa_sequence"
            and (worker.get("capabilities") or {}).get("aoa_hid_modes_v1") is not True):
        raise ApiError.conflict("Worker 不支持 AOA 输入模式选择，请更新 Worker 后再回放")
    if (not is_local and body.action in {"aoa_sequence", "aoa_record", "aoa_record_input"}
            and (worker.get("capabilities") or {}).get("aoa_identity_fencing_v1") is not True):
        raise ApiError.conflict("Worker AOA safety contract is outdated; update the Worker before recovery")
    if (body.playback_control_id and not is_local
            and (worker.get("capabilities") or {}).get("aoa_playback_control_v1") is not True):
        raise ApiError.conflict("Worker needs an update to support AOA pause/resume")
    if body.action in {"aoa_sequence", "aoa_probe"}:
        if len(requested) != 1:
            raise ApiError.conflict("AOA requires exactly one physical device")
        current_port = str((known[requested[0]].get("properties") or {}).get("usb") or "")
        if body.usb_port_path and current_port != body.usb_port_path:
            raise ApiError.conflict("physical USB identity differs from the reserved device")
        action_payload["usb_port_path"] = body.usb_port_path or current_port
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

    durable_local = is_local and body.action == "aoa_sequence"
    if durable_local:
        # AOA 会话期抑制主机 adb daemon（18d1:2D0x 复合 accessory 的 adb
        # 接口会让 daemon 与 pyusb 抢设备，实机 RK3576 复现 reset 风暴）。
        # 仅在无 Tradefed 任务且无 ADB Proxy 导入时启用：杀 adb server
        # 不能破坏本机其他任务的设备通信。远端 Worker 路径不携带本机
        # 决策，由 worker_agent.app 按自身状态判定。
        action_payload["suppress_adb_daemon"] = _local_aoa_adb_suppression_allowed()
    if is_local and not durable_local:
        try:
            from worker_agent.inventory import execute_device_action

            # ADB/aapt2/screenshot actions are blocking subprocess work. Keep
            # them off the FastAPI event loop so unrelated UI controls and
            # WebSockets remain responsive while Device Info is loading.
            if body.action in {"aoa_record", "aoa_record_input"}:
                action_payload = fenced_local_payload(repository, action_payload)
            result = await asyncio.to_thread(
                execute_device_action, body.action, requested, action_payload
            )
            return {"success": True, **result}
        except (ValueError, RuntimeError) as exc:
            # Worker 侧已分类的输入/操作错误使用 502，与远端命令路径一致。
            raise ApiError.upstream_failure(
                str(exc) or "Worker device action failed", service="worker") from exc
        except Exception as exc:
            message = record_internal_error(
                logger,
                "Worker device action failed",
                "local Worker device action raised an unexpected exception",
                context={"worker_id": body.worker_id, "action": body.action.value},
            )
            raise ApiError.upstream_failure(message, service="worker") from exc
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
            "initial_status": "accepted" if durable_local else "queued",
            "payload": {**action_payload, "devices": requested},
        })
    except Exception:
        if (action_payload.get("release_claim_on_terminal")
                and repository.get_command_by_operation(body.worker_id, operation_id) is None):
            repository.claims.release(claim_source, status="failed")
        raise

    if durable_local:
        try:
            started = repository.start_local_command(command["id"])
            if started:
                from worker_agent.inventory import execute_device_action

                threading.Thread(target=execute_local_operation,
                                 args=(repository, command, execute_device_action),
                                 name=f"LocalAction-{command['id']}", daemon=True).start()
        except Exception as exc:
            # SQLite busy / 线程耗尽等派发失败：显式置 failed 并释放本请求
            # 拥有的 claim，否则行会永久停在 accepted/running 且 claim 卡到
            # TTL（sweeper 只兜底重启孤儿，兜不住派发即失败）。
            message = record_internal_error(
                logger,
                "Local device action dispatch failed",
                "durable_local command could not start; marked failed",
                context={"worker_id": body.worker_id, "command_id": command["id"]},
            )
            repository.ack_command(body.worker_id, command["id"], {
                "status": "failed",
                "error": f"{message}; reconcile required",
            })
            if action_payload.get("release_claim_on_terminal"):
                repository.claims.release(claim_source, status="failed")
            raise ApiError.upstream_failure(message, service="worker") from exc
    return await _wait_for_command(repository, body.action, command)
