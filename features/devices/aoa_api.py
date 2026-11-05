"""AOA 工作台 API：Profile CRUD + probe/run-profile/recover-adb。

挂载在 devices 路由（features/cluster/api.py 聚合注册）：
- ``POST /api/devices/aoa/profiles``             创建 Profile
- ``GET  /api/devices/aoa/profiles``             列出当前账号 Profile
- ``PUT  /api/devices/aoa/profiles/{id}``        更新
- ``DELETE /api/devices/aoa/profiles/{id}``      删除
- ``POST /api/devices/aoa/probe``                检测设备 AOAv2 能力
- ``POST /api/devices/aoa/recover``              执行 Profile 并等待 ADB 恢复

执行统一走既有 device_action 通道（aoa_probe/aoa_sequence 已在
DeviceActionSpec 登记）：复用设备租约/claim/审计，不新建执行通道
（ADR 0022：继续使用已有设备租约）。
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect

from features.auth import principal_owner_id, require_human_principal, validate_websocket_request
from foundation import aoa_profile, cluster_port
from foundation.aoa_calibration import TouchMapping
from foundation.aoa_hid_mode import profile_hid_options
from foundation.cluster_port import get_local_worker_id
from foundation.error_model import ApiError

from . import scrcpy_stream
from .aoa_calibrations import calibration_store
from .aoa_mapping_service import replay_mapping_options
from .aoa_service import profile_store
from .operation_claims import acquire_device_operation_claim, release_device_operation_claim


router = APIRouter(prefix="/api/devices/aoa")


def _validate_commands_or_422(commands: list[str]) -> None:
    try:
        aoa_profile.parse_profile_commands(commands)
    except ValueError as exc:
        raise ApiError.invalid_semantics(str(exc)) from exc


def _int_or_422(body: dict[str, Any], key: str, *, default: int = 0) -> int:
    value = body.get(key)
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ApiError.malformed_request(f"{key} must be an integer") from exc


def _payload(body: dict[str, Any], *required: str) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise ApiError.malformed_request("request body must be a JSON object")
    missing = [key for key in required if not str(body.get(key) or "").strip()]
    if missing:
        raise ApiError.malformed_request(f"missing required fields: {', '.join(missing)}")
    return body


def _store_for(request: Request):
    user = require_human_principal(request)
    return user, profile_store()


@router.post("/profiles")
def create_profile(request: Request, body: dict[str, Any]):
    _user, store = _store_for(request)
    _payload(body, "name", "commands")
    commands = body.get("commands")
    if not isinstance(commands, list) or not commands:
        raise ApiError.malformed_request("commands must be a non-empty list of AOA actions")
    if len(commands) > aoa_profile.MAX_PROFILE_COMMANDS:
        raise ApiError.malformed_request(
            f"commands exceeds {aoa_profile.MAX_PROFILE_COMMANDS} actions")
    _validate_commands_or_422([str(item) for item in commands])
    profile = store.create_profile(
        principal_owner_id(request), str(body["name"]).strip(),
        str(body.get("description") or ""), [str(item) for item in commands],
        _int_or_422(body, "wait_adb_seconds"), metadata=body.get("metadata"))
    return {"success": True, "profile": profile}


@router.get("/profiles")
def list_profiles(request: Request):
    user, store = _store_for(request)
    include_all = user.role == "admin" and request.query_params.get("all") == "1"
    return {"success": True,
            "profiles": store.list_profiles(principal_owner_id(request), include_all)}


@router.get("/profiles/{profile_id}")
def get_profile(request: Request, profile_id: str):
    user, store = _store_for(request)
    include_all = user.role == "admin"
    return {"success": True,
            "profile": store.get_profile(profile_id, principal_owner_id(request), include_all)}


@router.put("/profiles/{profile_id}")
def update_profile(request: Request, profile_id: str, body: dict[str, Any]):
    _user, store = _store_for(request)
    commands = body.get("commands")
    if commands is not None and (
            not isinstance(commands, list)
            or len(commands) > aoa_profile.MAX_PROFILE_COMMANDS):
        raise ApiError.malformed_request(
            f"commands must be a list of at most {aoa_profile.MAX_PROFILE_COMMANDS} actions")
    if commands is not None:
        _validate_commands_or_422([str(item) for item in commands])
    profile = store.update_profile(
        profile_id, principal_owner_id(request),
        name=str(body["name"]).strip() if body.get("name") else None,
        description=str(body["description"]) if body.get("description") is not None else None,
        commands=[str(item) for item in commands] if commands is not None else None,
        wait_adb_seconds=(
            _int_or_422(body, "wait_adb_seconds")
            if body.get("wait_adb_seconds") is not None else None),
        metadata=body.get("metadata"))
    return {"success": True, "profile": profile}


@router.delete("/profiles/{profile_id}")
def delete_profile(request: Request, profile_id: str):
    _store_for(request)
    profile_store().delete_profile(profile_id, principal_owner_id(request))
    return {"success": True}


# ==================== 触摸标定记录（ADR 0026：持久化 + 复用匹配） ====================


@router.post("/calibrations")
def save_calibration(request: Request, body: dict[str, Any]):
    """保存一次触摸标定（Worker 标定结果的 mapping 持久化到 aoa_calibrations）。"""
    require_human_principal(request)
    mapping = body.get("mapping")
    if not isinstance(mapping, dict):
        raise ApiError.malformed_request("mapping is required")
    try:
        parsed = TouchMapping.from_dict(mapping)
    except ValueError as exc:
        raise ApiError.invalid_semantics(str(exc)) from exc
    label = str(body.get("label") or "")[:256]
    record = calibration_store().save_calibration(
        principal_owner_id(request), parsed, label)
    return {"success": True, "calibration": record}


@router.get("/calibrations")
def list_calibrations(request: Request):
    user = require_human_principal(request)
    include_all = user.role == "admin" and request.query_params.get("all") == "1"
    return {"success": True,
            "calibrations": calibration_store().list_calibrations(
                principal_owner_id(request), include_all)}


@router.delete("/calibrations/{calibration_id}")
def delete_calibration(request: Request, calibration_id: str):
    require_human_principal(request)
    calibration_store().delete_calibration(calibration_id, principal_owner_id(request))
    return {"success": True}


@router.post("/calibrations/match")
def match_calibration(request: Request, body: dict[str, Any]):
    """按设备环境键查询可复用的已验证标定（无匹配返回 matched=false）。"""
    require_human_principal(request)
    context = body.get("context")
    if not isinstance(context, dict):
        raise ApiError.malformed_request("context is required")
    record = calibration_store().find_matching(principal_owner_id(request), context)
    if record is None:
        return {"success": True, "matched": False,
                "fallback": {"mapping_policy": "direct_unverified",
                             "detail": "no verified calibration for this device "
                                       "environment; per-axis estimate applies"}}
    return {"success": True, "matched": True, "calibration": record}


@router.post("/probe")
async def probe(request: Request, body: dict[str, Any]):
    require_human_principal(request)
    _payload(body, "worker_id", "device")
    return await cluster_port.submit_device_action(request, {
        "worker_id": str(body["worker_id"]),
        "devices": [str(body["device"])],
        "action": "aoa_probe",
    })


@router.post("/recover")
async def recover(request: Request, body: dict[str, Any]):
    """执行 Profile（或内联 commands）并等待 ADB 恢复。"""
    require_human_principal(request)
    _payload(body, "worker_id", "device")
    commands = body.get("commands")
    wait_adb_seconds = _int_or_422(body, "wait_adb_seconds")
    metadata = body.get("metadata") or {}
    if body.get("profile_id"):
        profile = profile_store().get_profile(
            str(body["profile_id"]), principal_owner_id(request))
        commands = profile["commands"]
        metadata = profile.get("metadata") or {}
        wait_adb_seconds = wait_adb_seconds or int(profile["wait_adb_seconds"] or 0)
    if not commands:
        raise ApiError.malformed_request("profile_id or commands is required")
    commands = [str(item) for item in commands]
    # 执行前校验语法与上限，避免长等待后 Worker 端才失败。
    _validate_commands_or_422(commands)
    try:
        hid_options = profile_hid_options(metadata)
    except (ValueError, TypeError, AttributeError) as exc:
        raise ApiError.invalid_semantics(str(exc)) from exc
    payload = {
        "worker_id": str(body["worker_id"]),
        "devices": [str(body["device"])],
        "action": "aoa_sequence",
        "aoa_commands": commands,
        **hid_options,
        "wait_adb_seconds": max(wait_adb_seconds, 1),
        "playback_control_id": str(body.get("playback_control_id") or ""),
    }
    try:
        payload.update(replay_mapping_options(metadata, body.get("device_context"),
            principal_owner_id(request), hid_options["aoa_hid_touch_mode"]))
    except (ValueError, TypeError) as exc:
        raise ApiError.invalid_semantics(str(exc)) from exc
    if (payload.get("aoa_mapping_policy") == "unsupported"
            and any(c.kind in {"click", "longClick", "swipe"}
                    for c in aoa_profile.parse_profile_commands(commands))):
        raise ApiError.conflict("目标环境未匹配已验证映射，请采集目标设备环境并重新标定")
    return await cluster_port.submit_device_action(request, payload)


@router.get("/record/presets")
def record_presets(request: Request):
    """下发录制预设旅程（Web 端下拉的单一真值来源）。"""
    require_human_principal(request)
    return {"success": True, "presets": aoa_profile.PRESET_JOURNEYS}


@router.post("/record")
async def record(request: Request, body: dict[str, Any]):
    """在 userdebug 设备上录制 AOA 恢复旅程（ADR 0022）。

    流程：录制机（userdebug，ADB 可用）按 steps 导航——uiautomator 定位
    真实元素（多语言匹配）、实测标定触摸映射后换算 AOA 逻辑坐标
    （360×640）、逐点执行并记录等价 AOA 命令（含 sleep pacing）；可选先
    写 settings 跳过开机向导。返回的 ``commands`` 即可直接存为 Profile，
    供同机型 user 固件回放。
    """
    require_human_principal(request)
    _payload(body, "worker_id", "device")
    steps = body.get("steps")
    preset = str(body.get("preset") or "").strip()
    if preset:
        if steps:
            raise ApiError.malformed_request("provide either preset or steps, not both")
        journeys = aoa_profile.PRESET_JOURNEYS
        if preset not in journeys:
            raise ApiError.invalid_semantics(
                f"unknown preset {preset!r}; available: {', '.join(sorted(journeys))}")
        steps = journeys[preset]
    if not isinstance(steps, list) or not steps:
        raise ApiError.malformed_request("preset or steps is required")
    try:
        aoa_profile.parse_record_steps(steps)
    except ValueError as exc:
        raise ApiError.invalid_semantics(str(exc)) from exc
    calibration = body.get("calibrate_touch", False)
    if calibration not in (False, True, "after"):
        raise ApiError.invalid_semantics("calibrate_touch must be boolean or after")
    return await cluster_port.submit_device_action(request, {
        "worker_id": str(body["worker_id"]),
        "devices": [str(body["device"])],
        "action": "aoa_record",
        "aoa_record": {
            "steps": steps,
            "setup_wizard": bool(body.get("setup_wizard")),
            "tv": bool(body.get("tv")),
            "calibrate_touch": calibration,
            **({"hid_touch_mode": profile_hid_options(body["metadata"])["aoa_hid_touch_mode"]}
               if body.get("metadata") else {}),
        },
    })


@router.post("/record/input")
async def record_input(request: Request, body: dict[str, Any]):
    """Execute one interactive recorder input and return its AOA command."""
    require_human_principal(request)
    _payload(body, "worker_id", "device", "input")
    raw_input = body.get("input")
    try:
        normalized = aoa_profile.parse_record_input_action(raw_input)
    except ValueError as exc:
        raise ApiError.invalid_semantics(str(exc)) from exc
    return await cluster_port.submit_device_action(request, {
        "worker_id": str(body["worker_id"]),
        "devices": [str(body["device"])],
        "action": "aoa_record_input",
        "aoa_record": normalized,
    })


@router.post("/profiles/from-recording")
def create_profile_from_recording(request: Request, body: dict[str, Any]):
    """把录制结果保存为 Profile（同机型 user 固件回放用）。"""
    require_human_principal(request)
    _payload(body, "name", "commands")
    commands = body.get("commands")
    if not isinstance(commands, list) or not commands:
        raise ApiError.malformed_request("commands must be a non-empty list of AOA actions")
    if len(commands) > aoa_profile.MAX_PROFILE_COMMANDS:
        raise ApiError.malformed_request(
            f"commands exceeds {aoa_profile.MAX_PROFILE_COMMANDS} actions")
    _validate_commands_or_422([str(item) for item in commands])
    profile = profile_store().create_profile(
        principal_owner_id(request), str(body["name"]).strip(),
        str(body.get("description")
            or f"recorded from {body.get('model') or 'device'} "
               f"(Android {body.get('release') or '?'})"),
        [str(item) for item in commands],
        _int_or_422(body, "wait_adb_seconds"), metadata=body.get("metadata"))
    return {"success": True, "profile": profile}


# ==================== scrcpy 实时投屏（AOA 录制器显示通道） ====================
#
# 录制器投屏只做显示器：输入必须走 /record/input 生成 AOA 命令，scrcpy
# 以 control=false 纯显示模式启动（features/devices/scrcpy_stream.py）。
# 仅 Controller 本地 Worker（adb 在本机）可用；远程 Worker 返回 422。


def _stream_target(request: Request, body: dict[str, Any], serial: str) -> str:
    require_human_principal(request)
    if not str(serial or "").strip() or "/" in serial or ".." in serial:
        raise ApiError.malformed_request("invalid serial")
    worker_id = str(body.get("worker_id") or get_local_worker_id())
    if worker_id != get_local_worker_id():
        raise ApiError.invalid_semantics(
            "scrcpy 投屏仅在 Controller 本地 Worker 支持；远程设备请用截图通道")
    return worker_id


def _stream_access(request, serial):
    """Check session ownership and current device claims on every connection."""
    user = require_human_principal(request)
    session = scrcpy_stream.get_stream(serial)
    if session is not None and getattr(session, "owner_id", "") != user.resource_owner_id:
        raise ApiError.forbidden("投屏会话属于其他账号")
    claims = cluster_port.get_cluster_service().repository.claims.list_active(
        worker_id=get_local_worker_id())
    key = f"{get_local_worker_id()}:{serial}"
    if any(c["device_key"] == key and (
            c["owner_id"] != user.resource_owner_id
            or c.get("source_type") == "cluster-job") for c in claims):
        raise ApiError.conflict("设备正被其他操作占用")
    return user.resource_owner_id


@router.post("/record/stream/{serial}/start")
def start_record_stream(request: Request, serial: str, body: dict[str, Any]):
    """启动录制机实时投屏会话（复用已有会话）。"""
    _stream_target(request, body, serial)
    owner_id = _stream_access(request, serial)
    source, _records, error = acquire_device_operation_claim(
        request, [serial], "aoa-stream-start")
    if error is not None:
        return error
    try:
        info = scrcpy_stream.start_stream(serial, owner_id=owner_id)
    finally:
        release_device_operation_claim(source)
    return {"success": True, "serial": serial, **info}


@router.post("/record/stream/{serial}/stop")
def stop_record_stream(request: Request, serial: str, body: dict[str, Any]):
    """停止该设备的投屏会话（前端关闭投屏时调用）。"""
    _stream_target(request, body, serial)
    _stream_access(request, serial)
    return {"success": True, **scrcpy_stream.stop_stream(serial)}


@router.websocket("/record/stream/{serial}")
async def record_stream_ws(websocket: WebSocket, serial: str):
    """投屏数据通道：hello JSON + 原始 [12B 帧头+H.264] 二进制流。"""
    _user, close_code = validate_websocket_request(websocket)
    if close_code is not None:
        await websocket.close(code=close_code)
        return
    websocket.state.current_user = _user
    try:
        _stream_access(websocket, serial)
    except (ApiError, HTTPException):
        await websocket.close(code=4403)
        return
    session = scrcpy_stream.get_stream(serial)
    await websocket.accept()
    if session is None:
        await websocket.send_json({"type": "error", "error": "投屏会话不存在或已停止"})
        await websocket.close()
        return
    # 订阅返回队列订阅者 + 重放关键帧已入队；**先发 hello 再启动 pump**：
    # 否则 pump 与 hello 并发在同一条 WS 上 send（ASGI 并发 send 抛错），
    # pump 被 except 吞掉后数据永不上线（实机复现 8 秒未出画面）。
    sub = session.subscribe(websocket)
    pump_task = None
    try:
        await websocket.send_json({"type": "hello", **session.hello()})
        pump_task = asyncio.create_task(session.pump(sub))
        while True:
            # 客户端仅发 ping 保活；输入一律走 /record/input REST 通道。
            try:
                _stream_access(websocket, serial)
            except (ApiError, HTTPException):
                await websocket.close(code=4403)
                break
            try:
                message = await asyncio.wait_for(websocket.receive_text(), timeout=1)
            except asyncio.TimeoutError:
                continue
            if message == "ping":
                await websocket.send_json({"type": "pong"})
    except WebSocketDisconnect:
        pass
    finally:
        sub.alive = False
        if pump_task is not None:
            pump_task.cancel()
        session.unsubscribe(sub)


@router.post("/playback/{control_id}/control")
async def playback_control(request: Request, control_id: str, body: dict[str, Any]):
    """Control only the caller's persisted playback through its target Worker."""
    require_human_principal(request)
    action = str(body.get("action") or "").strip()
    if action not in {"pause", "resume", "cancel"}:
        raise ApiError.invalid_semantics("action must be pause, resume or cancel")
    if not control_id or len(control_id) > 64:
        raise ApiError.not_found("playback control handle not found")
    worker_id = str(body.get("worker_id") or "").strip()
    if not worker_id:
        raise ApiError.malformed_request("worker_id is required")
    return await cluster_port.control_aoa_playback(control_id, worker_id, principal_owner_id(request), action)
