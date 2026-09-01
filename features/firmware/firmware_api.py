import asyncio
import contextlib
import logging
import os
import re
import shlex
import time

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from features.auth import require_elevated_admin_when_auth_required
from features.devices import migrate_local_usbip_serial
from features.test_execution import get_default_suites_path
from features.users import get_client_username_from_request
from foundation.responses import error_response, success_response
from foundation.uploads import upload_temp_root

from . import chunk_uploads, runtime
from .api_helpers import (
    adb_proxy_devices as _adb_proxy_devices,
)
from .api_helpers import (
    normalize_firmware_devices as _normalize_firmware_devices,
)
from .api_helpers import (
    remote_file_exists as _remote_file_exists,
)
from .api_helpers import (
    resolve_gsi_remote_image as _resolve_gsi_remote_image,
)
from .fastboot_firmware import (
    FastbootFirmwareError as _FastbootFirmwareError,
)
from .fastboot_firmware import (
    run_usbip_fastboot_firmware as _run_usbip_fastboot_firmware,
)
from .firmware_validation import (
    FirmwareValidationResult,
    validate_local_update_image,
    validate_remote_update_image,
)
from .gsi_diagnostics import diagnose_gsi_burn_failure
from .gsi_transport import prepare_gsi_command, upload_gsi_assets
from .models import SNBurnRequest
from .partition_burn import PartitionBurnError as _PartitionBurnError
from .partition_burn import run_partition_burn as _run_partition_burn
from .upload_transport import upload_firmware_to_test_host as _upload_firmware_to_test_host
from .usbip_transport import (
    accept_direct_rockusb_loaders as _accept_direct_rockusb_loaders,
)
from .usbip_transport import (
    capture_rockusb_route_baseline as _capture_rockusb_route_baseline,
)
from .usbip_transport import (
    defer_usbip_firmware_reconnects_after_failure as _defer_usbip_reconnects,
)
from .usbip_transport import (
    device_flash_protocols as _device_flash_protocols,
)
from .usbip_transport import (
    diagnose_maskrom_reattach_failure as _diagnose_maskrom_reattach_failure,
)
from .usbip_transport import (
    notify_skipped_devices as _notify_skip,
)
from .usbip_transport import (
    partition_devices_by_flash_state as _partition_devices_by_flash_state,
)
from .usbip_transport import (
    pause_usbip_firmware_reconnects as _pause_usbip_reconnects,
)
from .usbip_transport import (
    prepare_usbip_firmware_routes as _prepare_usbip_firmware_routes,
)
from .usbip_transport import (
    reattach_usbip_after_rockusb_reset as _reattach_usbip_after_rockusb_reset,
)
from .usbip_transport import (
    resume_usbip_firmware_reconnects as _resume_usbip_reconnects,
)
from .usbip_transport import (
    schedule_usbip_mode_reconnect as _schedule_usbip_mode_reconnect,
)
from .usbip_transport import (
    wait_for_adb_devices as _wait_for_adb_devices,
)
from .usbip_transport import (
    wait_for_rockusb_loaders as _wait_for_rockusb_loaders,
)


logger = logging.getLogger(__name__)

# Loader→MaskROM 竞态输掉后（工具超时但传输已挂回）在同一请求内的自动
# 重跑次数上限；第二次工具从 MaskROM 起步，不再经历二次枚举竞态。
FIRMWARE_BURN_MAX_ATTEMPTS = 2

router = APIRouter()

UPLOAD_PROGRESS_EXPIRATION = 24 * 60 * 60
_FASTBOOT_OKAY_RE = re.compile(r"\s+OKAY\s+\[\s*[\d.]+s\]$")
_ANSI_ESCAPE_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
_FIRMWARE_CHUNK_ROOT = upload_temp_root("gms_firmware_uploads")
MAX_FIRMWARE_CHUNKS = chunk_uploads.MAX_FIRMWARE_CHUNKS


def _usbip_routes_for_devices(
    routes: list[dict], selected_devices: set[str],
) -> list[dict]:
    """Keep the BUSID/device pairing when a route serves mixed protocols."""
    selected = {str(device or "").strip() for device in selected_devices}
    if not selected:
        return []
    subset: list[dict] = []
    for route in routes or []:
        route_devices = [
            str(device or "").strip()
            for device in route.get("device_ids") or []
            if str(device or "").strip()
        ]
        route_busids = [
            str(busid or "").strip()
            for busid in route.get("busids") or []
            if str(busid or "").strip()
        ]
        if not route_devices or not route_busids:
            continue
        # resolve_usbip_flash_routes builds these lists from the same
        # assignment records, so their order is the stable device↔BUSID map.
        if len(route_devices) != len(route_busids):
            logger.error(
                "USB/IP route has mismatched device/BUSID lists: %s", route,
            )
            continue
        pairs = [
            (device, busid)
            for device, busid in zip(route_devices, route_busids)
            if device in selected
        ]
        if not pairs:
            continue
        filtered = dict(route)
        filtered["device_ids"] = [device for device, _busid in pairs]
        filtered["busids"] = [busid for _device, busid in pairs]
        bindings = route.get("bindings") or []
        if bindings:
            filtered["bindings"] = [
                dict(binding)
                for binding in bindings
                if str(binding.get("device_id") or "").strip() in selected
            ]
        subset.append(filtered)
    return subset


def _usbip_device_route_map(routes: list[dict]) -> dict[str, dict]:
    """Return the immutable physical route captured for each burn device."""
    mapped: dict[str, dict] = {}
    for route in routes or []:
        route_base = {
            "device_host": str(route.get("device_host") or "").strip(),
            "source_host": str(route.get("source_host") or "").strip(),
        }
        bindings = route.get("bindings") or []
        if bindings:
            for binding in bindings:
                device_id = str(binding.get("device_id") or "").strip()
                busid = str(binding.get("busid") or "").strip()
                if device_id and busid:
                    mapped[device_id] = {**route_base, **dict(binding)}
            continue
        for device_id, busid in zip(
            route.get("device_ids") or [], route.get("busids") or [],
        ):
            device_id = str(device_id or "").strip()
            busid = str(busid or "").strip()
            if device_id and busid:
                mapped[device_id] = {**route_base, "busid": busid}
    return mapped


def strip_ansi_codes(text: str) -> str:
    return _ANSI_ESCAPE_RE.sub("", text or "")


def _safe_upload_token(value: str) -> str:
    return chunk_uploads.safe_upload_token(value)


def _firmware_upload_session_dir(client_id: str, upload_id: str) -> str:
    return chunk_uploads.upload_session_dir(_FIRMWARE_CHUNK_ROOT, client_id, upload_id)


def _cleanup_expired_upload_sessions(client_id: str) -> None:
    chunk_uploads.cleanup_expired_upload_sessions(
        _FIRMWARE_CHUNK_ROOT,
        client_id,
        UPLOAD_PROGRESS_EXPIRATION,
    )


async def _handle_firmware_chunk_upload(form, client_id: str):
    return await chunk_uploads.handle_chunk_upload(
        form,
        client_id,
        _FIRMWARE_CHUNK_ROOT,
        runtime.global_state,
        UPLOAD_PROGRESS_EXPIRATION,
    )


async def _lock_devices(request: Request, client_id: str, devices: list, error_prefix="Devices occupied"):
    username = get_client_username_from_request(request)
    return await runtime.lock_firmware_devices(
        client_id=client_id,
        username=username,
        devices=devices,
        error_prefix=error_prefix,
    )


async def _reject_invalid_firmware(
    client_id: str,
    devices: list[str],
    firmware_name: str,
    validation: FirmwareValidationResult,
):
    if client_id in runtime.global_state.websocket_connections:
        with contextlib.suppress(Exception):
            await runtime.safe_websocket_send(client_id, {
                "type": "log_update",
                "log": validation.message,
                "log_type": "error",
            })
    if runtime.store_notification:
        runtime.store_notification(
            client_id,
            "Firmware validation failed",
            validation.message[:300],
            "error",
            "firmware",
            {"devices": devices, "firmware": firmware_name, "stage": "preflight"},
        )
    return error_response(validation.message, status_code=422)


@router.get("/api/burn/upload-progress")
async def get_firmware_upload_progress(request: Request):
    """Query firmware upload progress."""
    client_id = runtime.get_client_id_from_request(request)

    with runtime.global_state.firmware_upload_progress_lock:
        current_time = time.time()
        expired_clients = [
            cid for cid, data in runtime.global_state.firmware_upload_progress.items()
            if current_time - data["timestamp"] > UPLOAD_PROGRESS_EXPIRATION
        ]
        for cid in expired_clients:
            del runtime.global_state.firmware_upload_progress[cid]

        if client_id in runtime.global_state.firmware_upload_progress:
            progress_data = runtime.global_state.firmware_upload_progress[client_id]
            return JSONResponse(content={
                "in_progress": True,
                "progress": progress_data["progress"],
                "filename": progress_data["filename"],
                "uploaded_size": progress_data["uploaded_size"],
                "total_size": progress_data["total_size"],
                "stage": progress_data.get("stage", "uploading_to_server"),
                "upload_id": progress_data.get("upload_id", ""),
            })
        else:
            return JSONResponse(content={"in_progress": False})



@router.post("/api/burn/firmware")
async def burn_firmware(
    request: Request,
    h: str | None = Query(None),
    help: bool = Query(False),
    _admin=Depends(require_elevated_admin_when_auth_required),
):
    """Firmware burning - supports file upload."""
    resp = runtime.generate_help_or_continue(help, "POST", "/api/burn/firmware")
    if resp:
        return resp

    client_id = ""
    merged_firmware = None
    burn_lock_path = None
    burn_succeeded = False
    locked_devices: list[str] = []
    usbip_flash_routes: list[dict] = []
    usbip_reconnect_paused_devices: list[str] = []
    keep_usbip_reconnect_paused = False
    usbip_reconnect_after_finish = False
    loader_reattach_task = None
    try:
        client_id = runtime.get_client_id_from_request(request)

        form = await request.form()
        finalize_upload = str(form.get("finalize_upload") or "").strip().lower() in {
            "1", "true", "yes"
        }
        if finalize_upload:
            upload_id = str(form.get("upload_id") or "").strip()
            merged_firmware, staged_error = chunk_uploads.load_staged_upload(
                _FIRMWARE_CHUNK_ROOT,
                client_id,
                upload_id,
            )
            if not merged_firmware:
                return error_response(staged_error or "Firmware upload is not staged", 409)
            burn_lock_path = chunk_uploads.acquire_burn_lock(merged_firmware)
            if not burn_lock_path:
                return error_response("Firmware burn is already being finalized", 409)
        elif form.get("chunk_index") is not None or form.get("check_chunks") is not None:
            chunk_response, merged_firmware = await _handle_firmware_chunk_upload(form, client_id)
            if chunk_response is not None:
                return chunk_response

        devices_param = request.query_params.get("devices")
        if devices_param:
            raw_devices = devices_param.split(",")
        else:
            devices_str = form.get("devices")
            raw_devices = devices_str.split(",") if devices_str else []

        devices, invalid_devices = _normalize_firmware_devices(raw_devices)
        if invalid_devices:
            return error_response("Invalid device serial", status_code=400)

        if not devices:
            return error_response("No devices selected")
        proxy_devices = _adb_proxy_devices(devices)
        if proxy_devices:
            return error_response(
                "ADB Proxy远程设备不支持固件烧写，请在设备来源主机操作: "
                + ", ".join(proxy_devices),
                status_code=409,
            )

        locked_devices, lock_err = await _lock_devices(request, client_id, devices, "The following devices are occupied")
        if lock_err:
            return lock_err

        firmware_file = form.get("firmware_file")
        firmware_path = form.get("firmware_path", "").strip()
        # 烧写传输：auto（默认：Windows USB/IP 走 GPT + 两阶段 Fastboot，Ubuntu 本地 USB
        # 走 upgrade_tool uf）/ fastboot（显式要求 USB/IP Fastboot）/
        # partition（显式要求 USB/IP upgrade_tool DI 同会话分区烧写；
        # 要求 RID 存储就绪，RID 失败在 DB 前安全停止）/
        # uf（显式要求 Ubuntu 本地 upgrade_tool）/ transport-probe-force
        # （管理员诊断：只验证旧 RockUSB DB 重枚举，不写分区）。
        burn_mode = str(form.get("burn_mode", "auto")).strip().lower() or "auto"
        if burn_mode not in {
            "auto", "fastboot", "partition", "uf", "transport-probe-force",
        }:
            return error_response(
                "Invalid burn_mode, expected 'auto', 'fastboot', 'partition', "
                "'uf', or 'transport-probe-force'"
            )
        if merged_firmware:
            firmware_file = None
            firmware_path = merged_firmware["path"]

        if not firmware_file and not firmware_path:
            return error_response("Please upload a firmware file or provide a firmware path")

        local_tool = os.path.join(runtime.project_root, "tools", "upgrade_tool")
        if not os.path.exists(local_tool):
            return error_response(f"upgrade_tool not found: {local_tool}")

        # Staged browser uploads are local files, so reject malformed Rockchip
        # update images before spending time on SCP or rebooting a device.
        if merged_firmware:
            local_validation = await asyncio.to_thread(
                validate_local_update_image,
                local_tool,
                firmware_path,
            )
            if not local_validation.valid:
                return await _reject_invalid_firmware(
                    client_id,
                    devices,
                    merged_firmware["name"],
                    local_validation,
                )

        config = runtime.config_manager.load_config()
        async with runtime.ssh_manager.async_optional_connection(config) as ssh:
            if not ssh:
                return error_response("SSH connection failed")

            try:
                gms_suite_dir = get_default_suites_path(config)
                if firmware_file:
                    firmware_name = os.path.basename(firmware_file.filename or "").strip()
                    if not firmware_name:
                        return error_response("Invalid firmware filename")

                    firmware_stream = firmware_file.file
                    try:
                        firmware_stream.seek(0, os.SEEK_END)
                        firmware_size = firmware_stream.tell()
                        firmware_stream.seek(0)
                    except Exception as e:
                        return error_response(f"Failed to inspect firmware size: {e}")

                    if firmware_size <= 0:
                        return error_response("Uploaded firmware file is empty")

                    remote_firmware = os.path.join(gms_suite_dir, firmware_name)
                    await _upload_firmware_to_test_host(ssh, client_id, firmware_stream, remote_firmware, firmware_name, firmware_size)
                else:
                    firmware_name = (
                        merged_firmware["name"]
                        if merged_firmware
                        else os.path.basename(firmware_path.rstrip("/"))
                    )
                    remote_firmware = os.path.join(gms_suite_dir, firmware_name)
                    local_firmware_path = None

                    if firmware_path.startswith("/") or firmware_path.startswith("./"):
                        if await asyncio.to_thread(_remote_file_exists, ssh, firmware_path):
                            remote_firmware = firmware_path
                        elif os.path.exists(firmware_path):
                            local_firmware_path = firmware_path
                        else:
                            return error_response(f"Firmware not found: {firmware_path}")
                    elif os.path.exists(firmware_path):
                        local_firmware_path = firmware_path
                    else:
                        remote_candidate = os.path.join(gms_suite_dir, firmware_path)
                        if await asyncio.to_thread(_remote_file_exists, ssh, remote_candidate):
                            remote_firmware = remote_candidate
                        else:
                            return error_response(f"Firmware not found: {firmware_path}")

                    if local_firmware_path:
                        file_size = os.path.getsize(local_firmware_path)
                        if file_size <= 0:
                            return error_response("Firmware file is empty")
                        if not merged_firmware:
                            local_validation = await asyncio.to_thread(
                                validate_local_update_image,
                                local_tool,
                                local_firmware_path,
                            )
                            if not local_validation.valid:
                                return await _reject_invalid_firmware(
                                    client_id,
                                    devices,
                                    firmware_name,
                                    local_validation,
                                )
                        await _upload_firmware_to_test_host(
                            ssh,
                            client_id,
                            local_firmware_path,
                            remote_firmware,
                            firmware_name,
                            file_size,
                            upload_id=merged_firmware.get("upload_id", "") if merged_firmware else "",
                        )

                # Upload upgrade_tool only after the firmware source has been
                # validated. Missing paths must not reboot devices into loader.
                logger.info("[Firmware Burn] Uploading upgrade_tool...")
                # Use a private path so the platform does not overwrite a
                # potentially newer operator-managed upgrade_tool.
                remote_tool = os.path.join(gms_suite_dir, ".gms_upgrade_tool")

                import scp
                scp_client = scp.SCPClient(ssh.get_transport())
                scp_client.put(local_tool, remote_tool)
                scp_client.close()

                # Validate the exact remote bytes that will be burned. This
                # catches both invalid loader packages and transport damage.
                remote_validation = await asyncio.to_thread(
                    validate_remote_update_image,
                    runtime.ssh_manager,
                    ssh,
                    remote_tool,
                    remote_firmware,
                )
                if not remote_validation.valid:
                    return await _reject_invalid_firmware(
                        client_id,
                        devices,
                        firmware_name,
                        remote_validation,
                    )

                usbip_flash_routes, usbip_route_error = (
                    await _prepare_usbip_firmware_routes(devices)
                )
                if usbip_route_error:
                    return error_response(usbip_route_error, status_code=409)
                transport_probe = burn_mode == "transport-probe-force"
                if transport_probe and (
                    len(devices) != 1 or not usbip_flash_routes
                ):
                    return error_response(
                        "transport-probe-force 仅支持单台已分配的 USB/IP 设备",
                        status_code=409,
                    )

                protocols = await asyncio.to_thread(
                    _device_flash_protocols,
                    ssh,
                    devices,
                )
                unavailable = [
                    device for device in devices if not protocols.get(device)
                ]
                if unavailable and usbip_flash_routes:
                    direct_loader_cmd = (
                        f"cd {shlex.quote(gms_suite_dir)} && "
                        f"{shlex.quote(remote_tool)} ld"
                    )
                    loader_out, loader_err, _loader_code = await asyncio.to_thread(
                        runtime.ssh_manager.execute_command,
                        ssh,
                        direct_loader_cmd,
                        timeout=5,
                    )
                    protocols = _accept_direct_rockusb_loaders(
                        protocols,
                        usbip_flash_routes,
                        loader_out or loader_err,
                    )
                    unavailable = [
                        device for device in devices if not protocols.get(device)
                    ]
                if unavailable:
                    return error_response(
                        "设备未处于可烧写的 ADB/Fastboot/USB-IP RockUSB Loader 状态: "
                        + ", ".join(unavailable),
                        status_code=409,
                    )

                routed_usbip_devices = {
                    str(device or "").strip()
                    for route in usbip_flash_routes
                    for device in route.get("device_ids") or []
                    if str(device or "").strip()
                }
                if usbip_flash_routes and routed_usbip_devices != set(devices):
                    return error_response(
                        "不能在同一次固件任务中混合Windows USB/IP设备与Ubuntu"
                        "本地USB设备；请分开选择后重试。USB/IP设备使用Fastboot"
                        "后端，本地设备使用upgrade_tool后端。",
                        status_code=409,
                    )
                if burn_mode == "fastboot" and not usbip_flash_routes:
                    return error_response(
                        "fastboot固件模式仅用于已从Windows挂载到Controller的"
                        "USB/IP设备；Ubuntu本地USB设备请使用auto或uf。",
                        status_code=409,
                    )
                if burn_mode == "partition" and not usbip_flash_routes:
                    return error_response(
                        "partition固件模式仅用于已从Windows挂载到Controller的"
                        "USB/IP设备（upgrade_tool DI 同会话分区烧写）；"
                        "Ubuntu本地USB设备请使用auto或uf。",
                        status_code=409,
                    )
                if burn_mode == "uf" and usbip_flash_routes:
                    return error_response(
                        "Windows USB/IP设备禁止进入upgrade_tool Loader/DB烧写；"
                        "请使用auto或fastboot模式。Ubuntu本地USB设备仍使用uf。",
                        status_code=409,
                    )

                # Windows USB/IP firmware is deliberately isolated from the
                # local RockUSB backend.  It updates GPT and physical images
                # in bootloader Fastboot, then writes dynamic images in
                # Fastbootd; it never enters RockUSB Loader/DB/MaskROM.
                if usbip_flash_routes and burn_mode in {"auto", "fastboot"}:
                    rockusb_devices = [
                        device for device in devices
                        if protocols.get(device) == "rockusb-loader"
                    ]
                    if rockusb_devices:
                        return error_response(
                            "USB/IP Fastboot固件模式要求设备从ADB、Fastboot或"
                            "Fastbootd开始；当前设备已在RockUSB Loader，无法"
                            "安全自动返回Android: " + ", ".join(rockusb_devices),
                            status_code=409,
                        )

                    async def _fastboot_log(message: str) -> None:
                        if client_id in runtime.global_state.websocket_connections:
                            with contextlib.suppress(Exception):
                                await runtime.safe_websocket_send(client_id, {
                                    "type": "log_update",
                                    "log": message,
                                    "log_type": "info",
                                })

                    async def _fastboot_progress(percentage: float) -> None:
                        if client_id in runtime.global_state.websocket_connections:
                            with contextlib.suppress(Exception):
                                await runtime.safe_websocket_send(client_id, {
                                    "type": "firmware_progress",
                                    "percentage": round(float(percentage), 2),
                                })

                    usbip_device_routes = _usbip_device_route_map(
                        usbip_flash_routes
                    )

                    async def _migrate_flashed_serial(
                        old_serial: str, new_serial: str, route: dict,
                    ) -> None:
                        migrated, migration_error = await asyncio.to_thread(
                            migrate_local_usbip_serial,
                            device_host=str(route.get("device_host") or ""),
                            busid=str(route.get("busid") or ""),
                            old_serial=old_serial,
                            new_serial=new_serial,
                            expected_generation=(
                                int(route["generation"])
                                if route.get("generation") is not None else None
                            ),
                            expected_operation_id=str(
                                route.get("operation_id") or ""
                            ),
                        )
                        if not migrated:
                            raise _FastbootFirmwareError(
                                "固件已启动，但USB/IP序列号迁移失败: "
                                + (migration_error or "unknown error")
                            )

                    await _fastboot_log(
                        "Starting USB/IP firmware burn (GPT + two-stage Fastboot mode)..."
                    )
                    try:
                        fastboot_result = await _run_usbip_fastboot_firmware(
                            ssh,
                            suite_dir=gms_suite_dir,
                            remote_tool=remote_tool,
                            remote_firmware=remote_firmware,
                            devices=devices,
                            usbip_device_routes=usbip_device_routes,
                            on_transport_reset=_schedule_usbip_mode_reconnect,
                            on_android_serial_changed=_migrate_flashed_serial,
                            on_log=_fastboot_log,
                            on_progress=_fastboot_progress,
                        )
                    except _FastbootFirmwareError as exc:
                        runtime.store_notification(
                            client_id,
                            "USB/IP Fastboot firmware burn failed",
                            str(exc)[:300],
                            "error",
                            "firmware",
                            {"devices": devices, "firmware": firmware_name},
                        )
                        return error_response(
                            str(exc), status_code=exc.status_code,
                        )

                    completed_devices = [
                        str(item.get("device") or "")
                        for item in fastboot_result.get("results") or []
                        if str(item.get("device") or "")
                    ] or devices
                    await _fastboot_log("USB/IP Fastboot firmware burn complete!")
                    runtime.store_notification(
                        client_id,
                        "USB/IP Fastboot firmware burn complete",
                        f"Devices: {', '.join(completed_devices)}",
                        "success",
                        "firmware",
                        {
                            "devices": completed_devices,
                            "firmware": firmware_name,
                            "backend": "usbip-fastboot",
                        },
                    )
                    if client_id in runtime.global_state.websocket_connections:
                        with contextlib.suppress(Exception):
                            await runtime.safe_websocket_send(client_id, {
                                "type": "firmware_burn_complete",
                                "devices": completed_devices,
                                "success": True,
                                "backend": "usbip-fastboot",
                            })
                    burn_succeeded = True
                    return success_response(
                        data=fastboot_result,
                        message="USB/IP Fastboot firmware burn completed successfully",
                    )

                # Rockchip update.img is written by upgrade_tool in Loader
                # mode. A device selected in Fastboot first returns to Android
                # so the established `adb reboot loader` path remains valid.
                # Both USB identity changes are coordinated with USB/IP.
                fastboot_devices = [
                    device
                    for device, protocol in protocols.items()
                    if protocol == "fastboot"
                ]
                for device in fastboot_devices:
                    cmd = f"fastboot -s {shlex.quote(device)} reboot"
                    await asyncio.to_thread(
                        runtime.ssh_manager.execute_command,
                        ssh,
                        cmd,
                        timeout=15,
                    )
                    _schedule_usbip_mode_reconnect(device, "adb")

                if fastboot_devices:
                    adb_ready, observed = await _wait_for_adb_devices(
                        ssh,
                        fastboot_devices,
                    )
                    if not adb_ready:
                        return error_response(
                            "Fastboot 设备重启后未恢复 ADB，无法自动进入 Loader；"
                            "请确认系统可正常启动，或手动进入 Loader/MaskROM 后重试。"
                            f" 设备: {', '.join(fastboot_devices)}；"
                            f"当前 ADB: {', '.join(observed) or '无'}",
                            status_code=409,
                        )

                # Firmware owns the selected routes before issuing the reboot.
                # The generic disappearance monitor otherwise starts its own
                # usbip attach loop as soon as ADB vanishes, racing the
                # Loader→MaskROM re-enumeration on the same Windows BUSID.
                loader_transition_devices = {
                    device for device in devices
                    if protocols.get(device) != "rockusb-loader"
                }
                loader_transition_routes = _usbip_routes_for_devices(
                    usbip_flash_routes,
                    loader_transition_devices,
                )
                loader_transition_baseline = {}
                if usbip_flash_routes:
                    (
                        usbip_reconnect_paused_devices,
                        reconnect_pause_error,
                    ) = await _pause_usbip_reconnects(
                        usbip_flash_routes
                    )
                    if reconnect_pause_error:
                        return error_response(
                            reconnect_pause_error,
                            status_code=409,
                        )
                    if loader_transition_routes:
                        (
                            loader_transition_baseline,
                            baseline_error,
                        ) = await _capture_rockusb_route_baseline(
                            loader_transition_routes
                        )
                        if baseline_error:
                            return error_response(
                                baseline_error,
                                status_code=409,
                            )
                        # Arm the firmware-owned watcher before ADB is told to
                        # reboot. The ADB row (2207:0006) can be atomically
                        # replaced by the Loader row (2207:351a) between two
                        # polls, so absence alone is not a reliable signal
                        # here; a stable PnP identity change also counts.
                        loader_reattach_task = asyncio.create_task(
                            _reattach_usbip_after_rockusb_reset(
                                ssh,
                                loader_transition_routes,
                                timeout=120,
                                baseline=loader_transition_baseline,
                                allow_identity_transition=True,
                                require_forced=transport_probe,
                            )
                        )
                        await asyncio.sleep(0)

                # Enter Loader mode. During this transition the watcher above
                # is the only component allowed to reattach the USB/IP route.
                try:
                    for device in devices:
                        if protocols.get(device) == "rockusb-loader":
                            continue
                        cmd = f"adb -s {shlex.quote(device)} reboot loader"
                        await asyncio.to_thread(
                            runtime.ssh_manager.execute_command,
                            ssh,
                            cmd,
                            timeout=5,
                        )
                    if loader_reattach_task is not None:
                        loader_transition_result = await loader_reattach_task
                        loader_reattach_task = None
                        if not loader_transition_result.get("success"):
                            transition_errors = loader_transition_result.get(
                                "errors"
                            ) or {}
                            transition_detail = "; ".join(
                                f"{key}: {str(value).strip()[:160]}"
                                for key, value in transition_errors.items()
                            )
                            return error_response(
                                "ADB→Loader 后 USB/IP 未能重新挂载"
                                + (
                                    f"（{transition_detail}）"
                                    if transition_detail else ""
                                )
                                + "，烧写已停止，请检查 Windows USB 枚举状态后重试。",
                                status_code=409,
                            )
                except BaseException:
                    if loader_reattach_task is not None:
                        loader_reattach_task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await loader_reattach_task
                        loader_reattach_task = None
                    raise

                # Check Loader devices
                quoted_suite_dir = shlex.quote(gms_suite_dir)
                quoted_remote_tool = shlex.quote(remote_tool)
                check_cmd = f"cd {quoted_suite_dir} && {quoted_remote_tool} ld"
                loader_ready, output = await _wait_for_rockusb_loaders(
                    ssh,
                    check_cmd,
                    len(devices),
                )

                if not loader_ready:
                    return error_response(f"No Loader devices detected. Output:\n{output}", status_code=409)

                # USB/IP 设备显式 partition 模式改走同会话分区烧写：uf 在
                # Download Boot 后强制 Loader→MaskROM 二次枚举，该窗口在
                # usbipd-win 来源端口上实测不可依赖（0000:0002 描述符失败），
                # 且故障发生在 Windows 枚举层、任何 USB/IP 重试都无法触及。
                # 分区烧写全程停留在当前 Loader 会话，中途不再发生 USB 身份
                # 切换（RID 失败时在 DB 前安全停止）。
                if usbip_flash_routes and burn_mode in {
                    "partition", "transport-probe-force",
                }:
                    async def _partition_log(message: str) -> None:
                        if client_id in runtime.global_state.websocket_connections:
                            with contextlib.suppress(Exception):
                                await runtime.safe_websocket_send(client_id, {"type": "log_update", "log": message, "log_type": "info"})

                    async def _partition_progress(percentage: float) -> None:
                        if client_id in runtime.global_state.websocket_connections:
                            with contextlib.suppress(Exception):
                                await runtime.safe_websocket_send(client_id, {"type": "firmware_progress", "percentage": round(float(percentage), 2)})

                    if client_id in runtime.global_state.websocket_connections:
                        with contextlib.suppress(Exception):
                            await runtime.safe_websocket_send(client_id, {"type": "log_update", "log": "Starting firmware burn (partition mode)...", "log_type": "info"})

                    try:
                        _partition_result = await _run_partition_burn(
                            ssh,
                            suite_dir=gms_suite_dir,
                            remote_tool=remote_tool,
                            remote_firmware=remote_firmware,
                            usbip_routes=usbip_flash_routes,
                            on_log=_partition_log,
                            on_progress=_partition_progress,
                            transport_probe=transport_probe,
                            force_usbip_bind=transport_probe,
                        )
                    except _PartitionBurnError as exc:
                        runtime.store_notification(
                            client_id,
                            "Firmware burn failed",
                            str(exc)[:300],
                            "error",
                            "firmware",
                            {"devices": devices, "firmware": firmware_name},
                        )
                        detail = str(exc)
                        logger.warning(
                            "[Firmware Burn] partition burn failed: %s", detail,
                        )
                        return error_response(detail, status_code=exc.status_code)
                    usbip_reconnect_after_finish = True
                    if transport_probe:
                        burn_succeeded = True
                        return success_response(
                            message=(
                                "USB/IP DB transport probe completed successfully; "
                                "no partitions were written"
                            )
                        )
                    if client_id in runtime.global_state.websocket_connections:
                        with contextlib.suppress(Exception):
                            await runtime.safe_websocket_send(client_id, {"type": "firmware_progress", "percentage": 100})
                            await runtime.safe_websocket_send(client_id, {"type": "log_update", "log": "Firmware burn complete!", "log_type": "success"})
                    runtime.store_notification(client_id, "Firmware burn complete", f"Devices: {', '.join(devices)}", "success", "firmware", {"devices": devices, "firmware": firmware_name})
                    if client_id in runtime.global_state.websocket_connections:
                        with contextlib.suppress(Exception):
                            await runtime.safe_websocket_send(client_id, {"type": "firmware_burn_complete", "devices": devices, "success": True})
                    burn_succeeded = True
                    return success_response(message="Firmware burn completed successfully")

                rockusb_baseline = {}
                if usbip_flash_routes:
                    rockusb_baseline, baseline_error = (
                        await _capture_rockusb_route_baseline(
                            usbip_flash_routes
                        )
                    )
                    if baseline_error:
                        return error_response(baseline_error, status_code=409)

                # Burn firmware
                burn_cmd = f"cd {quoted_suite_dir} && {quoted_remote_tool} uf {shlex.quote(remote_firmware)}"

                if client_id in runtime.global_state.websocket_connections:
                    with contextlib.suppress(Exception):
                        await runtime.safe_websocket_send(client_id, {"type": "log_update", "log": "Starting firmware burn...", "log_type": "info"})

                # upgrade_tool 自身的 MaskROM 等待窗口实测仅 6~14 秒，可能短于
                # 设备在 Windows 侧重新可导出的耗时。若工具输给该竞态但 USB/IP
                # 传输随后已挂回（设备停留在 MaskROM），在同一请求内自动重跑
                # 一次：第二次工具直接从 MaskROM 起步，不再经历 Loader→MaskROM
                # 的二次枚举竞态（实测第二击即成功）。
                for burn_attempt in range(1, FIRMWARE_BURN_MAX_ATTEMPTS + 1):
                    # 必须在启动 upgrade_tool 之前布防 watcher。等 stdout
                    # 出现 "Download Boot Start" 后再建 Windows SSH，会白白
                    # 消耗工具仅数秒的 MaskROM 等待窗口。watcher 先完成 SSH
                    # 布防，直到 Download Boot Success 才进入 ARMED，因此
                    # 不会把旧 Loader 会话提前当成 MaskROM。
                    rockusb_reattach_task = None
                    maskrom_transition_event = asyncio.Event()
                    maskrom_watcher_ready = asyncio.Event()
                    if usbip_flash_routes:
                        rockusb_reattach_task = asyncio.create_task(
                            _reattach_usbip_after_rockusb_reset(
                                ssh,
                                usbip_flash_routes,
                                timeout=15,
                                baseline=rockusb_baseline,
                                transition_event=maskrom_transition_event,
                                ready_event=maskrom_watcher_ready,
                            )
                        )
                        try:
                            await asyncio.wait_for(
                                maskrom_watcher_ready.wait(), timeout=15,
                            )
                        except TimeoutError:
                            rockusb_reattach_task.cancel()
                            with contextlib.suppress(
                                asyncio.CancelledError
                            ):
                                await rockusb_reattach_task
                            return error_response(
                                "USB/IP MaskROM watcher 未能在烧写前完成 "
                                "Windows SSH 布防",
                                status_code=504,
                            )
                    try:
                        _stdin, stdout, stderr = await asyncio.to_thread(
                            lambda: ssh.exec_command(
                                burn_cmd, get_pty=True, timeout=300,
                            )
                        )
                    except BaseException:
                        if rockusb_reattach_task is not None:
                            rockusb_reattach_task.cancel()
                            with contextlib.suppress(asyncio.CancelledError):
                                await rockusb_reattach_task
                        raise
                    output_buffer = []

                    firmware_burn_start = False
                    current_progress = 0
                    last_progress_time = 0
                    transition_output_tail = ""

                    while not stdout.channel.exit_status_ready():
                        current_time = asyncio.get_event_loop().time()

                        if stdout.channel.recv_ready():
                            chunk = (await asyncio.to_thread(stdout.channel.recv, 1024)).decode("utf-8", errors="ignore")
                            output_buffer.append(chunk)
                            clean_chunk = strip_ansi_codes(chunk)
                            transition_output_tail = (
                                transition_output_tail + clean_chunk
                            )[-512:]
                            if (
                                "Download Boot Success"
                                in transition_output_tail
                                or "Wait For Maskrom Start"
                                in transition_output_tail
                            ):
                                maskrom_transition_event.set()

                            if "Download Firmware Start" in clean_chunk and not firmware_burn_start:
                                firmware_burn_start = True
                                current_progress = 0
                                last_progress_time = current_time

                            if client_id in runtime.global_state.websocket_connections:
                                try:
                                    for line in clean_chunk.split("\n"):
                                        line = line.strip()
                                        if line:
                                            if firmware_burn_start:
                                                if any(kw in line.lower() for kw in ["error", "failed", "fail"]):
                                                    await runtime.safe_websocket_send(client_id, {"type": "log_update", "log": line, "log_type": "error"})
                                                continue
                                            await runtime.safe_websocket_send(client_id, {"type": "log_update", "log": line, "log_type": "info"})
                                except Exception as e:
                                    logger.error(f"[Firmware Burn] Log send failed: {e}")

                        if firmware_burn_start and (current_time - last_progress_time > runtime.gsi_progress_poll_interval):
                            current_progress = min(current_progress + runtime.gsi_progress_increment, runtime.gsi_progress_max)
                            last_progress_time = current_time

                            if client_id in runtime.global_state.websocket_connections:
                                with contextlib.suppress(Exception):
                                    await runtime.safe_websocket_send(client_id, {"type": "firmware_progress", "percentage": current_progress})

                        await asyncio.sleep(0.1)

                    # exit_status_ready 置位时，进程最后一段输出可能仍在 channel
                    # 缓冲中未被 recv_ready 消费（失败诊断恰好依赖末尾输出，例如
                    # "Download Boot Fail"）。补一次 drain 保证 final_output 完整。
                    while stdout.channel.recv_ready():
                        chunk = (await asyncio.to_thread(stdout.channel.recv, 1024)).decode("utf-8", errors="ignore")
                        output_buffer.append(chunk)
                        transition_output_tail = (
                            transition_output_tail + strip_ansi_codes(chunk)
                        )[-512:]
                        if (
                            "Download Boot Success" in transition_output_tail
                            or "Wait For Maskrom Start" in transition_output_tail
                        ):
                            maskrom_transition_event.set()
                        if client_id in runtime.global_state.websocket_connections:
                            with contextlib.suppress(Exception):
                                for drain_line in strip_ansi_codes(chunk).split("\n"):
                                    drain_line = drain_line.strip()
                                    if drain_line:
                                        await runtime.safe_websocket_send(client_id, {"type": "log_update", "log": drain_line, "log_type": "info"})

                    final_output = "".join(output_buffer)
                    exit_status = stdout.channel.recv_exit_status()
                    if exit_status == 0:
                        error_output = ""
                        stderr_output = ""
                    else:
                        # 失败诊断不能只看 stdout：upgrade_tool 的真实错误（如
                        # loader 下载失败）常在 stderr，与 GSI 路径一致合并两者。
                        stderr_output = (
                            await asyncio.to_thread(stderr.read)
                        ).decode("utf-8", errors="ignore")
                        error_output = "\n".join(
                            part for part in (final_output, stderr_output) if part
                        )
                    clean_output = strip_ansi_codes(error_output).strip()
                    clean_final_output = strip_ansi_codes(final_output)
                    if (
                        "Download Boot Success" in clean_final_output
                        or "Wait For Maskrom Start" in clean_final_output
                    ):
                        maskrom_transition_event.set()
                    rockusb_reattach_result = {
                        "success": not usbip_flash_routes,
                        "attached": [],
                    }
                    if rockusb_reattach_task is not None:
                        try:
                            if (
                                exit_status != 0
                                and "Wait For Maskrom Start" in clean_output
                            ):
                                # upgrade_tool may lose its short MaskROM
                                # window before Windows PnP is safe to attach.
                                # Let the watcher finish so a bounded retry can
                                # start directly from the restored transport.
                                rockusb_reattach_result = (
                                    await rockusb_reattach_task
                                )
                            else:
                                # A successful burn (or an unrelated failure)
                                # must not wait for a transition that never
                                # occurred, especially on the retry path.
                                rockusb_reattach_task.cancel()
                                with contextlib.suppress(
                                    asyncio.CancelledError
                                ):
                                    await rockusb_reattach_task
                        except Exception as exc:
                            logger.warning(
                                "[Firmware Burn] RockUSB USB/IP reattach failed: %s",
                                exc,
                            )
                            rockusb_reattach_result = {
                                "success": False,
                                "attached": [],
                                "errors": {"watcher": str(exc)},
                            }

                    if exit_status == 0:
                        usbip_reconnect_after_finish = bool(
                            usbip_flash_routes
                        )
                        if client_id in runtime.global_state.websocket_connections:
                            with contextlib.suppress(Exception):
                                await runtime.safe_websocket_send(client_id, {"type": "firmware_progress", "percentage": 100})
                            with contextlib.suppress(Exception):
                                await runtime.safe_websocket_send(client_id, {"type": "log_update", "log": "Firmware burn complete!", "log_type": "success"})

                        runtime.store_notification(client_id, "Firmware burn complete", f"Devices: {', '.join(devices)}", "success", "firmware", {"devices": devices, "firmware": firmware_name})
                        # 通知前端刷新 ADB 设备状态；设备锁由 finally 统一释放。
                        if client_id in runtime.global_state.websocket_connections:
                            with contextlib.suppress(Exception):
                                await runtime.safe_websocket_send(client_id, {"type": "firmware_burn_complete", "devices": devices, "success": True})
                        burn_succeeded = True
                        return success_response(message="Firmware burn completed successfully")

                    # ---- 失败路径 ----
                    if (
                        burn_attempt < FIRMWARE_BURN_MAX_ATTEMPTS
                        and usbip_flash_routes
                        and "Wait For Maskrom Fail" in clean_output
                    ):
                        # watcher.success 只说明 USB/IP 传输层看起来已挂载；
                        # upgrade_tool ld 能发现 RockUSB 才是第二次 uf 是否
                        # 可执行的权威判据。
                        retry_loader_ready, retry_loader_output = (
                            await _wait_for_rockusb_loaders(
                                ssh,
                                check_cmd,
                                len(devices),
                                timeout=15,
                                interval=1,
                            )
                        )
                        if retry_loader_ready:
                            logger.info(
                                "[Firmware Burn] RockUSB confirmed by ld after "
                                "MaskROM timeout; retrying burn (attempt %s)",
                                burn_attempt + 1,
                            )
                            if client_id in runtime.global_state.websocket_connections:
                                with contextlib.suppress(Exception):
                                    await runtime.safe_websocket_send(client_id, {"type": "log_update", "log": "upgrade_tool ld 已确认 MaskROM 传输恢复，自动重试烧写...", "log_type": "info"})
                            continue
                        logger.warning(
                            "[Firmware Burn] USB/IP watcher result was %s, but "
                            "upgrade_tool ld did not find RockUSB: %s",
                            rockusb_reattach_result,
                            retry_loader_output,
                        )

                    if client_id in runtime.global_state.websocket_connections:
                        try:
                            await runtime.safe_websocket_send(client_id, {"type": "log_update", "log": f"Firmware burn failed (exit code: {exit_status})", "log_type": "error"})
                            if error_output and len(error_output) < 500:
                                await runtime.safe_websocket_send(client_id, {"type": "log_update", "log": f"Error: {error_output[:200]}", "log_type": "error"})
                        except Exception:
                            pass

                    runtime.store_notification(client_id, "Firmware burn failed", (error_output or "Burn failed")[:300], "error", "firmware", {"devices": devices, "firmware": firmware_name, "exit_status": exit_status})
                    burn_error_msg = f"Firmware burn failed (exit code: {exit_status})"
                    if clean_output:
                        burn_error_msg += f": {clean_output[:300]}"
                    descriptor_enumeration_failed = False
                    if (
                        "Wait For Maskrom Start" in clean_output
                        and not rockusb_reattach_result.get("success")
                    ):
                        logger.warning(
                            "[Firmware Burn] MaskROM USB/IP reattach result: %s",
                            rockusb_reattach_result,
                        )
                        attach_errors = rockusb_reattach_result.get("errors") or {}
                        attach_detail = "; ".join(
                            f"{key}: {str(value).strip()[:120]}"
                            for key, value in attach_errors.items()
                        )
                        descriptor_enumeration_failed = any(
                            "descriptor enumeration failed" in str(value).lower()
                            or "0000:0002" in str(value).lower()
                            for value in attach_errors.values()
                        )
                        if descriptor_enumeration_failed:
                            # Keep generic disappearance monitoring quiet after
                            # this request returns.  Otherwise its 30-attempt
                            # worker keeps cycling the same failed Windows port
                            # for roughly 15 minutes, preventing recovery.
                            keep_usbip_reconnect_paused = True
                        # 现场快照：Windows 侧 usbipd 设备表能直接回答
                        # "MaskROM 实例是否到达、是否被共享、是否枚举失败"。
                        source_snapshot = str(
                            rockusb_reattach_result.get("source_list") or ""
                        ).strip()
                        pending_busids = [
                            str(item.get("busid") or "")
                            for item in rockusb_reattach_result.get("pending") or []
                        ]
                        precise_diagnosis = _diagnose_maskrom_reattach_failure(
                            source_snapshot, pending_busids,
                        )
                        burn_error_msg += (
                            "；Loader切换MaskROM时USB/IP未能在等待窗口内重新挂载"
                            + (f"（{attach_detail[:300]}）" if attach_detail else "")
                            + "。"
                        )
                        if precise_diagnosis:
                            # 快照能定位到具体层面（Windows 枚举失败 / RockUSB
                            # 换了新 BUSID / 未共享）时给出精确结论，避免误导
                            # 性的 AutoBind/TCP 3240 通用提示。
                            burn_error_msg += f"诊断：{precise_diagnosis}"
                        else:
                            burn_error_msg += (
                                "请确认Windows usbipd-win>=4.2、来源BUSID的AutoBind策略"
                                "以及TCP 3240连接正常。"
                            )
                        # 快照明细完整保留（仅截断长度），不做行过滤——过滤会
                        # 漏掉枚举在其他 BUSID 上的设备，导致误判。
                        if source_snapshot:
                            excerpt = " / ".join(
                                line.strip()
                                for line in source_snapshot.splitlines()
                                if line.strip()
                            )[:500]
                            if excerpt:
                                burn_error_msg += f"；Windows usbipd现场: {excerpt}"
                    if usbip_flash_routes and not descriptor_enumeration_failed:
                        # 烧写失败后设备可能停留在 Loader/MaskROM 传输上；
                        # 在 finally 释放固件独占保护后再调度重连，避免该任务
                        # 与仍在收尾的 MaskROM watcher 竞争同一 BUSID。
                        usbip_reconnect_after_finish = True
                    return error_response(burn_error_msg, status_code=422)

            except Exception as e:
                if usbip_reconnect_paused_devices:
                    keep_usbip_reconnect_paused = True
                runtime.store_notification(client_id, "Firmware burn error", str(e)[:300], "error", "firmware", {"devices": devices, "firmware": firmware_name if 'firmware_name' in dir() else ""})
                return error_response(str(e))

    except Exception as e:
        if usbip_reconnect_paused_devices:
            keep_usbip_reconnect_paused = True
        import traceback
        logger.error(f"Error in burn_firmware: {e}")
        logger.error(f"Traceback: {traceback.format_exc()}")
        return error_response(str(e), 500)
    finally:
        if loader_reattach_task is not None and not loader_reattach_task.done():
            loader_reattach_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await loader_reattach_task
        if usbip_reconnect_paused_devices:
            if keep_usbip_reconnect_paused:
                _defer_usbip_reconnects(
                    usbip_reconnect_paused_devices
                )
            else:
                _resume_usbip_reconnects(
                    usbip_reconnect_paused_devices
                )
        if usbip_reconnect_after_finish and not keep_usbip_reconnect_paused:
            for device in devices:
                _schedule_usbip_mode_reconnect(device, "adb")
        if merged_firmware and client_id:
            chunk_uploads.clear_upload_progress(
                runtime.global_state,
                client_id,
                merged_firmware.get("upload_id", ""),
            )
        chunk_uploads.release_burn_lock(burn_lock_path)
        if burn_succeeded and merged_firmware:
            chunk_uploads.remove_staged_upload(merged_firmware)
        if locked_devices and client_id:
            with contextlib.suppress(Exception):
                await runtime.release_firmware_devices(client_id, locked_devices)

@router.post("/api/burn/gsi")
async def burn_gsi(
    request: Request,
    _admin=Depends(require_elevated_admin_when_auth_required),
):
    """GSI burning using run_GSI_Burn.sh script."""
    try:
        client_id = runtime.get_client_id_from_request(request)
        req_data = await request.json()
        devices = req_data.get("devices", [])
        script_path = req_data.get("script_path", "").strip()
        system_img = req_data.get("system_img", "").strip()
        vendor_img = req_data.get("vendor_img", "").strip()

        if not devices:
            return error_response("No devices selected")
        proxy_devices = _adb_proxy_devices(devices)
        if proxy_devices:
            return error_response(
                "ADB Proxy远程设备不支持GSI烧写，请在设备来源主机操作: "
                + ", ".join(proxy_devices),
                status_code=409,
            )
        if not script_path:
            return error_response("Script path is required")
        if not system_img and not vendor_img:
            return error_response("At least one of system image or vendor boot image is required")

        config = runtime.config_manager.load_config()
        async with runtime.ssh_manager.async_optional_connection(config) as ssh:
            if not ssh:
                return error_response("SSH connection failed")

            # 烧写前预检：允许设备从 ADB、bootloader Fastboot 或 Fastbootd
            # 开始；其他离线/未授权状态仍提前剔除。
            online_devices, offline_devices = await asyncio.to_thread(
                _partition_devices_by_flash_state, ssh, devices
            )
            if not online_devices:
                return error_response(
                    f"没有可烧写的 ADB/Fastboot 设备，离线/状态异常: {', '.join(offline_devices)}"
                )
            await _notify_skip(client_id, offline_devices)

            locked_devices, lock_err = await _lock_devices(request, client_id, online_devices)
            if lock_err:
                return lock_err

            # USB/IP 设备在 ADB→Fastboot→Fastbootd 切换时会重新枚举 USB 身份；
            # 与固件路径一致先做 AutoBind 预检，保证物理 BUSID 可被自动共享。
            _usbip_flash_routes, usbip_route_error = (
                await _prepare_usbip_firmware_routes(online_devices)
            )
            if usbip_route_error:
                await runtime.release_firmware_devices(client_id, locked_devices)
                return error_response(usbip_route_error, status_code=409)

            try:
                gms_suite_dir = get_default_suites_path(config)
                remote_script, resolved_misc, asset_error = await asyncio.to_thread(
                    upload_gsi_assets,
                    ssh=ssh,
                    ssh_manager=runtime.ssh_manager,
                    project_root=str(runtime.project_root),
                    suite_dir=gms_suite_dir,
                )
                if asset_error:
                    await runtime.release_firmware_devices(client_id, locked_devices)
                    return error_response(asset_error)

                resolved_system = ""
                if system_img:
                    resolved_system, system_error = await asyncio.to_thread(
                        _resolve_gsi_remote_image,
                        ssh,
                        gms_suite_dir,
                        system_img,
                        "System image",
                    )
                    if system_error:
                        await runtime.release_firmware_devices(client_id, locked_devices)
                        return error_response(system_error)

                remote_vendor = ""
                if vendor_img:
                    resolved_vendor, vendor_error = await asyncio.to_thread(
                        _resolve_gsi_remote_image,
                        ssh,
                        gms_suite_dir,
                        vendor_img,
                        "Vendor boot image",
                    )
                    if vendor_error:
                        await runtime.release_firmware_devices(client_id, locked_devices)
                        return error_response(vendor_error)
                    remote_vendor = resolved_vendor or ""

                results = []

                if client_id in runtime.global_state.websocket_connections:
                    with contextlib.suppress(Exception):
                        await runtime.safe_websocket_send(client_id, {"type": "log_update", "log": f"Starting GSI burn for {len(online_devices)} devices...", "log_type": "info"})

                for device in online_devices:
                    if client_id in runtime.global_state.websocket_connections:
                        with contextlib.suppress(Exception):
                            await runtime.safe_websocket_send(client_id, {"type": "log_update", "log": f"Burning device: {device}", "log_type": "info"})

                    try:
                        burn_cmd = await asyncio.to_thread(
                            prepare_gsi_command,
                            ssh=ssh,
                            ssh_manager=runtime.ssh_manager,
                            remote_script=remote_script,
                            device=device,
                            system_img=resolved_system,
                            misc_img=resolved_misc,
                            vendor_img=remote_vendor,
                            on_transport_reset=_schedule_usbip_mode_reconnect,
                        )
                    except Exception as prep_error:
                        error_msg = f"Fastboot preparation failed: {prep_error}"
                        results.append({
                            "device": device,
                            "success": False,
                            "error": error_msg,
                            "output": error_msg,
                        })
                        if client_id in runtime.global_state.websocket_connections:
                            with contextlib.suppress(Exception):
                                await runtime.safe_websocket_send(client_id, {"type": "log_update", "log": f"Device {device} GSI burn failed: {error_msg}", "log_type": "error"})
                        continue

                    _stdin, stdout, stderr = await asyncio.to_thread(
                        ssh.exec_command,
                        burn_cmd,
                        get_pty=True,
                        timeout=600,
                    )
                    output_buffer = []

                    while not stdout.channel.exit_status_ready():
                        if stdout.channel.recv_ready():
                            chunk = (await asyncio.to_thread(stdout.channel.recv, 1024)).decode("utf-8", errors="ignore")
                            output_buffer.append(chunk)
                            clean_chunk = strip_ansi_codes(chunk)

                            if client_id in runtime.global_state.websocket_connections:
                                try:
                                    for line in clean_chunk.split("\n"):
                                        line = line.strip()
                                        if not line:
                                            continue
                                        # 过滤 fastboot 冗余输出
                                        if (line.startswith("OKAY") or
                                            line.startswith("Writing '") or
                                            line.startswith("Finished.") or
                                            line.startswith("< waiting for")):
                                            continue
                                        # 保留操作名，去掉尾部的 OKAY [x.xxxs]
                                        cleaned = _FASTBOOT_OKAY_RE.sub("", line)
                                        await runtime.safe_websocket_send(client_id, {"type": "log_update", "log": cleaned, "log_type": "info"})
                                except Exception:
                                    pass
                        else:
                            await asyncio.sleep(0.5)

                    while stdout.channel.recv_ready():
                        chunk = await asyncio.to_thread(stdout.channel.recv, 1024)
                        output_buffer.append(chunk.decode("utf-8", errors="ignore"))
                    final_output = "".join(output_buffer)
                    exit_status = stdout.channel.recv_exit_status()
                    error_output = (await asyncio.to_thread(stderr.read)).decode("utf-8", errors="ignore")

                    if exit_status == 0:
                        reboot_output, reboot_error, reboot_code = await asyncio.to_thread(
                            runtime.ssh_manager.execute_command,
                            ssh,
                            f"fastboot -s {shlex.quote(device)} reboot",
                            timeout=30,
                        )
                        # Schedule even if fastboot reports a transport race:
                        # the device may already have accepted the reboot.
                        _schedule_usbip_mode_reconnect(device, "adb")
                        if reboot_code != 0:
                            detail = (
                                reboot_error or reboot_output or "unknown error"
                            ).strip()
                            error_msg = f"镜像已写入，但设备重启失败: {detail}"
                            results.append({
                                "device": device,
                                "success": False,
                                "error": error_msg,
                                "output": final_output,
                            })
                            if client_id in runtime.global_state.websocket_connections:
                                with contextlib.suppress(Exception):
                                    await runtime.safe_websocket_send(client_id, {"type": "log_update", "log": f"Device {device} GSI burn failed: {error_msg}", "log_type": "error"})
                            continue
                        results.append({"device": device, "success": True, "output": final_output})
                        if client_id in runtime.global_state.websocket_connections:
                            with contextlib.suppress(Exception):
                                await runtime.safe_websocket_send(client_id, {"type": "log_update", "log": f"Device {device} GSI burn complete", "log_type": "success"})
                    else:
                        combined_output = "\n".join(
                            part for part in (final_output, error_output) if part
                        )
                        error_msg = diagnose_gsi_burn_failure(combined_output)
                        results.append({"device": device, "success": False, "error": error_msg, "output": combined_output})
                        if client_id in runtime.global_state.websocket_connections:
                            try:
                                await runtime.safe_websocket_send(client_id, {"type": "log_update", "log": f"Device {device} GSI burn failed: {error_msg}", "log_type": "error"})
                            except Exception:
                                pass

                try:
                    await runtime.release_firmware_devices(client_id, locked_devices)
                except Exception as release_error:
                    logger.warning("[GSI Burn] Failed to release device locks: %s", release_error)

                all_success = all(r["success"] for r in results)
                if all_success:
                    try:
                        runtime.store_notification(client_id, "GSI burn complete", f"Devices: {', '.join(online_devices)}", "success", "firmware", {"devices": online_devices, "results": results})
                    except Exception as notify_error:
                        logger.warning("[GSI Burn] Failed to store success notification: %s", notify_error)
                    # 设备锁已释放，通知前端刷新 ADB 设备状态
                    if client_id in runtime.global_state.websocket_connections:
                        with contextlib.suppress(Exception):
                            await runtime.safe_websocket_send(client_id, {"type": "firmware_burn_complete", "devices": online_devices, "success": True})
                    return JSONResponse(content={"success": True, "message": "GSI burn completed successfully", "results": results})
                else:
                    failed_results = [r for r in results if not r.get("success")]
                    failure_summary = "; ".join(
                        f"{result.get('device')}: {result.get('error')}"
                        for result in failed_results
                    )
                    try:
                        runtime.store_notification(client_id, "GSI burn failed", failure_summary[:300], "error", "firmware", {"devices": online_devices, "results": results})
                    except Exception as notify_error:
                        logger.warning("[GSI Burn] Failed to store failure notification: %s", notify_error)
                    return error_response(f"部分设备烧写失败: {failure_summary}", results=results)

            except Exception as e:
                try:
                    runtime.store_notification(client_id, "GSI burn error", str(e)[:300], "error", "firmware", {"devices": online_devices})
                except Exception as notify_error:
                    logger.warning("[GSI Burn] Failed to store error notification: %s", notify_error)
                try:
                    await runtime.release_firmware_devices(client_id, locked_devices)
                except Exception as release_error:
                    logger.warning("[GSI Burn] Failed to release device locks after error: %s", release_error)
                return error_response(str(e))

    except Exception as e:
        logger.error(f"Error in burn_gsi: {e}")
        return error_response(str(e), 500)



@router.post("/api/burn/serial")
async def burn_sn(
    req: SNBurnRequest,
    _admin=Depends(require_elevated_admin_when_auth_required),
):
    """SN burning - burn serial number to selected devices."""
    try:
        devices = req.devices
        sn_code = req.sn_code

        if not devices:
            return error_response("No devices selected", 400)
        if not sn_code:
            return error_response("SN code is required", 400)

        config = runtime.config_manager.load_config()
        async with runtime.ssh_manager.async_optional_connection(config) as ssh:
            if not ssh:
                return error_response("SSH connection failed", 500)

            results = [
                {
                    "device": device_id,
                    "success": False,
                    "error": "SN burning requires device in loader mode. Feature needs specific tool support.",
                }
                for device_id in devices
            ]

            return JSONResponse(content={"success": True, "results": results})
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error burning SN: {e}")
        return error_response(str(e), status_code=500)
