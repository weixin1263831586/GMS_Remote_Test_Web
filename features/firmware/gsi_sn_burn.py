"""GSI and serial-number burn endpoints.

Split out of firmware_api.py so the main firmware API stays
under the reviewable-size limit. Both endpoints reuse the firmware runtime
bindings; the /api/burn/gsi script flow and the SN stub previously lived
inline at the tail of firmware_api.py.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import shlex

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from features.auth import require_elevated_admin_when_auth_required
from features.test_execution import get_default_suites_path
from foundation.responses import error_response

from . import runtime
from .api_helpers import (
    adb_proxy_devices as _adb_proxy_devices,
)
from .api_helpers import (
    resolve_gsi_remote_image as _resolve_gsi_remote_image,
)
from .gsi_diagnostics import diagnose_gsi_burn_failure
from .gsi_transport import prepare_gsi_command, upload_gsi_assets
from .models import SNBurnRequest
from .usbip_transport import (
    notify_skipped_devices as _notify_skip,
)
from .usbip_transport import (
    partition_devices_by_flash_state as _partition_devices_by_flash_state,
)
from .usbip_transport import (
    prepare_usbip_firmware_routes as _prepare_usbip_firmware_routes,
)
from .usbip_transport import (
    schedule_usbip_mode_reconnect as _schedule_usbip_mode_reconnect,
)


logger = logging.getLogger(__name__)

router = APIRouter()

# Moved with burn_gsi (was module-level in firmware_api.py).
_FASTBOOT_OKAY_RE = re.compile(r"\s+OKAY\s+\[\s*[\d.]+s\]$")
_ANSI_ESCAPE_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


def strip_ansi_codes(text: str) -> str:
    """Strip ANSI escape sequences from remote tool output."""
    return _ANSI_ESCAPE_RE.sub("", text)


async def _lock_devices(
    request: Request, client_id: str, devices: list, error_prefix="Devices occupied"
):
    """Lock devices for a burn operation (moved from firmware_api.py).

    The split-out copy passed four positional arguments while the real
    runtime binding (workflows/firmware_device.lock_firmware_devices) only
    accepts keyword parameters — any legitimate request failed with
    ``TypeError`` at the lock stage, before flashing. Mirror the original
    wrapper: resolve the display username from the request and call with
    keyword arguments.
    """
    from features.users import get_client_username_from_request

    username = get_client_username_from_request(request)
    return await runtime.lock_firmware_devices(
        client_id=client_id,
        username=username,
        devices=devices,
        error_prefix=error_prefix,
    )


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
                        reboot_result = await asyncio.to_thread(
                            runtime.ssh_manager.execute_command,
                            ssh,
                            f"fastboot -s {shlex.quote(device)} reboot",
                            timeout=30,
                        )
                        # Schedule even if fastboot reports a transport race:
                        # the device may already have accepted the reboot.
                        _schedule_usbip_mode_reconnect(device, "adb")
                        if not reboot_result.ok:
                            detail = (
                                reboot_result.stderr
                                or reboot_result.stdout
                                or "unknown error"
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
