from __future__ import annotations

from . import runtime


def configure_firmware_dependencies(**values: object) -> None:
    runtime.configure_runtime(**values)
