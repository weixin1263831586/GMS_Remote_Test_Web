from __future__ import annotations

from dataclasses import dataclass

from foundation.config import ConfigManager, RuntimeSettings, settings


@dataclass
class AppServices:
    config: ConfigManager
    settings: RuntimeSettings


def build_services(
    *,
    runtime_settings: RuntimeSettings | None = None,
) -> AppServices:
    selected = settings if runtime_settings is None else runtime_settings
    return AppServices(
        config=ConfigManager(project_root=selected.project_root),
        settings=selected,
    )
