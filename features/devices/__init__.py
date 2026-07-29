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
from .usbip import parse_adb_device_states


_LAZY_API_EXPORTS = {
    '_build_devices_management_payload',
    '_build_management_props_command',
    '_parse_management_device_props',
    'connect_wifi',
}


def __getattr__(name: str):
    if name not in _LAZY_API_EXPORTS:
        raise AttributeError(f'module {__name__!r} has no attribute {name!r}')
    if name == 'connect_wifi':
        from .operations_api import connect_wifi as value
    else:
        from . import management_api

        value = getattr(management_api, name)
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
    "USBIPDisconnectRequest",
    "USBIPStartRequest",
    "UiControlRequest",
    "UiTapRequest",
    "WifiConnectRequest",
    "_build_devices_management_payload",
    "_build_management_props_command",
    "_parse_management_device_props",
    "broadcast_device_lock_update",
    "connect_wifi",
    "device_lock_manager",
    "device_manager",
    "get_or_create_user_state",
    "get_usb_monitor",
    "parse_adb_device_states",
    "release_device_locks",
    "ssh_connection_failed_response",
    "update_user_state_field",
]
