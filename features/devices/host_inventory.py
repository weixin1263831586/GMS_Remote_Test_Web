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

from .usbip import usbip_manager


logger = logging.getLogger(__name__)

_CACHE_TTL_SECONDS = 60.0
_FAILURE_TTL_SECONDS = 300.0

_cache: dict[str, dict[str, Any]] = {}
_inflight: set[str] = set()
_lock = threading.Lock()


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
    devices: list[str] = []
    for item in result.get("devices") or []:
        serial = str(item.get("serial") or "").strip()
        busid = str(item.get("busid") or "").strip()
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
