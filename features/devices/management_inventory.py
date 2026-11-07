"""Read the Controller Worker's recent inventory without synchronous device probes."""

from __future__ import annotations

from datetime import datetime, timezone

from foundation.cluster_port import get_cluster_service
from foundation.networking import is_local_host

from . import runtime


def local_management_inventory() -> tuple[list[str], dict, dict, str] | None:
    """None means unavailable or empty; callers fall back to live probes.

    非空且新鲜的心跳清单是权威快照（跳过本机探针）；空清单不可当
    权威——Worker 侧 adb 抖动或清单尚未上报会误报“无设备”，此时
    返回 None 让调用方回退本机实时探针。
    """
    try:
        cluster = get_cluster_service()
        worker_id = cluster.config.local_worker_id
        worker = cluster.repository.get_worker(worker_id)
        if not worker or worker.get('status') not in {'online', 'busy', 'draining'}:
            return None
        observed_at = worker.get('last_heartbeat_at') or ''
        observed = datetime.fromisoformat(observed_at.replace('Z', '+00:00'))
        age = (datetime.now(timezone.utc) - observed).total_seconds()
        if not 0 <= age <= cluster.offline_seconds:
            return None
        inventory = cluster.repository.list_devices(worker_id)
    except (RuntimeError, AttributeError, TypeError, ValueError):
        return None

    devices, properties, protocols = [], {}, {}
    for item in inventory:
        serial = str(item.get('serial') or '')
        if (not serial or item.get('worker_id') != worker_id
                or item.get('state') in {'offline', 'unknown', 'unauthorized'}):
            continue
        devices.append(serial)
        props = item.get('properties') or {}
        properties[serial] = {key: props.get(key) or '' for key in (
            'model', 'android_version', 'battery_level', 'soc_model',
        )}
        properties[serial]['serial_no'] = serial
        if props.get('aoa_context'):
            properties[serial]['aoa_context'] = props['aoa_context']
        state = str(item.get('state') or '')
        protocols[serial] = state if state in {'fastboot', 'usb_only'} else 'adb'
    if not devices:
        return None
    return devices, properties, protocols, observed_at


def local_usb_only_inventory() -> dict[str, dict]:
    """Use the owning Worker's USB-only facts, never another execution host's USB.

    配置读取走 devices runtime 注入的 ``config_manager``，按能力降级：
    ``get_ubuntu_host``（生产 ConfigManager，含兜底）→ ``load_config``
    → ``get_runtime_config``。任何配置异常都按 "非 owning Worker" 处理
    返回空：本函数位于设备列表主路径（api.get_connected_devices），
    不得让辅助 inventory 拖垮整条接口。
    """
    config_manager = runtime.get_runtime().config_manager
    if config_manager is None:
        return {}
    try:
        host_reader = getattr(config_manager, "get_ubuntu_host", None)
        if callable(host_reader):
            ubuntu_host = host_reader()
        elif hasattr(config_manager, "load_config"):
            ubuntu_host = str((config_manager.load_config() or {}).get("ubuntu_host", "") or "")
        else:
            ubuntu_host = str((config_manager.get_runtime_config() or {}).get("ubuntu_host", "") or "")
        if not is_local_host(ubuntu_host):
            return {}
    except Exception:
        return {}
    snapshot = local_management_inventory()
    if snapshot is None:
        return {}
    devices, _properties, protocols, _observed = snapshot
    return {serial: {'device_id': serial, 'status': 'usb_only', 'protocol': 'usb_only',
                     'transport': 'local_usb'}
            for serial in devices if protocols.get(serial) == 'usb_only'}
