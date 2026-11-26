"""Typed and validated firmware-feature runtime bindings."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict


from typing_extensions import Unpack

from foundation.config import ConfigManager
from foundation.runtime_contracts import (
    ApiHelpProvider,
    ClientIdResolver,
    ControllerStateContract,
    DeviceLockRelease,
    FirmwareDeviceLocker,
    NotificationStore,
    SSHManagerContract,
    WebSocketSender,
)


@dataclass
class FirmwareRuntime:
    config_manager: ConfigManager | None = None
    ssh_manager: SSHManagerContract | None = None
    global_state: ControllerStateContract | None = None
    safe_websocket_send: WebSocketSender | None = None
    store_notification: NotificationStore | None = None
    generate_help_or_continue: ApiHelpProvider | None = None
    get_client_id_from_request: ClientIdResolver | None = None
    project_root: Path | None = None
    firmware_transfer_timeout: int = 3600
    lock_firmware_devices: FirmwareDeviceLocker | None = None
    release_firmware_devices: DeviceLockRelease | None = None
    firmware_share_store: str | Path | None = None


class FirmwareRuntimeBindings(TypedDict, total=False):
    config_manager: ConfigManager | None
    ssh_manager: SSHManagerContract | None
    global_state: ControllerStateContract | None
    safe_websocket_send: WebSocketSender | None
    store_notification: NotificationStore | None
    generate_help_or_continue: ApiHelpProvider | None
    get_client_id_from_request: ClientIdResolver | None
    project_root: str | Path | None
    firmware_transfer_timeout: int
    lock_firmware_devices: FirmwareDeviceLocker | None
    release_firmware_devices: DeviceLockRelease | None
    firmware_share_store: str | Path | None


_runtime = FirmwareRuntime()
_RUNTIME_FIELDS = frozenset(FirmwareRuntime.__dataclass_fields__)


def get_runtime() -> FirmwareRuntime:
    return _runtime


def __getattr__(name: str) -> object:
    if name in _RUNTIME_FIELDS:
        return getattr(_runtime, name)
    raise AttributeError(name)


def configure_runtime(**values: Unpack[FirmwareRuntimeBindings]) -> None:
    invalid = set(values) - _RUNTIME_FIELDS
    if invalid:
        raise TypeError(f"unknown firmware runtime bindings: {sorted(invalid)}")
    for name in _RUNTIME_FIELDS:
        globals().pop(name, None)
    for name, value in values.items():
        if name == "project_root" and value is not None:
            value = Path(value)
        setattr(_runtime, name, value)
