from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass
class ReportDependencies:
    ssh_manager: Any = None
    file_utils: Any = None
    universal_analyzer_factory: Callable[[], Any] | None = None
    resolve_suite_target: Callable[..., Any] | None = None
    make_empty_suite_target: Callable[..., Any] | None = None


dependencies = ReportDependencies()


def configure_report_dependencies(**values: Any) -> None:
    for name, value in values.items():
        if not hasattr(dependencies, name):
            raise ValueError(f"Unknown report dependency: {name}")
        setattr(dependencies, name, value)
