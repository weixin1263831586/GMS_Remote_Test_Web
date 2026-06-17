"""Background USB/IP reconnect coordination."""

import logging
import threading
import time
from typing import Any, Dict, Iterable

from core.config import config_manager
from core.state import global_state
from core.usbip import find_device_host_password, usbip_manager

logger = logging.getLogger(__name__)

USBIP_RECONNECT_ATTEMPTS = 30
USBIP_RECONNECT_INTERVAL_SECONDS = 5

_tasks: Dict[str, threading.Thread] = {}
_tasks_lock = threading.Lock()
_suppressed_hosts: Dict[str, float] = {}
_suppressed_devices: Dict[str, float] = {}
_suppression_lock = threading.Lock()
USBIP_MANUAL_DISCONNECT_SUPPRESS_SECONDS = 300


def _runtime_usbip_sources() -> Dict[str, Dict[str, Any]]:
    runtime = config_manager.get_runtime_config()
    sources = runtime.get("usbip_devices_source", {})
    return sources if isinstance(sources, dict) else {}


def _known_usbip_sources() -> Dict[str, Dict[str, Any]]:
    with global_state.usbip_devices_source_lock:
        sources = dict(global_state.usbip_devices_source)
    sources.update(getattr(usbip_manager, "device_sources", {}) or {})
    sources.update(_runtime_usbip_sources())
    return sources


def suppress_usbip_reconnect(
    device_host: str = "",
    device_ids: Iterable[str] = (),
    ttl_seconds: int = USBIP_MANUAL_DISCONNECT_SUPPRESS_SECONDS,
) -> None:
    """Temporarily suppress auto reconnect after an explicit user disconnect."""
    expires_at = time.time() + max(1, ttl_seconds)
    with _suppression_lock:
        if device_host:
            _suppressed_hosts[str(device_host).strip()] = expires_at
        for device_id in device_ids or []:
            if device_id:
                _suppressed_devices[str(device_id).strip()] = expires_at


def clear_usbip_reconnect_suppression(device_host: str = "", device_ids: Iterable[str] = ()) -> None:
    """Clear manual-disconnect suppression when the user explicitly reconnects."""
    with _suppression_lock:
        if device_host:
            _suppressed_hosts.pop(str(device_host).strip(), None)
        for device_id in device_ids or []:
            if device_id:
                _suppressed_devices.pop(str(device_id).strip(), None)


def is_usbip_reconnect_suppressed(device_host: str = "", device_id: str = "") -> bool:
    now = time.time()
    with _suppression_lock:
        for table, key in ((_suppressed_hosts, device_host), (_suppressed_devices, device_id)):
            key = str(key or "").strip()
            if not key:
                continue
            expires_at = table.get(key)
            if expires_at and expires_at > now:
                return True
            if expires_at:
                table.pop(key, None)
    return False


def schedule_usbip_reconnect_for_removed_devices(
    removed_devices: Iterable[str],
    reason: str = "USB/IP device removed",
) -> list[str]:
    """Schedule reconnect for any removed device that has a known USB/IP source."""
    known_sources = _known_usbip_sources()
    scheduled_hosts: list[str] = []
    for device_id in removed_devices or []:
        source_info = known_sources.get(device_id) or {}
        device_host = str(source_info.get("source") or "").strip()
        if not device_host:
            continue
        if is_usbip_reconnect_suppressed(device_host, device_id):
            logger.info(
                "[USB/IP Reconnect] suppressed for %s/%s (%s)",
                device_host,
                device_id,
                reason,
            )
            continue
        if schedule_usbip_reconnect(device_host, reason=f"{reason}: {device_id}"):
            scheduled_hosts.append(device_host)
    return scheduled_hosts


def schedule_usbip_reconnect_for_missing_devices(
    current_devices: Iterable[str],
    reason: str = "USB/IP persisted device missing",
) -> list[str]:
    """Schedule reconnect for persisted USB/IP devices that are not currently online."""
    current_set = set(current_devices or [])
    scheduled_hosts: list[str] = []
    for device_id, source_info in _runtime_usbip_sources().items():
        if device_id in current_set:
            continue
        device_host = str((source_info or {}).get("source") or "").strip()
        if not device_host:
            continue
        if is_usbip_reconnect_suppressed(device_host, device_id):
            logger.info(
                "[USB/IP Reconnect] startup/missing reconnect suppressed for %s/%s (%s)",
                device_host,
                device_id,
                reason,
            )
            continue
        if schedule_usbip_reconnect(device_host, reason=f"{reason}: {device_id}"):
            scheduled_hosts.append(device_host)
    return scheduled_hosts


def schedule_usbip_reconnect(device_host: str, reason: str = "") -> bool:
    """Start a background reconnect worker for a device host if one is not running."""
    device_host = str(device_host or "").strip()
    if not device_host:
        return False
    if is_usbip_reconnect_suppressed(device_host):
        logger.info("[USB/IP Reconnect] suppressed for %s (%s)", device_host, reason)
        return False

    with _tasks_lock:
        existing = _tasks.get(device_host)
        if existing and existing.is_alive():
            logger.info("[USB/IP Reconnect] already running for %s", device_host)
            return False

        worker = threading.Thread(
            target=_reconnect_worker,
            args=(device_host, reason),
            name=f"USBIPReconnect-{device_host}",
            daemon=True,
        )
        _tasks[device_host] = worker
        worker.start()
        logger.info("[USB/IP Reconnect] scheduled for %s (%s)", device_host, reason)
        return True


def _reconnect_worker(device_host: str, reason: str = ""):
    try:
        for attempt in range(1, USBIP_RECONNECT_ATTEMPTS + 1):
            if attempt > 1:
                time.sleep(USBIP_RECONNECT_INTERVAL_SECONDS)

            config = config_manager.load_config(force_reload=True)
            device_password = find_device_host_password(device_host, config) or config.get("device_pswd", "")
            if not device_password:
                logger.warning("[USB/IP Reconnect] no SSH credential for %s", device_host)
                return

            logger.info(
                "[USB/IP Reconnect] attempt %s/%s for %s (%s)",
                attempt,
                USBIP_RECONNECT_ATTEMPTS,
                device_host,
                reason,
            )
            if is_usbip_reconnect_suppressed(device_host):
                logger.info("[USB/IP Reconnect] stopped by manual disconnect suppression for %s", device_host)
                return
            result = usbip_manager.start_usbip(device_host, device_password)
            device_list = result.get("device_list") or []
            if result.get("success") and device_list:
                _record_reconnected_devices(device_host, device_list)
                logger.info(
                    "[USB/IP Reconnect] success for %s, devices=%s",
                    device_host,
                    device_list,
                )
                return

            logger.info(
                "[USB/IP Reconnect] not ready for %s: success=%s devices=%s error=%s",
                device_host,
                result.get("success"),
                device_list,
                result.get("error") or result.get("message"),
            )

        logger.error("[USB/IP Reconnect] exhausted attempts for %s", device_host)
    finally:
        with _tasks_lock:
            current = _tasks.get(device_host)
            if current is threading.current_thread():
                _tasks.pop(device_host, None)


def _record_reconnected_devices(device_host: str, device_list: list[str]):
    now = time.time()
    with global_state.usbip_states_lock:
        global_state.usbip_states[device_host] = {"connected": True, "timestamp": now}

    with global_state.usbip_devices_source_lock:
        for device_id in device_list:
            global_state.usbip_devices_source[device_id] = {
                "source": device_host,
                "timestamp": now,
            }

    with global_state.device_cache_lock:
        global_state.device_cache = {"devices": [], "timestamp": 0}

    try:
        runtime = config_manager.get_runtime_config()
        sources = runtime.get("usbip_devices_source", {})
        if not isinstance(sources, dict):
            sources = {}
        for device_id in device_list:
            sources[device_id] = {"source": device_host, "timestamp": now}
        runtime["usbip_devices_source"] = sources
        config_manager.save_runtime_config(runtime)
    except Exception as exc:
        logger.warning("[USB/IP Reconnect] failed to persist sources: %s", exc)
