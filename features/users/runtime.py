"""Typed and validated user-feature runtime bindings."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path


@dataclass
class UserRuntime:
    config_manager: object | None = None
    data_root: Path | None = None
    global_state: object | None = None
    ssh_async_manager: object | None = None
    get_or_create_user_state: Callable[..., object] | None = None


_runtime = UserRuntime()
_RUNTIME_FIELDS = frozenset(UserRuntime.__dataclass_fields__)


def get_runtime() -> UserRuntime:
    return _runtime


def __getattr__(name: str) -> object:
    if name in _RUNTIME_FIELDS:
        return getattr(_runtime, name)
    raise AttributeError(name)


def configure_runtime(**values: object) -> None:
    invalid = set(values) - _RUNTIME_FIELDS
    if invalid:
        raise TypeError(f"unknown user runtime bindings: {sorted(invalid)}")
    for name in _RUNTIME_FIELDS:
        globals().pop(name, None)
    for name, value in values.items():
        if name == "data_root" and value is not None:
            value = Path(value)
        setattr(_runtime, name, value)


def configure_user_dependencies(**values: object) -> None:
    """Bootstrap 装配入口:运行时绑定之外,同步注入 config/sessions 的配置管理器。"""
    from . import config_api
    from .sessions import client_manager

    configure_runtime(**values)
    client_manager.config_manager = values["config_manager"]
    config_api.config_manager = values["config_manager"]
