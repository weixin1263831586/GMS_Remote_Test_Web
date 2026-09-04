"""USB/IP coordination used by firmware and GSI mode transitions."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
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
        probe = await asyncio.to_thread(
            runtime.ssh_manager.execute_command, ssh, check_cmd, timeout=5
        )
        last_detail = (probe.stdout or probe.stderr or "").strip()
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
        adb_result = await asyncio.to_thread(
            runtime.ssh_manager.execute_command, ssh, "adb devices", timeout=8
        )
        states = parse_adb_device_states(adb_result.stdout)
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


def device_flash_protocols(ssh, devices: list[str]) -> dict[str, str]:
    adb_result = runtime.ssh_manager.execute_command(ssh, "adb devices", timeout=8)
    adb_states = parse_adb_device_states(adb_result.stdout)
    fastboot_result = runtime.ssh_manager.execute_command(
        ssh, "fastboot devices", timeout=8
    )
    fastboot_devices = set(
        DeviceUtils.parse_fastboot_devices(
            fastboot_result.stdout or fastboot_result.stderr
        )
    )
    return {
        serial: (
            "adb" if adb_states.get(serial) == "device"
            else "fastboot" if serial in fastboot_devices else ""
        )
        for serial in devices
    }


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
