from __future__ import annotations

from typing import Any


config_manager: Any = None
data_root: Any = None
global_state: Any = None
ssh_async_manager: Any = None
get_or_create_user_state: Any = None


def configure_runtime(**values: Any) -> None:
    globals().update(values)
