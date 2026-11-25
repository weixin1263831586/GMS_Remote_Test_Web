"""Typed and validated firmware-feature runtime bindings."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path


RuntimeCallable = Callable[..., object]


@dataclass
class FirmwareRuntime:
    config_manager: object | None = None
    ssh_manager: object | None = None
    global_state: object | None = None
    safe_websocket_send: RuntimeCallable | None = None
    store_notification: RuntimeCallable | None = None
    generate_help_or_continue: RuntimeCallable | None = None
    get_client_id_from_request: RuntimeCallable | None = None
    project_root: Path | None = None
    firmware_transfer_timeout: int = 3600
    gsi_progress_increment: int = 1
    gsi_progress_max: int = 95
    gsi_progress_poll_interval: float = 1
    lock_firmware_devices: RuntimeCallable | None = None
    release_firmware_devices: RuntimeCallable | None = None
    firmware_share_store: object | None = None


_runtime = FirmwareRuntime()
_RUNTIME_FIELDS = frozenset(FirmwareRuntime.__dataclass_fields__)


def get_runtime() -> FirmwareRuntime:
    return _runtime


def __getattr__(name: str) -> object:
    if name in _RUNTIME_FIELDS:
        return getattr(_runtime, name)
    raise AttributeError(name)


def configure_runtime(**values: object) -> None:
    invalid = set(values) - _RUNTIME_FIELDS
    if invalid:
        raise TypeError(f"unknown firmware runtime bindings: {sorted(invalid)}")
    for name in _RUNTIME_FIELDS:
        globals().pop(name, None)
    for name, value in values.items():
        if name == "project_root" and value is not None:
            value = Path(value)
        setattr(_runtime, name, value)
