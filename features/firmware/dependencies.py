from __future__ import annotations

from typing import Any

from . import runtime


def configure_firmware_dependencies(**values: Any) -> None:
    runtime.configure_runtime(**values)
