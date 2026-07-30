from .clients import (
    format_client_display_id,
    get_client_display_id_from_request,
    get_client_id_from_request,
    get_client_ip,
    get_client_source,
    get_client_username_from_request,
    hide_sensitive_info,
    is_manual_username_fallback_error,
    normalize_client_display_id,
    owner_id_from_request,
    parse_client_id,
    probe_windows_usbipd,
    resolve_client_display_id,
    resolve_tailscale_device_host,
)
from .device_groups import (
    auto_assign_new_devices,
    build_device_group_map,
    cluster_device_properties,
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
from .workspace_context import load_workspace_context, save_workspace_context


__all__ = [
    "ClientInfoRequest",
    "auto_assign_new_devices",
    "build_device_group_map",
    "client_manager",
    "cluster_device_properties",
    "current_username_for_request",
    "enforce_exclusive_device_group",
    "format_client_display_id",
    "get_client_display_id_from_request",
    "get_client_id_from_request",
    "get_client_ip",
    "get_client_source",
    "get_client_username_from_request",
    "hide_sensitive_info",
    "is_manual_username_fallback_error",
    "list_users",
    "load_device_groups",
    "load_workspace_context",
    "normalize_client_display_id",
    "normalize_device_groups",
    "owner_id_from_request",
    "parse_client_id",
    "probe_windows_usbipd",
    "resolve_client_display_id",
    "resolve_tailscale_device_host",
    "save_device_groups",
    "save_workspace_context",
    "soc_series",
]
