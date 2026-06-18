from __future__ import annotations

from typing import Any

from .adb_forward import adb_forward_manager
from .manager import device_manager
from .runtime import configure_runtime
from .usbip import usbip_manager


def configure_device_dependencies(
    *,
    ssh_manager: Any,
    config_manager: Any,
    global_state: Any,
    store_notification: Any,
    generate_help_or_continue: Any,
    get_client_id_from_request: Any,
    probe_windows_usbipd: Any,
    resolve_tailscale_device_host: Any,
    safe_websocket_send: Any,
    get_client_ip: Any,
    client_manager: Any,
    run_local_shell_command: Any,
    project_root: Any,
    device_cache_ttl: float,
) -> None:
    usbip_manager.ssh_manager = ssh_manager
    usbip_manager.config_manager = config_manager
    adb_forward_manager.ssh_manager = ssh_manager
    adb_forward_manager.config_manager = config_manager
    device_manager.ssh_manager = ssh_manager
    device_manager.config_manager = config_manager
    configure_runtime(
        selected_config_manager=config_manager,
        selected_global_state=global_state,
        selected_ssh_manager=ssh_manager,
        selected_store_notification=store_notification,
        selected_generate_help_or_continue=generate_help_or_continue,
        selected_get_client_id_from_request=get_client_id_from_request,
        selected_probe_windows_usbipd=probe_windows_usbipd,
        selected_resolve_tailscale_device_host=resolve_tailscale_device_host,
        selected_safe_websocket_send=safe_websocket_send,
        selected_get_client_ip=get_client_ip,
        selected_client_manager=client_manager,
        selected_run_local_shell_command=run_local_shell_command,
        selected_project_root=project_root,
        selected_device_cache_ttl=device_cache_ttl,
    )
