"""USB/IP 协议态探测边界（transport 之上的 Android 协议层）。

从 ``usbip.py`` 抽出的协议状态模块：transport(vhci 端口)attach 完成
之后，设备的可用性由 ADB/Fastboot 协议态决定。本模块聚合：

- ``parse_adb_device_states`` / ``parse_fastboot_devices``：远端命令
  输出的纯解析；
- ``probe_protocol_status``：在目标 Ubuntu 上执行一次协议探测并归类
  (adb/recovery/sideload/unauthorized/offline/fastboot)；
- ``scope_protocol_status``：把全局探测结果归因到本次 attach 的设备，
  无法归因时降级为 mode=unknown(原始结果保留在 ``unscoped``)；
- ``build_attach_message``：面向用户的 attach 结果文案。

全部为无状态函数；SSH 执行经 ``ssh_manager`` 注入，不持有连接。
"""

from __future__ import annotations

import logging
import shlex
from typing import Any

from .utils import DeviceUtils


logger = logging.getLogger(__name__)


def parse_adb_device_states(output: str) -> dict[str, str]:
    """Parse all adb-visible serials, including recovery/offline/unauthorized."""
    states: dict[str, str] = {}
    for raw_line in (output or "").splitlines():
        line = raw_line.strip()
        if not line or line.lower().startswith("list of devices"):
            continue
        parts = line.split()
        if len(parts) >= 2:
            states[parts[0]] = parts[1]
    return states


def parse_fastboot_devices(output: str) -> list[str]:
    """Parse fastboot device serials."""
    return DeviceUtils.parse_fastboot_devices(output)


def build_adb_devices_command(adb_server_socket: str | None = None) -> str:
    """Build the adb devices command, quoting any custom server socket."""
    if not adb_server_socket:
        return "adb devices"
    return (
        "ADB_SERVER_SOCKET="
        + shlex.quote(adb_server_socket)
        + " adb devices"
    )


def recompute_protocol_mode(status: dict[str, Any]) -> str:
    """Derive ``mode`` from the attribution lists — the single source of truth.

    Any code that filters/mutates the attribution lists (scope, reconnect
    keep-alive probes) must re-derive ``mode`` through this function. A stale
    global ``mode`` must never survive filtering: on a multi-device Ubuntu
    host the global mode can be contributed by a device outside the current
    USB/IP assignment (e.g. a locally attached fastboot device), and keeping
    it would yield ``mode="fastboot"`` with ``fastboot==[]`` — a
    self-contradictory status that downstream burn/reconnect logic trusts.
    """
    if status.get("fastboot"):
        return "fastboot"
    if status.get("recovery") or status.get("sideload"):
        return "recovery"
    if status.get("adb_ready"):
        return "adb"
    if status.get("unauthorized"):
        return "unauthorized"
    if status.get("offline"):
        return "offline"
    if status.get("adb"):
        return "adb_non_device"
    return "unknown"


def probe_protocol_status(
    ssh_manager,
    ssh,
    adb_server_socket: str | None = None,
) -> dict[str, Any]:
    """Probe Android protocol states after USB/IP transport is attached."""
    status: dict[str, Any] = {
        "adb": {},
        "adb_ready": [],
        "recovery": [],
        "sideload": [],
        "unauthorized": [],
        "offline": [],
        "fastboot": [],
        "mode": "unknown",
    }
    try:
        adb_probe = ssh_manager.execute_command(
            ssh,
            build_adb_devices_command(adb_server_socket),
            timeout=8,
        )
        adb_states = parse_adb_device_states(
            adb_probe.stdout or adb_probe.stderr or ""
        )
        status["adb"] = adb_states
        status["adb_ready"] = [serial for serial, state in adb_states.items() if state == "device"]
        status["recovery"] = [serial for serial, state in adb_states.items() if state == "recovery"]
        status["sideload"] = [serial for serial, state in adb_states.items() if state == "sideload"]
        status["unauthorized"] = [serial for serial, state in adb_states.items() if state == "unauthorized"]
        status["offline"] = [serial for serial, state in adb_states.items() if state == "offline"]
    except Exception as exc:
        logger.debug("[USB/IP] adb protocol probe failed: %s", exc)

    try:
        fastboot_probe = ssh_manager.execute_command(ssh, "fastboot devices", timeout=8)
        status["fastboot"] = parse_fastboot_devices(
            fastboot_probe.stdout or fastboot_probe.stderr or ""
        )
    except Exception as exc:
        logger.debug("[USB/IP] fastboot protocol probe failed: %s", exc)

    status["mode"] = recompute_protocol_mode(status)
    return status

def scope_protocol_status(
    protocol_status: dict[str, Any],
    device_list: list[str],
) -> dict[str, Any]:
    """Keep protocol status focused on the USB/IP devices from this attach.

    device_list 为空（transport-only/Loader/枚举失败）时，全局探测结果
    无法归因到本次 attach：Ubuntu 上其他来源设备（如直连的
    RK3562GMS7）的 ADB/Fastboot 状态不能算作 USB/IP 设备状态，否则
    重连 worker 会把"adb"误判为传输已恢复。此时清空归因列表并标记
    mode=unknown，原始探测保留在 ``unscoped`` 字段供诊断。
    """
    scoped = dict(protocol_status or {})
    if not device_list:
        scoped["adb"] = {}
        for key in ("adb_ready", "recovery", "sideload", "unauthorized", "offline", "fastboot"):
            scoped[key] = []
        scoped["unscoped"] = dict(protocol_status or {})
        scoped["mode"] = "unknown"
        return scoped
    allowed = set(device_list)
    adb_states = scoped.get("adb") or {}
    if isinstance(adb_states, dict):
        scoped["adb"] = {
            serial: state
            for serial, state in adb_states.items()
            if serial in allowed
        }
    for key in ("adb_ready", "recovery", "sideload", "unauthorized", "offline", "fastboot"):
        values = scoped.get(key) or []
        if isinstance(values, list):
            scoped[key] = [serial for serial in values if serial in allowed]
    # 过滤后必须重算 mode:全局 mode 可能由 scope 外设备(如直连
    # fastboot 设备)贡献,保留它会产生 mode="fastboot" 而 fastboot=[]
    # 的自相矛盾状态,烧录/重连逻辑会把残留 mode 当真。
    scoped["mode"] = recompute_protocol_mode(scoped)
    return scoped


def build_attach_message(
    attached: list[str],
    device_list: list[str],
    protocol_status: dict[str, Any],
) -> str:
    """Human-facing attach result message."""
    if device_list:
        return f'✅ 成功连接{len(attached)}个USB/IP设备，ADB在线: {", ".join(device_list)}'
    mode = (protocol_status or {}).get("mode") or "unknown"
    if mode in {"fastboot", "recovery", "unauthorized", "offline", "adb_non_device"}:
        return f'✅ USB/IP传输已连接，当前协议状态: {mode}'
    return '✅ USB/IP传输已连接，等待设备枚举完成'
