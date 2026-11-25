"""Typed device-feature runtime bindings.

The bindings remain process-local because they reference live connection pools
and locks, but they are configured as one validated object instead of mutating
an open-ended set of module globals.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path


RuntimeCallable = Callable[..., object]


@dataclass
class DeviceRuntime:
    config_manager: object | None = None
    global_state: object | None = None
    ssh_manager: object | None = None
    store_notification: RuntimeCallable | None = None
    generate_help_or_continue: RuntimeCallable | None = None
    get_client_id_from_request: RuntimeCallable | None = None
    probe_windows_usbipd: RuntimeCallable | None = None
    resolve_tailscale_device_host: RuntimeCallable | None = None
    safe_websocket_send: RuntimeCallable | None = None
    get_client_ip: RuntimeCallable | None = None
    client_manager: object | None = None
    run_local_shell_command: RuntimeCallable | None = None
    project_root: Path | None = None
    device_cache_ttl: float = 15


_runtime = DeviceRuntime()
_RUNTIME_FIELDS = frozenset(DeviceRuntime.__dataclass_fields__)


def get_runtime() -> DeviceRuntime:
    return _runtime


def __getattr__(name: str) -> object:
    if name in _RUNTIME_FIELDS:
        return getattr(_runtime, name)
    raise AttributeError(name)


def configure_runtime(
    *,
    selected_config_manager: object,
    selected_global_state: object,
    selected_ssh_manager: object,
    selected_store_notification: RuntimeCallable,
    selected_generate_help_or_continue: RuntimeCallable,
    selected_get_client_id_from_request: RuntimeCallable,
    selected_probe_windows_usbipd: RuntimeCallable,
    selected_resolve_tailscale_device_host: RuntimeCallable,
    selected_safe_websocket_send: RuntimeCallable | None = None,
    selected_get_client_ip: RuntimeCallable | None = None,
    selected_client_manager: object | None = None,
    selected_run_local_shell_command: RuntimeCallable | None = None,
    selected_project_root: Path | None = None,
    selected_device_cache_ttl: float = 15,
) -> None:
    for name in _RUNTIME_FIELDS:
        globals().pop(name, None)
    _runtime.config_manager = selected_config_manager
    _runtime.global_state = selected_global_state
    _runtime.ssh_manager = selected_ssh_manager
    _runtime.store_notification = selected_store_notification
    _runtime.generate_help_or_continue = selected_generate_help_or_continue
    _runtime.get_client_id_from_request = selected_get_client_id_from_request
    _runtime.probe_windows_usbipd = selected_probe_windows_usbipd
    _runtime.resolve_tailscale_device_host = selected_resolve_tailscale_device_host
    _runtime.safe_websocket_send = selected_safe_websocket_send
    _runtime.get_client_ip = selected_get_client_ip
    _runtime.client_manager = selected_client_manager
    _runtime.run_local_shell_command = selected_run_local_shell_command
    _runtime.project_root = selected_project_root
    _runtime.device_cache_ttl = float(selected_device_cache_ttl)


def configure_device_dependencies(
    *,
    ssh_manager: object,
    config_manager: object,
    global_state: object,
    store_notification: Callable[..., object],
    generate_help_or_continue: Callable[..., object],
    get_client_id_from_request: Callable[..., object],
    probe_windows_usbipd: Callable[..., object],
    resolve_tailscale_device_host: Callable[..., object],
    safe_websocket_send: Callable[..., object],
    get_client_ip: Callable[..., object],
    client_manager: object,
    run_local_shell_command: Callable[..., object],
    project_root: Path,
    device_cache_ttl: float,
) -> None:
    """Wire device managers and runtime bindings from the application bootstrap."""
    from .adb.forwarding import adb_forward_manager
    from .adb.proxy_service import adb_proxy_service
    from .manager import device_manager
    from .usbip.manager import usbip_manager

    usbip_manager.ssh_manager = ssh_manager
    usbip_manager.config_manager = config_manager
    adb_forward_manager.ssh_manager = ssh_manager
    adb_forward_manager.config_manager = config_manager
    adb_proxy_service.config_manager = config_manager
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
