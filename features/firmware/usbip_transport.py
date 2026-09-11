"""USB/IP coordination used by firmware and GSI mode transitions."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time

from features.devices import (
    ROCKUSB_SYSFS_PROBE_COMMAND,
    DeviceUtils,
    ensure_usbip_auto_bind_policies,
    parse_adb_device_states,
    resolve_usbip_flash_routes,
    rockusb_loader_serials,
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
    """Resolve immutable physical routes for a complete firmware burn.

    每条 route 显式携带 ``source_os``（windows|linux|""，探测失败时为
    空字符串）。烧写 backend 必须按 source_os 分流，禁止默默把 Linux
    source 送进 Windows-only backend。
    """
    from features.devices import (
        lookup_usbip_source_os,
        record_usbip_source_os,
    )

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

    def _route_source_os(device_host: str) -> str:
        # 缓存优先（TTL 内可信）；未命中时以 AutoBind 流程的探测结果
        # 为准回填，避免每次烧写都加一次 SSH 探测往返。
        cached = lookup_usbip_source_os(device_host)
        return {"linux": "linux", "windows": "windows"}.get(cached, "")

    for route in routes:
        device_host = str(route.get("device_host") or "").strip()
        result = await asyncio.to_thread(
            ensure_usbip_auto_bind_policies,
            device_host, route["busids"],
        )
        if not result.get("success"):
            return [], str(result.get("error") or "USB/IP AutoBind策略配置失败")
        probed = str(result.get("source_os") or "").strip()
        if not probed:
            probed = _route_source_os(device_host)
        if probed == "ubuntu":
            probed = "linux"
        if probed:
            route["source_os"] = probed
            with contextlib.suppress(Exception):
                record_usbip_source_os(device_host, probed)
        else:
            route["source_os"] = ""
    return routes, ""


async def release_usbip_devices_to_source(
    ssh, routes: list[dict],
) -> tuple[bool, str]:
    """Ownership handoff: target worker releases the USB/IP device so the
    physical source host owns it again before a source-side flash.

    状态机 PREPARE 段（见 ADR-0005）：
    1. suspend reconnect watchdog（固件 claim 期间禁止通用重连）；
    2. target 侧 vhci detach（worker 不再持有设备）；
    3. 确认 target 侧端口已消失（fail closed：查询失败视为未释放）。
    返回 (released, error)。调用方在 SOURCE_OWNED 状态后才允许下发烧写。
    """
    from features.devices import parse_usbip_port_entries, USBIP_PORT_COMMAND

    if not routes:
        return True, ""
    all_busids: list[str] = []
    for route in routes:
        all_busids.extend(str(b) for b in route.get("busids") or [])
        device_host = str(route.get("device_host") or "").strip()
        if device_host:
            usbip_reconnect.pause_usbip_reconnect(
                device_host=device_host,
                device_ids=[str(d) for d in route.get("device_ids") or []],
            )
    if not all_busids:
        return True, ""

    # 1) target 侧 detach：按 host/busid 结构化匹配，只拆本次路由的端口。
    port_result = await asyncio.to_thread(
        runtime.ssh_manager.execute_command, ssh, USBIP_PORT_COMMAND, timeout=10,
    )
    if not port_result.ok:
        return False, (
            "无法确认目标主机 USB/IP 端口状态，拒绝进入源端烧写: "
            + (port_result.stderr or port_result.stdout or "").strip()
        )
    target_busids = set(all_busids)
    ports_to_detach = [
        entry for entry in parse_usbip_port_entries(port_result.stdout or "")
        if entry["busid"] in target_busids
    ]
    for entry in ports_to_detach:
        await asyncio.to_thread(
            runtime.ssh_manager.execute_command,
            ssh, f"sudo -n usbip detach -p {entry['port']}", timeout=15,
        )

    # 2) fail-closed 复核：目标端口必须已消失；列表不可解析时按未释放
    # 处理（R07 假 detach 教训），由调用方中止烧写。
    verify = await asyncio.to_thread(
        runtime.ssh_manager.execute_command, ssh, USBIP_PORT_COMMAND, timeout=10,
    )
    if not verify.ok:
        return False, "USB/IP 释放后无法复核目标主机端口状态，拒绝继续烧写"
    remaining = [
        entry["port"] for entry in parse_usbip_port_entries(verify.stdout or "")
        if entry["busid"] in target_busids
    ]
    if remaining:
        return False, (
            "目标主机仍持有 USB/IP 端口: " + ", ".join(remaining)
            + "；设备所有权未交还源主机，已中止烧写"
        )
    if ports_to_detach:
        # 等待源主机侧 PnP 重新认领设备（Windows 重新枚举约 1-2s）。
        await asyncio.sleep(2)
    return True, ""


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
    try:
        loader_probe = runtime.ssh_manager.execute_command(
            ssh, ROCKUSB_SYSFS_PROBE_COMMAND, timeout=15
        )
        loader_devices = set(
            rockusb_loader_serials(
                loader_probe.stdout or "",
                exclude_serials=set(adb_states) | fastboot_devices,
            )
        )
    except Exception:
        logger.warning("rockusb loader probe failed", exc_info=True)
        loader_devices = set()
    return {
        serial: (
            "adb" if adb_states.get(serial) == "device"
            else "fastboot" if serial in fastboot_devices
            else "rockusb-loader" if serial in loader_devices
            else ""
        )
        for serial in devices
    }


def partition_devices_by_flash_state(
    ssh, devices: list[str],
) -> tuple[list[str], list[str]]:
    protocols = device_flash_protocols(ssh, devices)
    # GSI 直刷只支持从 ADB/Fastboot 起步；rockusb-loader 仅属于
    # update.img 固件烧写链路（firmware_api 直接探测 protocols）。
    flash_ready = {"adb", "fastboot"}
    ready = [serial for serial in devices if protocols.get(serial) in flash_ready]
    return ready, [serial for serial in devices if protocols.get(serial) not in flash_ready]


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
