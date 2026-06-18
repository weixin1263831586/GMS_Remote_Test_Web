from __future__ import annotations

from typing import Any


config_manager: Any = None
global_state: Any = None
ssh_manager: Any = None
store_notification: Any = None
generate_help_or_continue: Any = None
get_client_id_from_request: Any = None
probe_windows_usbipd: Any = None
resolve_tailscale_device_host: Any = None
safe_websocket_send: Any = None
get_client_ip: Any = None
client_manager: Any = None
run_local_shell_command: Any = None
project_root: Any = None
device_cache_ttl: float = 5


def configure_runtime(
    *,
    selected_config_manager: Any,
    selected_global_state: Any,
    selected_ssh_manager: Any,
    selected_store_notification: Any,
    selected_generate_help_or_continue: Any,
    selected_get_client_id_from_request: Any,
    selected_probe_windows_usbipd: Any,
    selected_resolve_tailscale_device_host: Any,
    selected_safe_websocket_send: Any = None,
    selected_get_client_ip: Any = None,
    selected_client_manager: Any = None,
    selected_run_local_shell_command: Any = None,
    selected_project_root: Any = None,
    selected_device_cache_ttl: float = 5,
) -> None:
    global config_manager, global_state, ssh_manager, store_notification
    global generate_help_or_continue, get_client_id_from_request
    global probe_windows_usbipd, resolve_tailscale_device_host
    global safe_websocket_send, get_client_ip, client_manager
    global run_local_shell_command, project_root, device_cache_ttl
    config_manager = selected_config_manager
    global_state = selected_global_state
    ssh_manager = selected_ssh_manager
    store_notification = selected_store_notification
    generate_help_or_continue = selected_generate_help_or_continue
    get_client_id_from_request = selected_get_client_id_from_request
    probe_windows_usbipd = selected_probe_windows_usbipd
    resolve_tailscale_device_host = selected_resolve_tailscale_device_host
    safe_websocket_send = selected_safe_websocket_send
    get_client_ip = selected_get_client_ip
    client_manager = selected_client_manager
    run_local_shell_command = selected_run_local_shell_command
    project_root = selected_project_root
    device_cache_ttl = selected_device_cache_ttl
