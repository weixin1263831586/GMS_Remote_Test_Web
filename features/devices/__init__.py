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
    release_device_locks,
    ssh_connection_failed_response,
    update_user_state_field,
)
from .ui_control_api import UiControlRequest, UiTapRequest
from .usbip import parse_adb_device_states, usbip_manager


_LAZY_API_EXPORTS = {
    '_build_devices_management_payload': '.management_api',
    '_build_management_props_command': '.management_api',
    '_parse_management_device_props': '.management_api',
    'annotate_cluster_usbip_devices': '.integrations_api',
    'connect_wifi': '.operations_api',
    'create_pair_grant': '.adb_proxy_security',
    'DeviceUtils': '.utils',
    'ensure_usbip_auto_bind_policies': '.usbip_flash',
    'incompatible_test_devices': '.transport_policy',
    'local_proxy_secret': '.adb_proxy_security',
    'pair_code_for_worker': '.adb_proxy_security',
    'reconcile_cluster_usbip_command': '.integrations_api',
    'reconcile_cluster_usbip_heartbeat': '.integrations_api',
    'resolve_usbip_flash_routes': '.usbip_flash',
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
    "_build_devices_management_payload",
    "_build_management_props_command",
    "_parse_management_device_props",
    "annotate_cluster_usbip_devices",
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
    "local_proxy_secret",
    "pair_code_for_worker",
    "parse_adb_device_states",
    "reconcile_cluster_usbip_command",
    "reconcile_cluster_usbip_heartbeat",
    "release_device_locks",
    "resolve_usbip_flash_routes",
    "ssh_connection_failed_response",
    "update_user_state_field",
    "usbip_manager",
    "validate_pair_grant",
]
