"""Freeze the human-selected recovery profile before firmware is dispatched."""
from __future__ import annotations

import hashlib
import json

from features.auth import AGENT_ROLE, is_machine_principal, principal_owner_id, require_human_principal
from features.devices import profile_store, usbip_source_host_for_device
from foundation.aoa_hid_mode import profile_hid_options
from foundation.aoa_profile import parse_profile_commands
from foundation.aoa_recovery_policy import firmware_compatibility, require_verified_recovery, target_firmware_context
from foundation.error_model import ApiError


def preflight_firmware_recovery(request, cluster, worker_id, devices, profile_id, boot_wait_seconds=60):
    """Read-only readiness check before any upload, claim or firmware command."""
    worker_id = worker_id or cluster.config.local_worker_id
    if not worker_id:
        raise ApiError.dependency_unavailable("本机 Worker 尚未配置，无法核对 AOA 恢复条件")
    if worker_id != cluster.config.local_worker_id:
        worker = cluster.repository.get_worker(worker_id) or {}
        if worker.get("status") not in {"online", "busy", "draining"}:
            raise ApiError.conflict("目标 Worker 不在线，请刷新设备清单后重试")
    try:
        snapshot = freeze_firmware_recovery(request, cluster.repository, worker_id, devices, profile_id, boot_wait_seconds)
    except ApiError as exc:
        raise ApiError(exc.code, exc.message, details={"stage": "aoa_preflight", "profile_id": profile_id},
            next_actions=({"action": "edit_aoa_profile", "profile_id": profile_id, "page": "devices", "view": "aoa"},
                          {"action": "disable_aoa_recovery", "label": "关闭自动 AOA，仅烧写固件"})) from exc
    return {"success": True, "ready": True, "worker_id": worker_id,
            "profile_id": snapshot["profile_id"], "profile_name": snapshot["profile_name"],
            "profile_revision": snapshot["profile_revision"],
            "expected_firmware_sha256": (snapshot.get("firmware_compatibility") or {}).get("sha256", ""),
            "mapping_policy": snapshot["mapping_options"]["aoa_mapping_policy"]}


def freeze_firmware_recovery(request, repository, worker_id: str, devices: list[str],
                             profile_id: str, boot_wait_seconds=60) -> dict | None:
    if not profile_id:
        return None
    # ADR 0012 human-only，双层守卫：
    # ① require_human_principal 先完成认证（无效/被拒 Bearer 在此 401，
    #    原实现会放行 None 用户、把守卫后移到 principal_owner_id），
    #    并拒绝带 auth_method 标记的 agent/machine 主体；
    # ② 对直接构造的 principal（如内部调用/测试注入，无 auth_method
    #    标记），按角色与主体身份再拒一次。固件审批不包含 UI 操作。
    user = require_human_principal(request)
    if is_machine_principal(user) or user.role == AGENT_ROLE:
        raise ApiError.forbidden("烧写后 AOA 恢复需要人工管理员会话；固件审批不包含 UI 操作")
    if len(devices) != 1:
        raise ApiError.invalid_semantics("启用烧写后 AOA 恢复时，一次请选择一台设备")
    try:
        wait = int(boot_wait_seconds)
        if (isinstance(boot_wait_seconds, bool) or not isinstance(boot_wait_seconds, (str, int))
                or not 0 <= wait <= 600):
            raise ValueError()
    except (TypeError, ValueError) as exc:
        raise ApiError.invalid_semantics("AOA 启动等待必须是 0..600 秒的整数") from exc
    worker = repository.get_worker(worker_id) or {}
    caps = worker.get("capabilities") or {}
    if any(caps.get(key) is not True for key in (
        "aoa_identity_fencing_v1", "aoa_usb_hid_v1", "aoa_record_input_v1",
        "firmware_aoa_recovery_v1",
        "aoa_hid_modes_v1",
    )):
        raise ApiError.conflict("请更新目标 Worker 并确认 AOA、ADB 依赖可用后再烧写")
    device = repository.resolve_worker_device(worker_id, devices[0])
    port = str(((device or {}).get("properties") or {}).get("usb") or "")
    if not device or not port or device.get("transport") == "adb_proxy":
        raise ApiError.conflict("AOA 恢复需要目标 Worker 的本地 USB 设备和已确认的物理端口")
    if usbip_source_host_for_device(device["serial"]):
        raise ApiError.conflict("USB/IP 源端烧写暂不支持自动接续 AOA；请取消勾选或使用 Worker 本地 USB")
    profile = next((p for p in profile_store().list_profiles(principal_owner_id(request), False)
                    if p["id"] == profile_id), None)
    if not profile:
        raise ApiError.not_found("当前账号中未找到所选 AOA 配置，请刷新后重试")
    commands = list(profile["commands"])
    try:
        parse_profile_commands(commands)
        options = profile_hid_options(profile.get("metadata"))
    except ValueError as exc:
        raise ApiError.invalid_semantics(str(exc)) from exc
    if options["aoa_hid_session_mode"] == "per_command" and caps.get("aoa_per_command_v1") is not True:
        raise ApiError.conflict("Worker 不支持按单条方式回放，请更新 Worker 后再烧写")
    from features.devices import replay_mapping_options

    properties = (device or {}).get("properties") or {}
    context = dict(properties.get("aoa_context") or {})
    for key in ("model", "release", "display", "rotation", "fingerprint", "density", "kernel", "display_route"):
        if key not in context:
            context[key] = properties.get(key, (device or {}).get(key))
    context["release"] = context.get("release") or properties.get("android_version")
    try:
        context = target_firmware_context(profile.get("metadata") or {}, commands, context)
        mappings = replay_mapping_options(profile.get("metadata") or {}, context,
            principal_owner_id(request), options["aoa_hid_touch_mode"], commands=commands)
        require_verified_recovery(commands, mappings, profile.get("metadata") or {})
    except (ValueError, TypeError) as exc:
        raise ApiError.invalid_semantics(str(exc)) from exc
    if mappings["aoa_mapping_policy"] == "unsupported":
        raise ApiError.conflict("目标设备显示/固件环境未匹配已验证标定，请重新采集目标环境后再烧写")
    if mappings.get("aoa_touch_mapping") and caps.get("aoa_affine_mapping_v2") is not True:
        raise ApiError.conflict("Worker 标定映射协议过旧，请更新 Worker 后再烧写")
    if mappings.get("aoa_semantic_plan") and caps.get("aoa_semantic_v1") is not True:
        raise ApiError.conflict("Worker 不支持认证语义回放，请更新 Worker 后再烧写")
    if mappings.get("aoa_semantic_plan"):
        from foundation.aoa_semantics import require_capture_capability

        try:
            require_capture_capability(mappings, caps, device["serial"], port)
        except ValueError as exc:
            raise ApiError.conflict(str(exc)) from exc
    return {
        "profile_id": profile["id"], "profile_name": profile["name"],
        "profile_revision": profile.get("revision", ""),
        "commands": commands,
        "commands_sha256": hashlib.sha256(json.dumps(
            commands, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest(),
        "metadata": profile.get("metadata") or {},
        "wait_adb_seconds": max(1, int(profile.get("wait_adb_seconds") or 60)),
        "boot_wait_seconds": wait, "usb_port_path": port,
        # 标定记录随快照冻结（ADR 0026 流程：userdebug 标定 → 刷 user
        # build → 匹配标定记录 → AOA HID 恢复）：烧写后设备无 ADB,无法
        # 现场查询环境,必须在刷前把已验证 TouchMapping 一并冻结进快照。
        # 无匹配记录时显式标记 unverified,回放结果不得当作已验证坐标。
        "target_context": context,
        "firmware_compatibility": firmware_compatibility(profile.get("metadata") or {}, commands),
        "mapping_options": mappings,
        "touch_mapping": mappings.get("aoa_touch_mapping"),
    }


def prepare_local_firmware_recovery(request, client_id: str, devices: list[str],
                                    profile_id: str, boot_wait_seconds=60) -> dict | None:
    if not profile_id:
        return None
    from .api import service

    cluster = service()
    repository, worker_id = cluster.repository, cluster.config.local_worker_id
    snapshot = freeze_firmware_recovery(
        request, repository, worker_id, devices, profile_id, boot_wait_seconds)
    device = repository.resolve_worker_device(worker_id, devices[0])
    source = f"firmware:{client_id}"
    claim = repository.claims.active_claim(device["id"])
    if not claim or claim["source_id"] != source or claim["owner_id"] != client_id:
        raise ApiError.conflict("固件设备锁已变化，请刷新设备后重试")
    # Extend this exact burn's claim to its physical port and aliases. Generic
    # device actions still cannot borrow another operation's firmware claim.
    extra_source = f"firmware-aoa:{claim['id']}"
    aliases = [item for item in repository.alias_claim_targets(worker_id, device["id"])
               if item["device_key"] != device["id"]]
    acquired, extra = repository.claims.acquire(
        aliases, owner_id=client_id, username=claim["username"], source_type="firmware",
        source_id=extra_source, ttl_seconds=3600)
    if not acquired:
        raise ApiError.conflict("目标 USB 端口已被其他操作占用")
    records = [claim, *extra]
    return {"worker_id": worker_id, "device_id": device["id"], "snapshot": snapshot,
            "owner_id": principal_owner_id(request), "claim_source_id": source,
            "extra_claim_source_id": extra_source,
            "lease_tokens": repository.claim_fencing_tokens(records, claim["id"]),
            "operation_id": f"firmware-aoa:{claim['id']}"}


def release_local_firmware_recovery(prepared: dict | None) -> None:
    if prepared:
        from .api import service

        service().repository.claims.release(prepared["extra_claim_source_id"])


def validate_local_firmware_recovery(prepared: dict | None) -> None:
    if prepared:
        from .api import service
        from .device_action_operations import fenced_local_payload
        from .device_actions_api import validate_local_firmware_identity

        try:
            fenced_local_payload(service().repository, prepared)
        except RuntimeError as exc:
            raise ApiError.conflict(str(exc)) from exc
        validate_local_firmware_identity(prepared["device_id"], prepared["snapshot"]["usb_port_path"])


async def run_local_firmware_recovery(prepared: dict, on_event=None) -> dict:
    """Continue the frozen burn claim, with a durable receipt before HID input."""
    import asyncio

    from .api import service
    from .device_action_operations import execute_local_operation, fenced_local_payload, request_digest
    from .device_actions_api import execute_local_firmware_aoa

    repository = service().repository
    snapshot = prepared["snapshot"]
    payload = {"action": "aoa_sequence", "devices": [prepared["device_id"]],
               **profile_hid_options(snapshot.get("metadata")),
               "aoa_commands": snapshot["commands"], "usb_port_path": snapshot["usb_port_path"],
               "wait_adb_seconds": snapshot["wait_adb_seconds"],
               "post_flash_recovery": snapshot, "lease_tokens": prepared["lease_tokens"],
               "owner_id": prepared["owner_id"], "release_claim_on_terminal": False,
               "claim_source_id": prepared["claim_source_id"]}
    # 冻结在快照里的标定记录（可能为 None）：Worker 端 None → 按 unverified
    # 估算执行并在结果里如实标记；记录存在 → 坐标经映射动态转换。
    from foundation.aoa_calibration import mapping_options

    payload.update(mapping_options({**payload, **(snapshot.get("mapping_options") or {})}))
    from features.devices import validate_owned_semantic

    validate_owned_semantic(payload, prepared["owner_id"])
    fenced_local_payload(repository, payload)
    payload["request_sha256"] = request_digest(
        prepared["worker_id"], payload["devices"], payload, prepared["owner_id"])
    command = repository.create_command({
        "worker_id": prepared["worker_id"], "command_type": "device_action",
        "operation_id": prepared["operation_id"], "initial_status": "accepted", "payload": payload})
    try:
        started = repository.start_local_command(command["id"])
    except Exception:
        # SQLite busy 等：UPDATE 未生效，命令无人认领，走下方兜底。
        started = False
    if started:
        def execute(*args, **kwargs):
            original = kwargs.get("progress_event_callback")

            def report(event):
                if original:
                    original(event)
                if on_event:
                    on_event(event)

            return execute_local_firmware_aoa(*args, **{**kwargs, "progress_event_callback": report})

        await asyncio.to_thread(execute_local_operation, repository, command, execute)
    elif (repository.get_command(command["id"]) or {}).get("status") == "accepted":
        # 仍停留 accepted 且本进程未认领 = 派发失败（无人会执行它）：
        # 烧写已完成，显式置 failed 供 reconcile，不得让请求卡在 accepted。
        # burn claim 归烧写请求的 finally 管，此处不释放。
        repository.ack_command(prepared["worker_id"], command["id"], {
            "status": "failed",
            "error": "local AOA recovery dispatch failed; firmware already flashed, "
                     "replay the AOA recovery separately (reconcile required)",
        })
        return repository.get_command(command["id"])
    current = repository.get_command(command["id"])
    result = current.get("result") or {}
    success = (current["status"] == "completed" and result.get("success") is True
               and result.get("adb_recovered") is True)
    from foundation.aoa_recovery_status import recovery_failure_reason

    return {"success": success, "command_id": command["id"], "result": result,
            "error": "" if success else recovery_failure_reason(result, current.get("error") or ""),
            "reconcile_required": not success}
