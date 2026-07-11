from .clients import (
    get_client_display_id_from_request,
    get_client_id_from_request,
    get_client_ip,
    get_client_source,
    get_client_username_from_request,
    hide_sensitive_info,
    is_manual_username_fallback_error,
    owner_id_from_request,
    parse_client_id,
    probe_windows_usbipd,
    resolve_tailscale_device_host,
)
from .device_groups import (
    auto_assign_new_devices,
    build_device_group_map,
    current_username_for_request,
    enforce_exclusive_device_group,
    load_device_groups,
    normalize_device_groups,
    save_device_groups,
    soc_series,
)
from .models import ClientInfoRequest
from .sessions import client_manager
from .users_api import list_users


__all__ = [
    "ClientInfoRequest",
    "auto_assign_new_devices",
    "build_device_group_map",
    "client_manager",
    "current_username_for_request",
    "enforce_exclusive_device_group",
    "get_client_display_id_from_request",
    "get_client_id_from_request",
    "get_client_ip",
    "get_client_source",
    "get_client_username_from_request",
    "hide_sensitive_info",
    "is_manual_username_fallback_error",
    "list_users",
    "load_device_groups",
    "normalize_device_groups",
    "owner_id_from_request",
    "parse_client_id",
    "probe_windows_usbipd",
    "resolve_tailscale_device_host",
    "save_device_groups",
    "soc_series",
]
