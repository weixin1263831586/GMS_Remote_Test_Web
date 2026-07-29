from __future__ import annotations

import asyncio
import logging
import os
import re
import shlex
import time

from fastapi import APIRouter, Body, HTTPException, Query, Request

from features.test_execution import get_default_suites_path
from foundation.responses import error_response, success_response
from foundation.security import sanitize_device_ids
from worker_agent.fastboot_workflow import (
    CommandResult,
    FastbootPreparer,
)

from . import runtime
from .manager import device_manager
from .models import DeviceActionRequest, DeviceLockRequest, VerifiedBootState
from .support import (
    SSHConnection,
    device_claim_conflict_response,
    device_mutation_guard,
    get_device_properties_optimized,
)


logger = logging.getLogger(__name__)
router = APIRouter()


def _api_success(data=None, message="操作成功"):
    return success_response(data=data, message=message)


def _api_error(message, status_code=500):
    return error_response(message, status_code=status_code)


def _help_or_continue(help_requested: bool, method: str, path: str):
    if runtime.generate_help_or_continue is None:
        return None
    return runtime.generate_help_or_continue(help_requested, method, path)


def _adb_state_is_ready(output: str, code: int) -> bool:
    return code == 0 and output.strip() == "device"


def _adb_proxy_devices(devices: list[str]) -> list[str]:
    try:
        from worker_agent.adb_proxy import imported_device_for_serial

        return [
            device_id
            for device_id in devices
            if imported_device_for_serial(device_id) is not None
        ]
    except (ImportError, OSError, RuntimeError, ValueError):
        return []


def _bootloader_operation_response(results: list[dict], action_text: str):
    success_count = sum(item.get("success", False) for item in results)
    payload = {
        "results": results,
        "summary": {
            "total": len(results),
            "success": success_count,
            "failed": len(results) - success_count,
        },
    }
    if success_count != len(results):
        details = "; ".join(
            f"{item.get('device')}: {item.get('error') or item.get('output') or 'unknown error'}"
            for item in results
            if not item.get("success")
        )
        return error_response(
            f"Device {action_text} failed: {details}",
            status_code=200,
            data=payload,
        )
    return _api_success(payload, f"Device {action_text} operation completed")


async def _manage_bootloader_lock(
    devices: list[str],
    action: str,
    client_id: str = "",
):
    try:
        if not devices:
            return _api_error("No devices selected", status_code=400)
        proxy_devices = _adb_proxy_devices(devices)
        if proxy_devices:
            return _api_error(
                "ADB Proxy远程设备没有本地USB/Fastboot通道，不能锁定或解锁: "
                + ", ".join(proxy_devices),
                status_code=409,
            )
        conflict = device_claim_conflict_response(
            devices, client_id, allow_owner=True
        )
        if conflict:
            return conflict

        valid_device_pattern = re.compile(r"^[a-zA-Z0-9.:-]+$")
        for device_id in devices:
            if not valid_device_pattern.match(device_id):
                return _api_error(
                    f"Invalid device ID format: {device_id}", status_code=400
                )

        config = runtime.config_manager.load_config()
        with runtime.ssh_manager.connection(config) as ssh:
            local_script = os.path.join(
                runtime.project_root, "scripts", "run_Device_Lock.sh"
            )
            remote_script = os.path.join(
                get_default_suites_path(config), "run_Device_Lock.sh"
            )
            if not os.path.exists(local_script):
                return _api_error(
                    f"Script file not found: {local_script}", status_code=404
                )
            try:
                with ssh.open_sftp() as sftp:
                    sftp.put(local_script, remote_script)
                runtime.ssh_manager.execute_command(
                    ssh, f"chmod +x {shlex.quote(remote_script)}"
                )
            except Exception as exc:
                return _api_error(f"Script upload failed: {exc!s}", status_code=500)

            results = []
            for device_id in devices:
                try:
                    def remote_runner(argv: list[str], timeout: int) -> CommandResult:
                        output, error, code = runtime.ssh_manager.execute_command(
                            ssh,
                            shlex.join(argv),
                            timeout=timeout,
                        )
                        return CommandResult(output, error, code)

                    prepared = await asyncio.to_thread(
                        FastbootPreparer(remote_runner).prepare_bootloader,
                        device_id,
                    )
                    cmd = shlex.join([
                        "bash",
                        remote_script,
                        device_id,
                        prepared.oem_argument(action),
                    ])
                    output, error, code = runtime.ssh_manager.execute_command(ssh, cmd)
                    if code == 0:
                        start_time = time.time()
                        adb_ready = False
                        while time.time() - start_time < 60:
                            check_output, _, check_code = runtime.ssh_manager.execute_command(
                                ssh, f"adb -s {device_id} get-state"
                            )
                            if _adb_state_is_ready(check_output, check_code):
                                adb_ready = True
                                break
                            await asyncio.sleep(1)
                        if not adb_ready:
                            code = 1
                            error = (
                                f"{error}\n" if error else ""
                            ) + "设备操作后未在 60 秒内返回 ADB device 状态"
                    await asyncio.sleep(2)
                    result_output = "\n".join(
                        part.strip() for part in (output, error) if part and part.strip()
                    )
                    results.append({
                        "device": device_id,
                        "success": code == 0,
                        "output": result_output[-500:],
                        **({"error": result_output[-500:]} if code != 0 else {}),
                    })
                except Exception as exc:
                    results.append({
                        "device": device_id, "success": False, "error": str(exc),
                    })

            action_text = "unlock" if action == "unlock" else "lock"
            return _bootloader_operation_response(results, action_text)
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Error managing device lock: %s", exc)
        return _api_error(str(exc), status_code=500)


def _resolve_device_lock_devices(req: DeviceLockRequest | None) -> list[str]:
    if req is None:
        return []
    if req.device_id:
        return [req.device_id]
    return req.devices or []


@router.post("/api/devices/bootloader-lock")
@device_mutation_guard("bootloader-lock")
async def lock_bootloader(
    request: Request,
    help: bool = Query(False),
    req: DeviceLockRequest = Body(None),
):
    response = _help_or_continue(help, "POST", "/api/devices/bootloader-lock")
    if response:
        return response
    return await _manage_bootloader_lock(
        _resolve_device_lock_devices(req),
        "lock",
        runtime.get_client_id_from_request(request),
    )


@router.post("/api/devices/bootloader-unlock")
@device_mutation_guard("bootloader-unlock")
async def unlock_bootloader(
    request: Request,
    help: bool = Query(False),
    req: DeviceLockRequest = Body(None),
):
    response = _help_or_continue(help, "POST", "/api/devices/bootloader-unlock")
    if response:
        return response
    return await _manage_bootloader_lock(
        _resolve_device_lock_devices(req),
        "unlock",
        runtime.get_client_id_from_request(request),
    )


@router.post("/api/devices/bootloader-status")
async def check_bootloader_status(
    req: DeviceActionRequest,
    request: Request,
):
    try:
        devices = sanitize_device_ids(req.devices)
        if not devices:
            return _api_error("No valid device serials", status_code=400)
        conflict = device_claim_conflict_response(
            devices,
            runtime.get_client_id_from_request(request),
            allow_owner=True,
        )
        if conflict:
            return conflict

        with SSHConnection() as ssh:
            def check_single_device(device_id: str) -> dict:
                output, _error, _code = runtime.ssh_manager.execute_command(
                    ssh,
                    f"adb -s {device_id} shell getprop ro.boot.verifiedbootstate",
                )
                state = output.strip()
                try:
                    boot_state = VerifiedBootState(state)
                    is_locked = boot_state.is_locked
                    status_text = boot_state.display_text
                except ValueError:
                    is_locked = False
                    status_text = f"Unknown state ({state})"
                return {
                    "device": device_id,
                    "locked": is_locked,
                    "state": state,
                    "status": status_text,
                }

            results = []
            for device_id in devices:
                results.append(await asyncio.to_thread(check_single_device, device_id))
            return _api_success({"results": results}, "Lock status check completed")
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Error checking lock status: %s", exc)
        return _api_error(str(exc), status_code=500)


@router.post("/api/devices/info")
async def get_device_info(req: DeviceActionRequest, request: Request):
    try:
        devices = sanitize_device_ids(req.devices)
        if not devices:
            return _api_error("No valid device serials", status_code=400)
        conflict = device_claim_conflict_response(
            devices,
            runtime.get_client_id_from_request(request),
            allow_owner=True,
        )
        if conflict:
            return conflict

        with SSHConnection() as ssh:
            def get_single_device_info(device_id: str) -> dict:
                device_info = {"device": device_id, "properties": {}}
                base_info = device_manager.get_device_info(device_id, ssh)
                field_mapping = {
                    "serial_no": "Serial Number",
                    "model": "Model",
                    "android_version": "Android Version",
                    "fingerprint": "Fingerprint",
                    "build_type": "Build Type",
                    "build_tags": "Build Tags",
                    "build_date": "Build Date",
                    "sdk_version": "SDK Version",
                    "security_patch": "Security Patch",
                }
                for key, label in field_mapping.items():
                    if key in base_info:
                        device_info["properties"][label] = base_info[key]

                extra_props = get_device_properties_optimized(device_id, ssh)
                prop_mapping = {
                    "boot_state": ("Boot State", lambda x: x if x else "Unknown"),
                    "api_level": (
                        "API Level",
                        lambda x: x.split("[")[-1].replace("]", "")
                        if "[" in x else (x or "Unknown"),
                    ),
                    "mali_version": ("Mali Version", lambda x: x or "Unknown"),
                    "mem_total": ("Total Memory", lambda x: f"{x} KB" if x else "Unknown"),
                    "mem_free": ("Free Memory", lambda x: f"{x} KB" if x else "Unknown"),
                    "timezone": ("Timezone", lambda x: x or "Unknown"),
                    "locale": ("Language", lambda x: x or "Unknown"),
                    "data_partition": (
                        "DATA Partition",
                        lambda x: x.split()[-1] if x and "userdata" in x else "Unknown",
                    ),
                }
                for key, (label, formatter) in prop_mapping.items():
                    if key in extra_props:
                        device_info["properties"][label] = formatter(extra_props[key])
                return device_info

            results = []
            for device_id in devices:
                results.append(await asyncio.to_thread(get_single_device_info, device_id))
            return _api_success({"results": results}, "Device info retrieved")
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Error getting device info: %s", exc)
        return _api_error(str(exc), status_code=500)
