"""Read-only validation of an existing local USB/IP transport."""

import logging
from typing import Any

from . import runtime
from .usbip import usbip_manager
from .usbip_transaction import USBIP_PORT_COMMAND, parse_usbip_port_entries


logger = logging.getLogger(__name__)


def _normalize_host(host: str) -> str:
    host = str(host or "").strip()
    return host.split("@", 1)[-1] if "@" in host else host


def _local_transport_target(
    device_host: str,
    expected_devices: set[str],
    local_worker_id: str,
) -> tuple[set[str], set[str]]:
    target = _normalize_host(device_host)
    if not target or not local_worker_id:
        return set(), set()

    getter = getattr(runtime.config_manager, "get_runtime_config", None)
    runtime_config = getter() if callable(getter) else {}
    assignments = (runtime_config or {}).get("usbip_cluster_assignments") or {}
    if not isinstance(assignments, dict):
        return set(), set()

    matching: list[dict[str, Any]] = []
    for info in assignments.values():
        if not isinstance(info, dict):
            continue
        if str(info.get("worker_id") or "") != local_worker_id:
            continue
        if info.get("status") not in {
            "attaching", "attached", "unknown", "cleanup_required",
        }:
            continue
        source_hosts = {
            _normalize_host(info.get("device_host")),
            _normalize_host(info.get("source_host")),
        }
        if target in source_hosts:
            matching.append(info)

    if expected_devices:
        expected_matches = [
            info for info in matching
            if expected_devices.intersection({
                str(serial or "").strip()
                for serial in info.get("device_serials") or []
                if str(serial or "").strip()
            })
        ]
        if expected_matches:
            matching = expected_matches

    busids = {
        str(info.get("busid") or "").strip()
        for info in matching
        if str(info.get("busid") or "").strip()
    }
    serials = {
        str(serial or "").strip()
        for info in matching
        for serial in info.get("device_serials") or []
        if str(serial or "").strip()
    }
    return busids, serials


def _scope_protocol_status(
    protocol_status: dict[str, Any],
    expected_devices: set[str],
) -> dict[str, Any]:
    scoped = dict(protocol_status or {})
    if expected_devices:
        adb_states = scoped.get("adb") or {}
        scoped["adb"] = {
            serial: state
            for serial, state in adb_states.items()
            if serial in expected_devices
        }
        for key in (
            "adb_ready", "recovery", "sideload", "unauthorized", "offline", "fastboot",
        ):
            scoped[key] = [
                serial for serial in scoped.get(key) or []
                if serial in expected_devices
            ]

    if scoped.get("fastboot"):
        scoped["mode"] = "fastboot"
    elif scoped.get("recovery") or scoped.get("sideload"):
        scoped["mode"] = "recovery"
    elif scoped.get("adb_ready"):
        scoped["mode"] = "adb"
    elif scoped.get("unauthorized"):
        scoped["mode"] = "unauthorized"
    elif scoped.get("offline"):
        scoped["mode"] = "offline"
    elif scoped.get("adb"):
        scoped["mode"] = "adb_non_device"
    else:
        scoped["mode"] = "unknown"
    return scoped


def probe_existing_local_usbip_transport(
    device_host: str,
    expected_devices: set[str],
    config: dict[str, Any],
    *,
    local_worker_id: str,
) -> dict[str, Any] | None:
    """Return state only when exact source host/BUSID pairs remain attached."""
    busids, assignment_serials = _local_transport_target(
        device_host,
        expected_devices,
        local_worker_id,
    )
    if not busids:
        return None

    ssh = runtime.ssh_manager.get_connection(config)
    if not ssh:
        return None
    try:
        stdout, _stderr, code = runtime.ssh_manager.execute_command(
            ssh,
            USBIP_PORT_COMMAND,
            timeout=10,
        )
        if code != 0:
            return None
        source_host = _normalize_host(device_host)
        attached = {
            (str(entry.get("host") or ""), str(entry.get("busid") or ""))
            for entry in parse_usbip_port_entries(stdout or "")
        }
        if not all((source_host, busid) in attached for busid in busids):
            return None

        protocol_status = _scope_protocol_status(
            usbip_manager.probe_protocol_status(ssh),
            expected_devices or assignment_serials,
        )
        return {
            "success": True,
            "transport_connected": True,
            "transport_state": "attached",
            "device_list": list(protocol_status.get("adb_ready") or []),
            "protocol_status": protocol_status,
        }
    except Exception as exc:
        logger.debug(
            "[USB/IP Reconnect] existing transport probe failed for %s: %s",
            device_host,
            exc,
        )
        return None
    finally:
        runtime.ssh_manager.return_connection(ssh)
