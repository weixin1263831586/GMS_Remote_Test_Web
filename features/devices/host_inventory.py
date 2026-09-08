"""用户主机本地直连 Android 设备清单。

用户管理页"直连设备"列展示每台用户主机物理直连的全部 Android
设备（含当前已通过 USB/IP / ADB Proxy 共享出去的设备——设备物理
上仍接在该主机上，直观计数以此为准；是否被测试操作占用由
"占用设备"列表达）。SSH 枚举单次要数秒，因此采用 TTL 缓存 +
后台线程刷新：读取立即返回缓存值，过期时只触发一次后台刷新，
绝不阻塞调用方（用户列表 10 秒轮询一次）。

能力通过 :mod:`foundation.devices_port` 暴露给 users 等特性，
由组合根 ``bootstrap.dependencies`` 调用 ``register_devices_port`` 接线。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from foundation import devices_port
from foundation.networking import split_host_port

from .usbip import usbip_manager


logger = logging.getLogger(__name__)

_CACHE_TTL_SECONDS = 60.0
_FAILURE_TTL_SECONDS = 300.0

_cache: dict[str, dict[str, Any]] = {}
_inflight: set[str] = set()
_lock = threading.Lock()

# 测试主机侧 vhci 已导入设备的序列号扫描（local busid -> serial）。
# 排除根集线器（usb*，serial 为 vhci_hcd.N）与接口实例（*:1.0）。
_VHCI_SERIAL_COMMAND = (
    'for f in $(find /sys/devices/platform/vhci_hcd* -maxdepth 4 '
    '-name serial -type f 2>/dev/null); do '
    'd=$(basename "$(dirname "$f")"); '
    'case "$d" in *:*) continue;; usb*) continue;; esac; '
    's=$(cat "$f" 2>/dev/null); [ -n "$s" ] && echo "$d $s"; '
    'done'
)
# vhci 端口状态：hub port sta spd dev sockfd local_busid。
_VHCI_STATUS_COMMAND = "cat /sys/devices/platform/vhci_hcd*/status 2>/dev/null"


def _host_token(device_host: str) -> str:
    """``user@host[:port]`` -> 纯主机地址（与 usbip port 报告的 host 可比）。"""
    hostname = str(device_host or "").rsplit("@", 1)[-1].strip()
    host, _port = split_host_port(hostname)
    return (host or hostname).strip()


def _run_on_test_host(command: str, timeout: int = 10):
    """在测试主机（本机或配置的 Ubuntu 主机）上执行只读命令。"""
    from .management_api import _run_on_test_host as runner

    config = usbip_manager.config_manager.load_config()
    return runner(config, command, timeout)


def _attached_usbip_serial_map() -> dict[tuple[str, str], str]:
    """Map (来源主机, 来源BUSID) -> 序列号，覆盖当前活跃的 USB/IP 导入。

    设备被 USB/IP attach 到测试主机后，来源侧（Windows PnP / 来源 adb）
    解析不到它的序列号，"直连设备"列只能回退显示 BUSID。测试主机侧
    ``usbip port`` 保留 (remote host, remote busid)（URL 格式行还带本地
    busid），vhci sysfs 保留 (local busid, serial)，二者可拼出完整映射：

    - URL 格式（``3-1 -> usbip://host:3240/1-1``）：local_busid 直接可析；
    - 管道格式（``1-1 | 2207:0006 | ... | Remote USB/IP host host``）：
      经 vhci status 的 port -> local_busid 关联。

    任何失败都按"无映射"降级返回空 dict，绝不影响枚举主流程。
    """
    try:
        from .usbip_transaction import USBIP_PORT_COMMAND, parse_usbip_port_entries

        port_result = _run_on_test_host(USBIP_PORT_COMMAND, timeout=10)
        if not port_result.ok:
            return {}
        entries = [
            entry for entry in parse_usbip_port_entries(port_result.stdout or '')
            if entry.get('busid') and entry.get('host')
        ]
        if not entries:
            return {}

        serial_result = _run_on_test_host(_VHCI_SERIAL_COMMAND, timeout=10)
        if not serial_result.ok:
            return {}
        serial_by_local: dict[str, str] = {}
        for line in (serial_result.stdout or '').splitlines():
            parts = line.split(None, 1)
            if len(parts) == 2 and parts[1].strip():
                serial_by_local[parts[0]] = parts[1].strip()
        if not serial_by_local:
            return {}

        mapping: dict[tuple[str, str], str] = {}
        unresolved: list[dict[str, str]] = []
        for entry in entries:
            local = str(entry.get('local_busid') or '')
            serial = serial_by_local.get(local or '')
            if serial:
                mapping[(entry['host'], entry['busid'])] = serial
            else:
                unresolved.append(entry)
        if unresolved:
            # 管道格式没有 local_busid：用 vhci status 的端口号关联。
            local_by_port: dict[str, str] = {}
            status_result = _run_on_test_host(_VHCI_STATUS_COMMAND, timeout=5)
            for line in ((status_result.stdout or '') if status_result.ok else '').splitlines():
                parts = line.split()
                if len(parts) >= 7 and parts[0] in {'hs', 'ss'} and parts[6] != '0-0':
                    local_by_port.setdefault(parts[1], parts[6])
            for entry in unresolved:
                port = str(entry.get('port') or '')
                local = (
                    local_by_port.get(f"{int(port):04d}")
                    if port.isdigit() else ''
                )
                serial = serial_by_local.get(local or '')
                if serial:
                    mapping[(entry['host'], entry['busid'])] = serial
        # 不做“唯一导入+唯一序列号”的跨主机兜底：BUSID 仅在来源主机
        # 内唯一，串主机回填会把 B 主机的序列号显示到 A 主机清单。
        return mapping
    except Exception as exc:
        logger.debug("[HostInventory] usbip serial map unavailable: %s", exc)
        return {}


def _enumerate(device_host: str) -> dict[str, Any]:
    result = usbip_manager.list_source_devices(device_host)
    if not result.get("success"):
        return {
            "devices": [],
            "source_os": "",
            "available": False,
            "error": str(result.get("error") or "USB设备枚举失败"),
        }
    # 物理直连的设备全量展示，不排除已通过 USB/IP / ADB Proxy
    # 共享出去的设备；无序列号设备回退显示 BUSID。
    raw_items = list(result.get("devices") or [])
    serial_map: dict[tuple[str, str], str] = {}
    if any(not str(item.get("serial") or "").strip() for item in raw_items):
        # 来源侧解析不到序列号（典型：设备正被 USB/IP attach 到测试
        # 主机，Windows PnP 与来源 adb 都看不到它）→ 用测试主机侧的
        # (来源主机, BUSID) -> 序列号 映射补齐。
        serial_map = _attached_usbip_serial_map()
    host_token = _host_token(device_host)
    try:
        attach_host = str(
            (usbip_manager.config_manager.load_config() or {}).get(
                "usbip_attach_host"
            ) or ""
        ).strip()
    except Exception:
        attach_host = ""
    devices: list[str] = []
    for item in raw_items:
        serial = str(item.get("serial") or "").strip()
        busid = str(item.get("busid") or "").strip()
        if not serial and busid and serial_map:
            # 仅在 (来源主机, BUSID) 精确命中时回填。BUSID 只在单一
            # 来源主机内有意义：按“全局唯一 busid”兜底曾把另一台来源
            # 主机的序列号错误显示到本主机清单（跨主机误归属回归）。
            serial = (
                serial_map.get((host_token, busid), "")
                or serial_map.get((attach_host, busid), "")
            )
        devices.append(serial or busid)
    return {
        "devices": devices,
        "source_os": str(result.get("source_os") or ""),
        "available": True,
        "error": "",
    }


def _refresh(device_host: str) -> None:
    try:
        entry = _enumerate(device_host)
        entry["updated_at"] = time.time()
        with _lock:
            _cache[device_host] = entry
            _inflight.discard(device_host)
    except Exception as exc:
        logger.warning(
            "[HostInventory] refresh failed for %s: %s", device_host, exc,
        )
        with _lock:
            _inflight.discard(device_host)
            _cache[device_host] = {
                "devices": [],
                "source_os": "",
                "available": False,
                "error": str(exc),
                "updated_at": time.time(),
            }


def host_local_device_inventory(device_host: str) -> dict[str, Any] | None:
    """Return the cached inventory for ``user@ip``; refresh in background.

    Returns ``None`` before the first enumeration completes. Stale entries
    trigger one background refresh; failures back off for longer so
    unreachable hosts are not SSH-hammered by the users list polling.
    """
    host = str(device_host or "").strip()
    if not host or "@" not in host:
        return None
    with _lock:
        entry = _cache.get(host)
        inflight = host in _inflight
    if entry is not None:
        ttl = (
            _CACHE_TTL_SECONDS
            if entry.get("available")
            else _FAILURE_TTL_SECONDS
        )
        expired = time.time() - float(entry.get("updated_at") or 0) > ttl
    else:
        expired = True
    if expired and not inflight:
        with _lock:
            if host not in _inflight:
                _inflight.add(host)
                thread = threading.Thread(
                    target=_refresh, args=(host,), daemon=True,
                    name=f"HostInventory-{host}",
                )
                thread.start()
    if entry is None:
        return None
    return {
        "devices": list(entry.get("devices") or []),
        "source_os": str(entry.get("source_os") or ""),
        "available": bool(entry.get("available")),
        "error": str(entry.get("error") or ""),
    }


def register_devices_port() -> None:
    """Wire this feature's host inventory into ``foundation.devices_port``.

    Called by the composition root at startup; importing this module alone
    does not wire the port, so single-module consumers keep the documented
    "no data" fallback.
    """
    devices_port.configure_host_inventory_provider(host_local_device_inventory)


__all__ = [
    "host_local_device_inventory",
    "register_devices_port",
]
