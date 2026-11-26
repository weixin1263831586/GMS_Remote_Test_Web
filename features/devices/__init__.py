from importlib import import_module

from .aoa.service import profile_store
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
from .support import (
    DeviceSSHConnection,
    broadcast_device_lock_update,
    device_fencing_owner_id,
    get_or_create_user_state,
    iter_websocket_targets,
    release_device_locks,
    ssh_connection_failed_response,
    update_user_state_field,
)
from .ui_control_api import UiControlRequest, UiTapRequest
from .usbip.manager import parse_adb_device_states, usbip_manager


# 跨 Feature 公开能力；域内实现直接从所属模块导入。
_LAZY_API_EXPORTS = {
    'AoaCertificationStore': '.aoa.certifications',
    'authorize_semantic_options': '.aoa.mapping_service',
    'annotate_cluster_usbip_devices': '.integrations_api',
    'connect_wifi': '.operations.api',
    'create_pair_grant': '.adb.proxy_security',
    'DeviceUtils': '.utils',
    'replay_mapping_options': '.aoa.mapping_service',
    'ensure_usbip_auto_bind_policies': '.usbip.flash',
    'incompatible_test_devices': '.transport.registry',
    'test_transport_requirement': '.transport.registry',
    'pause_usbip_reconnect': '.reconnect',
    'resume_usbip_reconnect': '.reconnect',
    'schedule_usbip_reconnect': '.reconnect',
    'usbip_source_host_for_device': '.reconnect',
    'local_proxy_secret': '.adb.proxy_security',
    'pair_code_for_worker': '.adb.proxy_security',
    'reconcile_cluster_usbip_command': '.integrations_api',
    'reconcile_cluster_usbip_heartbeat': '.integrations_api',
    'resolve_usbip_flash_routes': '.usbip.flash',
    'lookup_usbip_source_os': '.usbip.persistence',
    'record_usbip_source_os': '.usbip.persistence',
    'rockusb_loader_serials': '.rockusb',
    'rockusb_loader_vid_pids': '.rockusb',
    'probe_rockusb_sysfs': '.rockusb',
    'list_attached_usbip_ports': '.usbip.transaction',
    'detach_attached_usbip_port': '.usbip.transaction',
    'parse_usbip_port_entries': '.usbip.transaction',
    'validate_pair_grant': '.adb.proxy_security',
    'validate_owned_semantic': '.aoa.mapping_service',
}


def get_adb_proxy_service():
    """Return the shared service without colliding with its submodule name."""
    from .adb.proxy_service import adb_proxy_service

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
    "AoaCertificationStore",
    "DeviceActionRequest",
    "DeviceLockManager",
    "DeviceLockRequest",
    "DeviceSSHConnection",
    "DeviceShellRequest",
    "DeviceUtils",
    "USBIPDisconnectRequest",
    "USBIPStartRequest",
    "UiControlRequest",
    "UiTapRequest",
    "WifiConnectRequest",
    "annotate_cluster_usbip_devices",
    "authorize_semantic_options",
    "broadcast_device_lock_update",
    "connect_wifi",
    "create_pair_grant",
    "detach_attached_usbip_port",
    "device_fencing_owner_id",
    "device_lock_manager",
    "device_manager",
    "ensure_usbip_auto_bind_policies",
    "get_adb_proxy_service",
    "get_aoa_router",
    "get_or_create_user_state",
    "get_usb_monitor",
    "incompatible_test_devices",
    "iter_websocket_targets",
    "list_aoa_profiles",
    "list_attached_usbip_ports",
    "local_proxy_secret",
    "lookup_usbip_source_os",
    "pair_code_for_worker",
    "parse_adb_device_states",
    "parse_usbip_port_entries",
    "pause_usbip_reconnect",
    "probe_rockusb_sysfs",
    "profile_store",
    "reconcile_cluster_usbip_command",
    "reconcile_cluster_usbip_heartbeat",
    "record_usbip_source_os",
    "release_device_locks",
    "replay_mapping_options",
    "resolve_usbip_flash_routes",
    "resume_usbip_reconnect",
    "rockusb_loader_serials",
    "rockusb_loader_vid_pids",
    "schedule_usbip_reconnect",
    "ssh_connection_failed_response",
    "test_transport_requirement",
    "update_user_state_field",
    "usbip_manager",
    "usbip_source_host_for_device",
    "validate_owned_semantic",
    "validate_pair_grant",
]


def list_aoa_profiles(request):
    """Owner-scoped AOA catalog for project tool discovery."""
    from .aoa.api import list_profiles

    return list_profiles(request)


def get_aoa_router():
    """Expose the AOA router for application composition and integration tests."""
    from .aoa.api import router

    return router
