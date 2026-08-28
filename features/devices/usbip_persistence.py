"""Runtime-config persistence for USB/IP quality history and device sources."""

from __future__ import annotations

import logging
import threading
import time

from . import runtime
from .usbip import usbip_manager


logger = logging.getLogger(__name__)

# 集群分配与网络质量历史的串行写锁；integrations_api 共享同一把锁。
usbip_assignment_lock = threading.RLock()


def record_usbip_network_quality(
    device_host: str,
    worker_id: str,
    quality: dict[str, object],
) -> None:
    if not quality:
        return
    with usbip_assignment_lock:
        getter = getattr(runtime.config_manager, "get_runtime_config", None)
        runtime_config = getter() if callable(getter) else {}
        runtime_config = runtime_config or {}
        history = runtime_config.get("usbip_network_quality_history") or []
        if not isinstance(history, list):
            history = []
        history = [
            *history[-99:],
            {
                **quality,
                "device_host": device_host,
                "worker_id": worker_id,
                "timestamp": time.time(),
            },
        ]
        updater = getattr(runtime.config_manager, "update_runtime_config", None)
        if callable(updater):
            updater({"usbip_network_quality_history": history})
        else:
            runtime_config["usbip_network_quality_history"] = history
            runtime.config_manager.save_runtime_config(runtime_config)


def persist_local_usbip_sources(device_host: str, serials: list[str]) -> None:
    """Persist USB/IP source metadata used by local device-list endpoints."""
    serials = list(dict.fromkeys(
        str(serial or "").strip()
        for serial in serials
        if str(serial or "").strip()
    ))
    if not serials:
        return

    timestamp = time.time()
    updates: dict[str, dict[str, object]] = {}
    try:
        runtime_config = runtime.config_manager.get_runtime_config() or {}
        persisted = runtime_config.get("usbip_devices_source") or {}
        if not isinstance(persisted, dict):
            persisted = {}
        persisted = dict(persisted)
        for serial in serials:
            existing_source = str(
                (persisted.get(serial) or {}).get("source") or ""
            ).strip()
            if existing_source and existing_source != device_host:
                logger.info(
                    "[USB/IP] Keep existing source for %s: %s (new: %s)",
                    serial,
                    existing_source,
                    device_host,
                )
                continue
            updates[serial] = {"source": device_host, "timestamp": timestamp}
        if updates:
            persisted.update(updates)
            updater = getattr(runtime.config_manager, "update_runtime_config", None)
            if callable(updater):
                saved = updater({"usbip_devices_source": persisted})
            else:
                runtime_config["usbip_devices_source"] = persisted
                saved = runtime.config_manager.save_runtime_config(runtime_config)
            if not saved:
                raise RuntimeError("无法保存USB/IP设备来源")
    except Exception as exc:
        logger.warning("[USB/IP] Failed to persist local device sources: %s", exc)

    if not updates:
        return
    with runtime.global_state.usbip_devices_source_lock:
        runtime.global_state.usbip_devices_source.update(updates)
    getattr(usbip_manager, "device_sources", {}).update(updates)
