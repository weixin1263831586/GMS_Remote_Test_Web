"""USB/IP coordination used by firmware and GSI mode transitions."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import shlex
import time

from features.devices import (
    DeviceUtils,
    ensure_usbip_auto_bind_policies,
    parse_adb_device_states,
    resolve_usbip_flash_routes,
)
from features.devices import reconnect as usbip_reconnect

from . import runtime


logger = logging.getLogger(__name__)
ROCKUSB_LOADER_COUNT_RE = re.compile(
    r"List\s+of\s+rockusb\s+connected\(\s*(\d+)\s*\)", re.IGNORECASE
)


def schedule_usbip_mode_reconnect(device: str, target_protocol: str) -> bool:
    """Rebind a USB/IP device after its USB identity changes."""
    try:
        device_host = usbip_reconnect.usbip_source_host_for_device(device)
        if not device_host:
            return False
        return usbip_reconnect.schedule_usbip_reconnect(
            device_host,
            reason=f"USB/IP {device} switching to {target_protocol}",
            expected_devices=[device],
            accept_transport_only=True,
        )
    except Exception as exc:
        logger.warning("USB/IP mode reconnect schedule failed for %s: %s", device, exc)
        return False


async def wait_for_rockusb_loaders(
    ssh, check_cmd: str, expected_count: int, *,
    timeout: float = 120, interval: float = 2,
) -> tuple[bool, str]:
    deadline, last_detail = time.monotonic() + max(1, timeout), ""
    while True:
        output, error, _code = await asyncio.to_thread(
            runtime.ssh_manager.execute_command, ssh, check_cmd, timeout=5
        )
        last_detail = (output or error or "").strip()
        match = ROCKUSB_LOADER_COUNT_RE.search(last_detail)
        if match and int(match.group(1)) >= max(1, expected_count):
            return True, last_detail
        if time.monotonic() >= deadline:
            return False, last_detail
        await asyncio.sleep(max(0.1, interval))


async def wait_for_adb_devices(
    ssh, expected_devices: list[str], *,
    timeout: float = 120, interval: float = 2,
) -> tuple[bool, list[str]]:
    expected = set(expected_devices)
    deadline = time.monotonic() + max(1, timeout)
    observed: list[str] = []
    while True:
        output, _error, _code = await asyncio.to_thread(
            runtime.ssh_manager.execute_command, ssh, "adb devices", timeout=8
        )
        states = parse_adb_device_states(output)
        observed = sorted(
            serial for serial, state in states.items() if state == "device"
        )
        if expected.issubset(observed):
            return True, observed
        if time.monotonic() >= deadline:
            return False, observed
        await asyncio.sleep(max(0.1, interval))


async def prepare_usbip_firmware_routes(
    devices: list[str],
) -> tuple[list[dict], str]:
    usbip_devices = [
        device for device in devices
        if usbip_reconnect.usbip_source_host_for_device(device)
    ]
    if not usbip_devices:
        return [], ""
    routes = resolve_usbip_flash_routes(usbip_devices)
    routed = {
        str(device or "") for route in routes
        for device in route.get("device_ids") or []
    }
    unresolved = [device for device in usbip_devices if device not in routed]
    if unresolved:
        return [], (
            "USB/IP固件烧写缺少设备到物理BUSID的持久分配记录: "
            + ", ".join(unresolved)
            + "。请断开后从设备管理页重新选择该USB设备并连接。"
        )
    for route in routes:
        result = await asyncio.to_thread(
            ensure_usbip_auto_bind_policies,
            route["device_host"], route["busids"],
        )
        if not result.get("success"):
            return [], str(result.get("error") or "USB/IP AutoBind策略配置失败")
    return routes, ""


async def reattach_usbip_after_rockusb_reset(
    ssh, routes: list[dict], *, timeout: float = 15, interval: float = 0.15,
    attach_command_timeout: float = 4,
) -> dict:
    """Reattach Loader ports while upgrade_tool waits for MaskROM.

    ``upgrade_tool`` 自身只等待数秒；窗口需覆盖 Download Boot 下载耗时、
    Windows 端重新枚举并按 AutoBind 策略共享新实例、以及 Linux 端 attach
    握手。单次 attach 给足数秒（常规连接流程为 15s；被拒绝的请求会立即
    返回，超时上限只保护慢速成功路径，避免 1s 截断杀死即将成功的挂载）。
    即使 attach 晚于工具退出才完成，设备也会留在 MaskROM 传输上，直接
    重试烧写即可通过 Loader 预检。
    """
    pending = {
        (str(route.get("source_host") or "").strip(), str(busid).strip())
        for route in routes for busid in route.get("busids") or []
        if str(route.get("source_host") or "").strip() and str(busid).strip()
    }
    if not pending:
        return {"success": True, "attached": []}
    attached, errors = [], {}
    attempts = 0
    started = time.monotonic()
    deadline = started + max(0.5, timeout)
    while pending and time.monotonic() < deadline:
        for source_host, busid in list(pending):
            command = (
                "sudo usbip attach -r "
                f"{shlex.quote(source_host)} -b {shlex.quote(busid)}"
            )
            attempts += 1
            try:
                output, error, code = await asyncio.to_thread(
                    runtime.ssh_manager.execute_command,
                    ssh, command, timeout=attach_command_timeout,
                )
            except Exception as exc:
                errors[f"{source_host}/{busid}"] = str(exc)
                continue
            if code == 0:
                pending.remove((source_host, busid))
                attached.append({"source_host": source_host, "busid": busid})
                logger.info(
                    "RockUSB USB/IP reattach succeeded for %s/%s "
                    "after %s attempt(s)",
                    source_host, busid, attempts,
                )
            else:
                errors[f"{source_host}/{busid}"] = (
                    error or output or f"usbip attach exited with code {code}"
                ).strip()
        if pending:
            await asyncio.sleep(max(0.05, interval))
    result = {
        "success": not pending,
        "attached": attached,
        "pending": [
            {"source_host": host, "busid": busid}
            for host, busid in sorted(pending)
        ],
        "errors": errors,
        "attempts": attempts,
        "elapsed_seconds": round(time.monotonic() - started, 2),
    }
    if pending:
        logger.warning(
            "RockUSB USB/IP reattach unfinished after %ss (%s attempt(s)): "
            "pending=%s errors=%s",
            result["elapsed_seconds"], attempts, result["pending"], errors,
        )
    return result


def device_flash_protocols(ssh, devices: list[str]) -> dict[str, str]:
    adb_output, _error, _code = runtime.ssh_manager.execute_command(
        ssh, "adb devices", timeout=8
    )
    adb_states = parse_adb_device_states(adb_output)
    fastboot_output, fastboot_error, _code = runtime.ssh_manager.execute_command(
        ssh, "fastboot devices", timeout=8
    )
    fastboot_devices = set(
        DeviceUtils.parse_fastboot_devices(fastboot_output or fastboot_error)
    )
    return {
        serial: (
            "adb" if adb_states.get(serial) == "device"
            else "fastboot" if serial in fastboot_devices else ""
        )
        for serial in devices
    }


def accept_direct_rockusb_loaders(
    protocols: dict[str, str], usbip_routes: list[dict], loader_output: str,
) -> dict[str, str]:
    updated = dict(protocols)
    unavailable = [device for device, protocol in updated.items() if not protocol]
    routed = {
        str(device or "") for route in usbip_routes
        for device in route.get("device_ids") or []
    }
    match = ROCKUSB_LOADER_COUNT_RE.search(loader_output or "")
    if (unavailable and set(unavailable).issubset(routed)
            and match and int(match.group(1)) >= len(unavailable)):
        updated.update({device: "rockusb-loader" for device in unavailable})
    return updated


def partition_devices_by_flash_state(
    ssh, devices: list[str],
) -> tuple[list[str], list[str]]:
    protocols = device_flash_protocols(ssh, devices)
    ready = [serial for serial in devices if protocols.get(serial)]
    return ready, [serial for serial in devices if not protocols.get(serial)]


async def notify_skipped_devices(client_id: str, offline: list[str]) -> None:
    if not offline or client_id not in runtime.global_state.websocket_connections:
        return
    with contextlib.suppress(Exception):
        await runtime.safe_websocket_send(client_id, {
            "type": "log_update",
            "log": (
                "跳过不可烧写设备（未在 ADB/Fastboot 中或状态异常）: "
                + ", ".join(offline)
            ),
            "log_type": "warning",
        })
