"""Background USB/IP reconnect coordination."""

import logging
import threading
import time
from collections.abc import Iterable
from typing import Any

from . import runtime
from .manager import device_manager, has_blocked_adb_process
from .usbip import find_device_host_password, usbip_manager
from .usbip_flash import resolve_usbip_flash_routes
from .usbip_persistence import migrate_local_usbip_busid
from .usbip_transport_probe import probe_existing_local_usbip_transport


logger = logging.getLogger(__name__)

USBIP_RECONNECT_ATTEMPTS = 30
USBIP_RECONNECT_INTERVAL_SECONDS = 5
USBIP_RECONNECT_STABLE_CHECKS = 3
USBIP_RECONNECT_STABLE_INTERVAL_SECONDS = 2

_tasks: dict[str, threading.Thread] = {}
_stop_events: dict[str, threading.Event] = {}
_transport_only_hosts: set[str] = set()
_tasks_lock = threading.Lock()
_suppressed_hosts: dict[str, float] = {}
_suppressed_devices: dict[str, float] = {}
_suppression_lock = threading.Lock()
USBIP_MANUAL_DISCONNECT_SUPPRESS_SECONDS = 300

# Firmware flashing owns the selected physical USB/IP route while the device
# changes ADB/Loader/MaskROM identities.  This pause is deliberately separate
# from manual-disconnect suppression: paused devices remain visible in normal
# device views, but generic disappearance monitoring must not start a second
# attach loop against the same BUSID.
_paused_hosts: dict[str, float] = {}
_paused_devices: dict[str, float] = {}
_pause_lock = threading.Lock()
USBIP_FIRMWARE_RECONNECT_PAUSE_SECONDS = 2 * 60 * 60


def _runtime_usbip_sources() -> dict[str, dict[str, Any]]:
    runtime_config = runtime.config_manager.get_runtime_config()
    sources = runtime_config.get("usbip_devices_source", {})
    return sources if isinstance(sources, dict) else {}


def _known_usbip_sources() -> dict[str, dict[str, Any]]:
    with runtime.global_state.usbip_devices_source_lock:
        sources = dict(runtime.global_state.usbip_devices_source)
    sources.update(getattr(usbip_manager, "device_sources", {}) or {})
    sources.update(_runtime_usbip_sources())
    return sources


def usbip_source_host_for_device(device_id: str) -> str:
    """Return the persisted USB/IP source host for an Android serial."""
    source_info = _known_usbip_sources().get(str(device_id or "").strip()) or {}
    return str(source_info.get("source") or "").strip()


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


def pause_usbip_reconnect(
    device_host: str = "",
    device_ids: Iterable[str] = (),
    ttl_seconds: int = USBIP_FIRMWARE_RECONNECT_PAUSE_SECONDS,
) -> None:
    """Pause generic reconnect without hiding an owned firmware device."""
    expires_at = time.time() + max(1, ttl_seconds)
    with _pause_lock:
        if device_host:
            _paused_hosts[str(device_host).strip()] = expires_at
        for device_id in device_ids or []:
            if device_id:
                _paused_devices[str(device_id).strip()] = expires_at


def resume_usbip_reconnect(
    device_host: str = "", device_ids: Iterable[str] = (),
) -> None:
    """Release a firmware reconnect pause."""
    with _pause_lock:
        if device_host:
            _paused_hosts.pop(str(device_host).strip(), None)
        for device_id in device_ids or []:
            if device_id:
                _paused_devices.pop(str(device_id).strip(), None)


def is_usbip_reconnect_paused(
    device_host: str = "", device_id: str = "",
) -> bool:
    now = time.time()
    with _pause_lock:
        for table, key in (
            (_paused_hosts, device_host),
            (_paused_devices, device_id),
        ):
            key = str(key or "").strip()
            if not key:
                continue
            expires_at = table.get(key)
            if expires_at and expires_at > now:
                return True
            if expires_at:
                table.pop(key, None)
    return False


def _reconnect_paused_for_devices(
    device_host: str, device_ids: Iterable[str],
) -> bool:
    if is_usbip_reconnect_paused(device_host=device_host):
        return True
    return any(
        is_usbip_reconnect_paused(device_id=device_id)
        for device_id in device_ids or ()
    )


def _normalize_host(host: str) -> str:
    """Strip user@ prefix and whitespace so host identities compare reliably."""
    host = str(host or "").strip()
    if "@" in host:
        host = host.split("@", 1)[-1]
    return host


def _local_worker_id() -> str:
    try:
        from foundation.cluster_port import get_cluster_service

        return str(get_cluster_service().config.local_worker_id or "")
    except Exception:
        return ""


def _resolved_busids_for_devices(device_ids: set[str]) -> list[str]:
    """Resolve assigned physical BUSIDs for the given Android serials.

    Returns [] when no persistent assignment matches so callers can keep
    their whole-host fallback.
    """
    if not device_ids:
        return []
    try:
        routes = resolve_usbip_flash_routes(sorted(device_ids)) or []
    except Exception as exc:
        logger.warning("[USB/IP Reconnect] busid resolution failed: %s", exc)
        return []
    busids: list[str] = []
    for route in routes:
        for busid in route.get("busids") or []:
            busid = str(busid or "").strip()
            if busid and busid not in busids:
                busids.append(busid)
    return busids


def _refresh_reenumerated_busids(
    device_host: str,
    expected_devices: set[str],
    selected_busids: list[str],
    device_password: str,
) -> list[str]:
    """Replace vanished BUSIDs only when source serial identity is exact."""
    if not expected_devices or not selected_busids:
        return selected_busids
    list_source_devices = getattr(usbip_manager, "list_source_devices", None)
    if not callable(list_source_devices):
        return selected_busids
    inventory = list_source_devices(device_host, device_password)
    if not isinstance(inventory, dict) or not inventory.get("success"):
        return selected_busids
    source_devices = [
        item for item in inventory.get("devices") or []
        if isinstance(item, dict) and str(item.get("busid") or "").strip()
    ]
    current_busids = {
        str(item.get("busid") or "").strip() for item in source_devices
    }
    missing_busids = [
        busid for busid in selected_busids if busid not in current_busids
    ]
    if not missing_busids:
        return selected_busids

    runtime_config = runtime.config_manager.get_runtime_config() or {}
    assignments = runtime_config.get("usbip_cluster_assignments") or {}
    if not isinstance(assignments, dict):
        return selected_busids
    refreshed = list(selected_busids)
    claimed_new_busids: set[str] = set()
    for old_busid in missing_busids:
        assignment = assignments.get(f"{device_host}|{old_busid}") or {}
        assigned_serials = {
            str(serial or "").strip()
            for serial in assignment.get("device_serials") or []
            if str(serial or "").strip()
        }
        if not assigned_serials or not assigned_serials.issubset(expected_devices):
            continue
        matches = [
            item for item in source_devices
            if str(item.get("serial") or "").strip() in assigned_serials
            and str(item.get("busid") or "").strip() not in claimed_new_busids
        ]
        if len(matches) != 1:
            continue
        source_device = matches[0]
        new_busid = str(source_device.get("busid") or "").strip()
        if new_busid in selected_busids:
            continue
        migrated, error = migrate_local_usbip_busid(
            device_host=device_host,
            old_busid=old_busid,
            new_busid=new_busid,
            expected_serials=assigned_serials,
            source_device=source_device,
        )
        if not migrated:
            logger.warning(
                "[USB/IP Reconnect] BUSID migration refused for %s/%s -> %s: %s",
                device_host, old_busid, new_busid, error,
            )
            continue
        refreshed[refreshed.index(old_busid)] = new_busid
        claimed_new_busids.add(new_busid)
        logger.info(
            "[USB/IP Reconnect] migrated re-enumerated BUSID for %s: %s -> %s (%s)",
            device_host, old_busid, new_busid,
            ", ".join(sorted(assigned_serials)),
        )
    return refreshed


def _device_host_has_remote_assignment(device_host: str) -> bool:
    """Return True if any busid on this source host is assigned to a remote Worker.

    The local reconnect worker re-attaches a whole source host's USB/IP exports.
    If those exports were explicitly assigned to a remote Worker, a blind local
    re-attach steals them back and the two hosts flap the device between each
    other. ``usbip_cluster_assignments`` is written by the connect/disconnect
    endpoints and is the source of truth for cluster-wide ownership.
    """
    target = _normalize_host(device_host)
    if not target:
        return False
    local = _local_worker_id()
    now = time.time()
    getter = getattr(runtime.config_manager, "get_runtime_config", None)
    runtime_config = getter() if callable(getter) else {}
    runtime_config = runtime_config or {}
    assignments = runtime_config.get("usbip_cluster_assignments") or {}
    if not isinstance(assignments, dict):
        return False
    for info in assignments.values():
        if not isinstance(info, dict):
            continue
        status = info.get("status")
        worker_id = str(info.get("worker_id") or "")
        if not worker_id or worker_id == local:
            continue
        # "attaching" is transient; ignore stale entries left by a crashed attach.
        if status == "attaching" and now - float(info.get("timestamp") or 0) > 120:
            continue
        if status not in {
            "attaching", "attached", "unknown", "cleanup_required",
        }:
            continue
        candidates = {
            _normalize_host(info.get("device_host")),
            _normalize_host(info.get("source_host")),
        }
        if target in candidates:
            return True
    return False


def filter_suppressed_usbip_devices(devices: Iterable[str]) -> list[str]:
    """Hide manually disconnected USB/IP serials from generic device views."""
    filtered: list[str] = []
    for device_id in devices or []:
        device_id = str(device_id or "").strip()
        if not device_id:
            continue
        if is_usbip_reconnect_suppressed(device_id=device_id):
            logger.info("[USB/IP] hiding manually disconnected device from generic list: %s", device_id)
            continue
        filtered.append(device_id)
    return filtered


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
        if _reconnect_paused_for_devices(device_host, [device_id]):
            logger.info(
                "[USB/IP Reconnect] paused by firmware ownership for %s/%s (%s)",
                device_host,
                device_id,
                reason,
            )
            continue
        _mark_usbip_reconnecting(device_host, [device_id], reason)
        if schedule_usbip_reconnect(
            device_host,
            reason=f"{reason}: {device_id}",
            expected_devices=[device_id],
        ):
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
        if _reconnect_paused_for_devices(device_host, [device_id]):
            logger.info(
                "[USB/IP Reconnect] startup/missing reconnect paused by firmware "
                "ownership for %s/%s (%s)",
                device_host,
                device_id,
                reason,
            )
            continue
        if schedule_usbip_reconnect(
            device_host,
            reason=f"{reason}: {device_id}",
            expected_devices=[device_id],
        ):
            scheduled_hosts.append(device_host)
    return scheduled_hosts


def schedule_usbip_reconnect(
    device_host: str,
    reason: str = "",
    expected_devices: Iterable[str] = (),
    *,
    accept_transport_only: bool = False,
) -> bool:
    """Start a background reconnect worker for a device host if one is not running.

    ``accept_transport_only`` is reserved for intentional flash-mode changes,
    where RockUSB Loader is valid even though neither ADB nor Fastboot is
    visible. Generic disappearance monitoring keeps waiting for a protocol.
    """
    device_host = str(device_host or "").strip()
    expected_devices = tuple(dict.fromkeys(expected_devices or ()))
    if not device_host:
        return False
    if is_usbip_reconnect_suppressed(device_host):
        logger.info("[USB/IP Reconnect] suppressed for %s (%s)", device_host, reason)
        return False
    if _reconnect_paused_for_devices(device_host, expected_devices):
        logger.info(
            "[USB/IP Reconnect] paused by firmware ownership for %s (%s)",
            device_host,
            reason,
        )
        return False
    if _device_host_has_remote_assignment(device_host):
        logger.info(
            "[USB/IP Reconnect] %s is assigned to a remote Worker, skipping local reconnect (%s)",
            device_host,
            reason,
        )
        return False

    with _tasks_lock:
        if accept_transport_only:
            # A flash-mode request may race the generic ADB removal monitor.
            # Upgrade an already-running task so Loader's protocol-less USB
            # transport is accepted instead of repeatedly detached.
            _transport_only_hosts.add(device_host)
        existing = _tasks.get(device_host)
        if existing and existing.is_alive():
            logger.info("[USB/IP Reconnect] already running for %s", device_host)
            return False

        stop_event = threading.Event()
        worker = threading.Thread(
            target=_reconnect_worker,
            args=(device_host, reason, stop_event, expected_devices),
            name=f"USBIPReconnect-{device_host}",
            daemon=True,
        )
        _tasks[device_host] = worker
        _stop_events[device_host] = stop_event
        worker.start()
        logger.info("[USB/IP Reconnect] scheduled for %s (%s)", device_host, reason)
        return True


def active_usbip_reconnect_hosts() -> list[str]:
    with _tasks_lock:
        return sorted(
            host for host, task in _tasks.items() if task.is_alive()
        )


def reconcile_observed_usbip_devices(current_devices: Iterable[str]) -> dict[str, list[str]]:
    """Promote reconnecting USB/IP devices when they reappear in ADB output."""
    current_set = {str(device_id) for device_id in current_devices or [] if str(device_id)}
    if not current_set:
        return {}

    restored: dict[str, list[str]] = {}
    with runtime.global_state.usbip_states_lock:
        state_items = list(runtime.global_state.usbip_states.items())

    for device_host, state_info in state_items:
        if not isinstance(state_info, dict) or not state_info.get("reconnecting"):
            continue
        expected = {
            str(device_id)
            for device_id in state_info.get("expected_devices", [])
            if str(device_id)
        }
        observed = sorted(expected & current_set)
        if not observed:
            continue
        _record_reconnected_devices(
            device_host,
            observed,
            {
                "transport_connected": True,
                "protocol_status": {
                    "mode": "adb",
                    "adb_ready": observed,
                },
            },
        )
        restored[device_host] = observed

    return restored


def stop_usbip_reconnect_tasks(timeout: float = 5) -> None:
    with _tasks_lock:
        tasks = list(_tasks.items())
        for stop_event in _stop_events.values():
            stop_event.set()

    deadline = time.monotonic() + max(0, timeout)
    for _, task in tasks:
        remaining = max(0, deadline - time.monotonic())
        task.join(timeout=remaining)

    with _tasks_lock:
        for host, task in list(_tasks.items()):
            if not task.is_alive():
                _tasks.pop(host, None)
                _stop_events.pop(host, None)
                _transport_only_hosts.discard(host)


def stop_usbip_reconnect_for_host(device_host: str, timeout: float = 5) -> None:
    """Stop an in-flight reconnect worker for a single host."""
    device_host = str(device_host or "").strip()
    if not device_host:
        return
    with _tasks_lock:
        task = _tasks.get(device_host)
        stop_event = _stop_events.get(device_host)
        if stop_event:
            stop_event.set()

    if task and task is not threading.current_thread():
        task.join(timeout=max(0, timeout))

    with _tasks_lock:
        current = _tasks.get(device_host)
        if current is task and (not task or not task.is_alive()):
            _tasks.pop(device_host, None)
            _stop_events.pop(device_host, None)
            _transport_only_hosts.discard(device_host)


def _reconnect_worker(
    device_host: str,
    reason: str = "",
    stop_event: threading.Event | None = None,
    expected_devices: Iterable[str] = (),
):
    stop_event = stop_event or threading.Event()
    expected_set = {str(device_id) for device_id in expected_devices or [] if str(device_id)}
    try:
        for attempt in range(1, USBIP_RECONNECT_ATTEMPTS + 1):
            if stop_event.is_set():
                return
            if _reconnect_paused_for_devices(device_host, expected_set):
                logger.info(
                    "[USB/IP Reconnect] stopped by firmware ownership for %s",
                    device_host,
                )
                return
            if attempt > 1 and stop_event.wait(USBIP_RECONNECT_INTERVAL_SECONDS):
                return
            if has_blocked_adb_process():
                logger.warning(
                    "[USB/IP Reconnect] paused for %s because local adb is blocked in kernel state",
                    device_host,
                )
                # ADB can be transiently blocked while the USB gadget is
                # disappearing. Keep the scheduled task alive so the new
                # Fastboot/Loader identity can be bound on the next attempt.
                continue

            config = runtime.config_manager.load_config(force_reload=True)
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
            if _reconnect_paused_for_devices(device_host, expected_set):
                logger.info(
                    "[USB/IP Reconnect] stopped by firmware ownership for %s",
                    device_host,
                )
                return
            if stop_event.is_set():
                return
            if _device_host_has_remote_assignment(device_host):
                logger.info(
                    "[USB/IP Reconnect] stopping for %s, its devices were reassigned to a remote Worker",
                    device_host,
                )
                return
            existing_transport = probe_existing_local_usbip_transport(
                device_host, expected_set, config,
                local_worker_id=_local_worker_id(),
            )
            if existing_transport:
                device_list = existing_transport.get("device_list") or []
                _record_reconnected_devices(device_host, device_list, existing_transport)
                logger.info(
                    "[USB/IP Reconnect] preserving existing transport for %s, protocol=%s devices=%s",
                    device_host,
                    (existing_transport.get("protocol_status") or {}).get("mode", "unknown"),
                    device_list,
                )
                return

            device_password = find_device_host_password(device_host, config) or config.get("device_pswd", "")
            if not device_password:
                logger.warning("[USB/IP Reconnect] no SSH credential for %s", device_host)
                return
            with _tasks_lock:
                accept_transport_only = device_host in _transport_only_hosts
            start_kwargs = (
                {"allow_transport_only": True}
                if accept_transport_only
                else {}
            )
            # 定向重连：按已分配的物理 BUSID 限定作用范围，避免同一
            # Windows 主机上其他设备的传输被连带 detach/attach。只要调用方
            # 指定了期望设备，就不允许退化为整主机发现；缺失分配记录需要
            # 用户重新选择设备建立映射，不能拿同主机其他设备冒险兜底。
            selected_busids = _resolved_busids_for_devices(expected_set)
            if selected_busids:
                selected_busids = _refresh_reenumerated_busids(
                    device_host,
                    expected_set,
                    selected_busids,
                    device_password,
                )
                start_kwargs["selected_busids"] = selected_busids
            elif expected_set:
                detail = (
                    "missing persistent BUSID assignment for "
                    + ", ".join(sorted(expected_set))
                )
                _mark_usbip_reconnecting(
                    device_host, expected_set, f"{reason}; {detail}",
                )
                logger.error(
                    "[USB/IP Reconnect] refusing whole-host fallback for %s: %s",
                    device_host, detail,
                )
                return
            result = usbip_manager.start_usbip(
                device_host,
                device_password,
                **start_kwargs,
            )
            if (
                is_usbip_reconnect_suppressed(device_host)
                or _reconnect_paused_for_devices(device_host, expected_set)
                or stop_event.is_set()
            ):
                logger.info(
                    "[USB/IP Reconnect] result ignored after reconnect was "
                    "stopped or paused for %s",
                    device_host,
                )
                return
            device_list = result.get("device_list") or []
            if result.get("success") and result.get("transport_connected", True):
                if device_list and _usbip_devices_stable(
                    device_host,
                    device_list,
                    expected_set,
                    stop_event,
                ):
                    _record_reconnected_devices(device_host, device_list, result)
                    logger.info(
                        "[USB/IP Reconnect] success for %s, devices=%s",
                        device_host,
                        device_list,
                    )
                    return
                if not device_list:
                    protocol_mode = (result.get("protocol_status") or {}).get("mode", "unknown")
                    with _tasks_lock:
                        accept_transport_only = device_host in _transport_only_hosts
                    # RockUSB Loader has no ADB/Fastboot protocol endpoint, so
                    # an attached transport with an unknown protocol is a valid
                    # flash-ready state. Retrying here would detach the active
                    # vhci port and can interrupt upgrade_tool mid-burn.
                    if protocol_mode != "unknown" or accept_transport_only:
                        _record_reconnected_devices(device_host, [], result)
                        logger.info(
                            "[USB/IP Reconnect] transport restored for %s, protocol=%s",
                            device_host,
                            protocol_mode,
                        )
                        return
                    _mark_usbip_reconnecting(
                        device_host,
                        expected_set,
                        f"{reason}; protocol={protocol_mode}",
                    )
                    logger.info(
                        "[USB/IP Reconnect] transport attached but protocol not visible for %s, continue waiting",
                        device_host,
                    )

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
                _stop_events.pop(device_host, None)
                _transport_only_hosts.discard(device_host)


def _usbip_devices_stable(
    device_host: str,
    device_list: Iterable[str],
    expected_devices: set[str],
    stop_event: threading.Event,
) -> bool:
    """Return True only after USB/IP ADB devices stay visible for consecutive checks."""
    observed = {str(device_id) for device_id in device_list or [] if str(device_id)}
    if expected_devices and not expected_devices.issubset(observed):
        logger.info(
            "[USB/IP Reconnect] expected devices not all returned for %s: expected=%s observed=%s",
            device_host,
            sorted(expected_devices),
            sorted(observed),
        )
        return False

    for check_index in range(USBIP_RECONNECT_STABLE_CHECKS):
        if stop_event.is_set():
            return False
        if check_index and stop_event.wait(USBIP_RECONNECT_STABLE_INTERVAL_SECONDS):
            return False
        if has_blocked_adb_process():
            logger.warning(
                "[USB/IP Reconnect] stable check paused for %s because local adb is blocked",
                device_host,
            )
            return False
        current = set(device_manager.get_connected_devices(force_refresh=True))
        if not observed.issubset(current):
            logger.info(
                "[USB/IP Reconnect] devices not stable for %s: expected_online=%s current=%s",
                device_host,
                sorted(observed),
                sorted(current),
            )
            return False
    return True


def _record_reconnected_devices(
    device_host: str,
    device_list: list[str],
    result: dict[str, Any] | None = None,
):
    now = time.time()
    with runtime.global_state.usbip_states_lock:
        runtime.global_state.usbip_states[device_host] = {
            "connected": True,
            "timestamp": now,
            "transport_connected": bool((result or {}).get("transport_connected", True)),
            "adb_ready": bool(device_list),
            "reconnecting": False,
            "protocol_status": (result or {}).get("protocol_status") or {},
        }

    with runtime.global_state.usbip_devices_source_lock:
        for device_id in device_list:
            runtime.global_state.usbip_devices_source[device_id] = {
                "source": device_host,
                "timestamp": now,
            }

    with runtime.global_state.device_cache_lock:
        runtime.global_state.device_cache = {"devices": [], "timestamp": 0}

    try:
        runtime_config = runtime.config_manager.get_runtime_config()
        sources = runtime_config.get("usbip_devices_source", {})
        if not isinstance(sources, dict):
            sources = {}
        for device_id in device_list:
            sources[device_id] = {"source": device_host, "timestamp": now}
        runtime_config["usbip_devices_source"] = sources
        runtime.config_manager.save_runtime_config(runtime_config)
    except Exception as exc:
        logger.warning("[USB/IP Reconnect] failed to persist sources: %s", exc)


def _mark_usbip_reconnecting(
    device_host: str,
    expected_devices: Iterable[str],
    reason: str = "",
) -> None:
    now = time.time()
    with runtime.global_state.usbip_states_lock:
        runtime.global_state.usbip_states[device_host] = {
            "connected": True,
            "timestamp": now,
            "transport_connected": False,
            "adb_ready": False,
            "reconnecting": True,
            "expected_devices": list(dict.fromkeys(
                str(device_id)
                for device_id in expected_devices or []
                if str(device_id)
            )),
            "reason": reason,
            "protocol_status": {"mode": "reconnecting"},
        }
