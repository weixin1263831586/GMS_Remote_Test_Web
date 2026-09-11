from importlib import import_module

from .locks import DeviceLockManager, device_lock_manager
from .manager import device_manager
from .models import (
    ADBForwardStartRequest,
    ADBForwardStopRequest,
    ADBProxyPairCodeRequest,
    DeviceActionRequest,
    DeviceLockRequest,
    DeviceShellRequest,
    USBIPDisconnectRequest,
    USBIPStartRequest,
    WifiConnectRequest,
)
from .monitor import get_usb_monitor
from .service import DeviceService
from .support import (
    DeviceSSHConnection,
    broadcast_device_lock_update,
    get_or_create_user_state,
    iter_websocket_targets,
    release_device_locks,
    ssh_connection_failed_response,
    update_user_state_field,
)
from .ui_control_api import UiControlRequest, UiTapRequest
from .usbip import parse_adb_device_states, usbip_manager


# 模块内部实现函数（_ 前缀）不再通过 package facade 暴露（ADR 0002：
# features 域内私有边界）——
# 它们的使用方只有 management_api 自身与其单测（经 from .management_api
# import 直连）。跨 Feature 需要的能力走下方显式公开符号。
_LAZY_API_EXPORTS = {
    'annotate_cluster_usbip_devices': '.integrations_api',
    'connect_wifi': '.operations_api',
    'create_pair_grant': '.adb_proxy_security',
    'DeviceUtils': '.utils',
    'bind_usbip_busid_via_ssh': '.usbip_flash',
    'ensure_usbip_auto_bind_policies': '.usbip_flash',
    'incompatible_test_devices': '.transport_policy',
    'local_proxy_secret': '.adb_proxy_security',
    'pair_code_for_worker': '.adb_proxy_security',
    'reconcile_cluster_usbip_command': '.integrations_api',
    'reconcile_cluster_usbip_heartbeat': '.integrations_api',
    'resolve_usbip_flash_routes': '.usbip_flash',
    'open_usbip_source_ssh': '.usbip_flash',
    'migrate_local_usbip_serial': '.usbip_persistence',
    'lookup_usbip_source_os': '.usbip_persistence',
    'record_usbip_source_os': '.usbip_persistence',
    'rockusb_loader_serials': '.rockusb',
    'rockusb_loader_vid_pids': '.rockusb',
    'ROCKUSB_SYSFS_PROBE_COMMAND': '.rockusb',
    'usbipd_list_via_ssh': '.usbip_flash',
    'usbipd_policy_list_via_ssh': '.usbip_flash',
    'query_usbipd_busid_instance_ids': '.usbip_identity',
    'query_usbipd_device_states': '.usbip_identity',
    'USBIP_PORT_COMMAND': '.usbip_transaction',
    'parse_usbip_port_entries': '.usbip_transaction',
    'validate_pair_grant': '.adb_proxy_security',
}


def get_adb_proxy_service():
    """Return the shared service without colliding with its submodule name."""
    from .adb_proxy_service import adb_proxy_service

    return adb_proxy_service


def __getattr__(name: str):
    if name not in _LAZY_API_EXPORTS:
        raise AttributeError(f'module {__name__!r} has no attribute {name!r}')
    module = import_module(_LAZY_API_EXPORTS[name], __name__)
    value = getattr(module, name)
    globals()[name] = value
    return value


__all__ = [
    "ROCKUSB_SYSFS_PROBE_COMMAND",
    "USBIP_PORT_COMMAND",
    "ADBForwardStartRequest",
    "ADBForwardStopRequest",
    "ADBProxyPairCodeRequest",
    "DeviceActionRequest",
    "DeviceLockManager",
    "DeviceLockRequest",
    "DeviceSSHConnection",
    "DeviceService",
    "DeviceShellRequest",
    "DeviceUtils",
    "USBIPDisconnectRequest",
    "USBIPStartRequest",
    "UiControlRequest",
    "UiTapRequest",
    "WifiConnectRequest",
    "annotate_cluster_usbip_devices",
    "bind_usbip_busid_via_ssh",
    "broadcast_device_lock_update",
    "connect_wifi",
    "create_pair_grant",
    "device_lock_manager",
    "device_manager",
    "ensure_usbip_auto_bind_policies",
    "get_adb_proxy_service",
    "get_or_create_user_state",
    "get_usb_monitor",
    "incompatible_test_devices",
    "iter_websocket_targets",
    "local_proxy_secret",
    "lookup_usbip_source_os",
    "migrate_local_usbip_serial",
    "open_usbip_source_ssh",
    "pair_code_for_worker",
    "parse_adb_device_states",
    "parse_usbip_port_entries",
    "query_usbipd_busid_instance_ids",
    "query_usbipd_device_states",
    "reconcile_cluster_usbip_command",
    "reconcile_cluster_usbip_heartbeat",
    "record_usbip_source_os",
    "release_device_locks",
    "resolve_usbip_flash_routes",
    "rockusb_loader_serials",
    "rockusb_loader_vid_pids",
    "ssh_connection_failed_response",
    "update_user_state_field",
    "usbip_manager",
    "usbipd_list_via_ssh",
    "usbipd_policy_list_via_ssh",
    "validate_pair_grant",
]
