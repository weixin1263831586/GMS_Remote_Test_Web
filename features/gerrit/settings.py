from __future__ import annotations

from pathlib import Path
from typing import Any

from foundation.config import ConfigManager

from .config import (
    denormalize_gerrit_dashboard_config,
    normalize_gerrit_dashboard_config,
)
from features.redmine.users import owner_runtime_config_path


class GerritConfig:
    """Feature-owned Gerrit dashboard configuration facade."""

    def __init__(self, project_root: Path | str | None = None):
        self.manager = ConfigManager(project_root=project_root)
        self.project_root = self.manager.project_root

    @property
    def runtime_config_path(self) -> Path:
        return Path(self.manager.runtime_config_path)

    @runtime_config_path.setter
    def runtime_config_path(self, value: Path | str) -> None:
        self.manager.runtime_config_path = str(value)

    def for_owner(self, owner_id: str) -> "GerritConfig":
        manager = GerritConfig(self.project_root)
        runtime_path = owner_runtime_config_path(owner_id)
        runtime_path.parent.mkdir(parents=True, exist_ok=True)
        manager.runtime_config_path = runtime_path
        return manager

    def load_config(self, force_reload: bool = False) -> dict[str, Any]:
        return self.manager.load_config(force_reload=force_reload)

    def get_gerrit_dashboard_config(self) -> dict[str, Any]:
        return normalize_gerrit_dashboard_config(
            self.load_config().get("gerrit_dashboard") or {}
        )

    def save_gerrit_dashboard_config(
        self,
        payload: dict[str, Any],
    ) -> bool:
        current = self.load_config().get("gerrit_dashboard") or {}
        normalized = denormalize_gerrit_dashboard_config(
            {**current, **(payload or {})}
        )
        return self.manager.save_runtime({"gerrit_dashboard": normalized})

    def get_redmine_dashboard_config(self) -> dict[str, Any]:
        return dict(self.load_config().get("redmine_dashboard") or {})


config_manager = GerritConfig()
