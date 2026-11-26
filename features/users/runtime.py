"""Typed and validated user-feature runtime bindings."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict

from typing_extensions import Unpack

from foundation.runtime_contracts import ControllerStateContract, SSHAsyncManagerContract, UserStateProvider


if TYPE_CHECKING:
    from foundation.config import ConfigManager


@dataclass
class UserRuntime:
    config_manager: ConfigManager | None = None
    data_root: Path | None = None
    global_state: ControllerStateContract | None = None
    ssh_async_manager: SSHAsyncManagerContract | None = None
    get_or_create_user_state: UserStateProvider | None = None


class UserRuntimeBindings(TypedDict, total=False):
    config_manager: ConfigManager | None
    data_root: str | Path | None
    global_state: ControllerStateContract | None
    ssh_async_manager: SSHAsyncManagerContract | None
    get_or_create_user_state: UserStateProvider | None


_runtime = UserRuntime()
_RUNTIME_FIELDS = frozenset(UserRuntime.__dataclass_fields__)


def get_runtime() -> UserRuntime:
    return _runtime


def __getattr__(name: str) -> object:
    if name in _RUNTIME_FIELDS:
        return getattr(_runtime, name)
    raise AttributeError(name)


def configure_runtime(**values: Unpack[UserRuntimeBindings]) -> None:
    invalid = set(values) - _RUNTIME_FIELDS
    if invalid:
        raise TypeError(f"unknown user runtime bindings: {sorted(invalid)}")
    for name in _RUNTIME_FIELDS:
        globals().pop(name, None)
    for name, value in values.items():
        if name == "data_root" and value is not None:
            value = Path(value)
        setattr(_runtime, name, value)


def configure_user_dependencies(
    *,
    config_manager: ConfigManager,
    data_root: Path,
    global_state: ControllerStateContract,
    ssh_async_manager: SSHAsyncManagerContract,
    get_or_create_user_state: UserStateProvider,
) -> None:
    """Wire user runtime and configuration consumers."""
    from . import config_api
    from .sessions import client_manager

    configure_runtime(
        config_manager=config_manager,
        data_root=data_root,
        global_state=global_state,
        ssh_async_manager=ssh_async_manager,
        get_or_create_user_state=get_or_create_user_state,
    )
    client_manager.config_manager = config_manager
    config_api.config_manager = config_manager
