from typing import Any

from . import runtime
from .sessions import client_manager


def configure_user_dependencies(**values: Any) -> None:
    runtime.configure_runtime(**values)
    client_manager.config_manager = values["config_manager"]
