"""Runtime-config persistence for USB/IP quality history and device sources."""

from __future__ import annotations

import logging
import re
import threading
import time
import uuid

from . import runtime
from .usbip import usbip_manager


logger = logging.getLogger(__name__)

# 集群分配与网络质量历史的串行写锁；integrations_api 共享同一把锁。
usbip_assignment_lock = threading.RLock()
_DEVICE_SERIAL_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_USBIP_BUSID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def migrate_local_usbip_busid(
    *,
    device_host: str,
    old_busid: str,
    new_busid: str,
    expected_serials: set[str],
    source_device: dict[str, object] | None = None,
) -> tuple[bool, str]:
    """Move one persisted local assignment after safe source re-enumeration.

    The caller must already have matched the new source BUSID by an exact,
    unique Android serial.  Re-check the assignment under the shared lock so
    a concurrent manual connect/disconnect cannot be overwritten.
    """
    device_host = str(device_host or "").strip()
    old_busid = str(old_busid or "").strip()
    new_busid = str(new_busid or "").strip()
    expected = {
        str(serial or "").strip()
        for serial in expected_serials or set()
        if str(serial or "").strip()
    }
    if (
        not device_host
        or not _USBIP_BUSID_RE.fullmatch(old_busid)
        or not _USBIP_BUSID_RE.fullmatch(new_busid)
        or old_busid == new_busid
        or not expected
    ):
        return False, "USB/IP BUSID迁移参数无效"

    timestamp = time.time()
    with usbip_assignment_lock:
        runtime_config = runtime.config_manager.get_runtime_config() or {}
        assignments = runtime_config.get("usbip_cluster_assignments") or {}
        if not isinstance(assignments, dict):
            return False, "USB/IP持久分配记录格式无效"
        assignments = dict(assignments)
        old_key = f"{device_host}|{old_busid}"
        new_key = f"{device_host}|{new_busid}"
        current = assignments.get(old_key)
        if not isinstance(current, dict):
            return False, f"找不到USB/IP物理分配 {device_host}/{old_busid}"
        if str(current.get("status") or "") not in {
            "attaching", "attached", "unknown", "cleanup_required",
        }:
            return False, "USB/IP物理分配已不再活动"
        assigned = {
            str(serial or "").strip()
            for serial in current.get("device_serials") or []
            if str(serial or "").strip()
        }
        if assigned != expected:
            return False, "USB/IP物理分配序列号已变化，拒绝迁移"
        if new_key in assignments:
            return False, f"新BUSID {new_busid} 已存在USB/IP分配"

        next_generation = max(
            (int(item.get("generation") or 0) for item in assignments.values()
             if isinstance(item, dict)),
            default=0,
        ) + 1
        identity = source_device or {}
        migrated = {
            **current,
            "busid": new_busid,
            "status": "unknown",
            "generation": next_generation,
            "operation_id": f"usbip-reenumerate-{uuid.uuid4().hex}",
            "timestamp": timestamp,
        }
        for field in (
            "physical_device_id", "identity_source", "identity_stable",
            "usb_serial", "container_id", "pnp_instance_id",
            "location_path", "vid_pid",
        ):
            if identity.get(field) not in {None, ""}:
                migrated[field] = identity[field]
        assignments.pop(old_key)
        assignments[new_key] = migrated

        updater = getattr(runtime.config_manager, "update_runtime_config", None)
        if callable(updater):
            saved = updater({"usbip_cluster_assignments": assignments})
        else:
            runtime_config["usbip_cluster_assignments"] = assignments
            saved = runtime.config_manager.save_runtime_config(runtime_config)
        if not saved:
            return False, "保存USB/IP新BUSID分配失败"
    return True, ""


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


def migrate_local_usbip_serial(
    *,
    device_host: str,
    busid: str,
    old_serial: str,
    new_serial: str,
    expected_generation: int | None = None,
    expected_operation_id: str = "",
) -> tuple[bool, str]:
    """Atomically move one physical USB/IP assignment to a new ADB serial.

    Firmware may replace Android's USB iSerial.  The migration is accepted
    only for the exact assignment that the burn locked before rebooting; a
    concurrent detach/attach generation change fails closed.
    """
    device_host = str(device_host or "").strip()
    busid = str(busid or "").strip()
    old_serial = str(old_serial or "").strip()
    new_serial = str(new_serial or "").strip()
    if not all((device_host, busid, old_serial, new_serial)):
        return False, "USB/IP序列号迁移参数不完整"
    if not _DEVICE_SERIAL_RE.fullmatch(old_serial) or not _DEVICE_SERIAL_RE.fullmatch(
        new_serial
    ):
        return False, "USB/IP序列号格式无效"
    if old_serial == new_serial:
        return True, ""

    timestamp = time.time()
    with usbip_assignment_lock:
        runtime_config = runtime.config_manager.get_runtime_config() or {}
        assignments = runtime_config.get("usbip_cluster_assignments") or {}
        if not isinstance(assignments, dict):
            return False, "USB/IP持久分配记录格式无效"
        assignments = dict(assignments)
        match_key = ""
        current: dict[str, object] = {}
        for key, value in assignments.items():
            if not isinstance(value, dict):
                continue
            if (
                str(value.get("device_host") or "").strip() == device_host
                and str(value.get("busid") or "").strip() == busid
            ):
                match_key = key
                current = dict(value)
                break
        if not match_key:
            return False, f"找不到USB/IP物理分配 {device_host}/{busid}"

        for key, value in assignments.items():
            if key == match_key or not isinstance(value, dict):
                continue
            other_serials = {
                str(serial or "").strip()
                for serial in value.get("device_serials") or []
                if str(serial or "").strip()
            }
            if new_serial in other_serials:
                return False, (
                    f"新序列号 {new_serial} 已属于其他USB/IP物理分配 {key}"
                )

        assigned_serials = {
            str(value or "").strip()
            for value in current.get("device_serials") or []
            if str(value or "").strip()
        }
        if old_serial not in assigned_serials:
            return False, (
                f"USB/IP物理分配已变化：{device_host}/{busid} 当前序列号为 "
                + (", ".join(sorted(assigned_serials)) or "空")
            )
        current_generation = int(current.get("generation") or 0)
        if (
            expected_generation is not None
            and current_generation != int(expected_generation)
        ):
            return False, "USB/IP物理分配代次已变化，拒绝迁移旧烧写任务"
        current_operation_id = str(current.get("operation_id") or "")
        if (
            expected_operation_id
            and current_operation_id != str(expected_operation_id)
        ):
            return False, "USB/IP物理分配操作已变化，拒绝迁移旧烧写任务"

        current.update({
            "device_serials": [new_serial],
            "status": "attached",
            "timestamp": timestamp,
        })
        assignments[match_key] = current

        persisted = runtime_config.get("usbip_devices_source") or {}
        persisted = dict(persisted) if isinstance(persisted, dict) else {}
        old_source = str((persisted.get(old_serial) or {}).get("source") or "")
        new_source = str((persisted.get(new_serial) or {}).get("source") or "")
        if new_source and new_source != device_host:
            return False, (
                f"新序列号 {new_serial} 已属于其他USB/IP来源 {new_source}"
            )
        if not old_source or old_source == device_host:
            persisted.pop(old_serial, None)
        persisted[new_serial] = {
            "source": device_host,
            "timestamp": timestamp,
        }
        updater = getattr(runtime.config_manager, "update_runtime_config", None)
        updates = {
            "usbip_cluster_assignments": assignments,
            "usbip_devices_source": persisted,
        }
        if callable(updater):
            saved = updater(updates)
        else:
            runtime_config.update(updates)
            saved = runtime.config_manager.save_runtime_config(runtime_config)
        if not saved:
            return False, "保存USB/IP新序列号分配失败"

    source_info = {"source": device_host, "timestamp": timestamp}
    with runtime.global_state.usbip_devices_source_lock:
        existing = runtime.global_state.usbip_devices_source.get(old_serial) or {}
        if not existing or str(existing.get("source") or "") == device_host:
            runtime.global_state.usbip_devices_source.pop(old_serial, None)
        runtime.global_state.usbip_devices_source[new_serial] = source_info
    device_sources = getattr(usbip_manager, "device_sources", {})
    existing = device_sources.get(old_serial) or {}
    if not existing or str(existing.get("source") or "") == device_host:
        device_sources.pop(old_serial, None)
    device_sources[new_serial] = source_info

    with runtime.global_state.usbip_states_lock:
        state = dict(runtime.global_state.usbip_states.get(device_host) or {})
        expected_devices = [
            str(serial or "").strip()
            for serial in state.get("expected_devices") or []
            if str(serial or "").strip() and str(serial or "").strip() != old_serial
        ]
        if new_serial not in expected_devices:
            expected_devices.append(new_serial)
        protocol_status = dict(state.get("protocol_status") or {})
        adb_states = dict(protocol_status.get("adb") or {})
        adb_states.pop(old_serial, None)
        adb_states[new_serial] = "device"
        adb_ready = [
            str(serial or "").strip()
            for serial in protocol_status.get("adb_ready") or []
            if str(serial or "").strip() and str(serial or "").strip() != old_serial
        ]
        if new_serial not in adb_ready:
            adb_ready.append(new_serial)
        for key in ("fastboot", "recovery", "sideload", "unauthorized", "offline"):
            protocol_status[key] = [
                str(serial or "").strip()
                for serial in protocol_status.get(key) or []
                if str(serial or "").strip() and str(serial or "").strip() != old_serial
            ]
        protocol_status.update({
            "mode": "adb",
            "adb": adb_states,
            "adb_ready": adb_ready,
        })
        state.update({
            "connected": True,
            "transport_connected": True,
            "adb_ready": True,
            "reconnecting": False,
            "expected_devices": expected_devices,
            "timestamp": timestamp,
            "protocol_status": protocol_status,
        })
        runtime.global_state.usbip_states[device_host] = state
    with runtime.global_state.device_cache_lock:
        runtime.global_state.device_cache = {"devices": [], "timestamp": 0}
    return True, ""
