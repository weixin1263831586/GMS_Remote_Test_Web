from __future__ import annotations

from pathlib import Path
from typing import Any

from foundation.config import ConfigManager, settings
from foundation.secrets import decrypt_secret, encrypt_secret

from .config import (
    denormalize_gerrit_dashboard_config,
    normalize_gerrit_dashboard_config,
)


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

    def for_owner(self, owner_id: str) -> GerritConfig:
        manager = GerritConfig(self.project_root)
        safe_owner = "".join(
            ch if ch.isalnum() or ch in ("-", "_") else "_"
            for ch in str(owner_id or "").strip()
        )
        if not safe_owner:
            raise ValueError("owner_id is required")
        runtime_path = (
            settings.data_root
            / "gerrit/by_user"
            / safe_owner
            / "config_runtime.json"
        )
        runtime_path.parent.mkdir(parents=True, exist_ok=True)
        manager.runtime_config_path = runtime_path
        return manager

    def load_config(self, force_reload: bool = False) -> dict[str, Any]:
        return self.manager.load_config(force_reload=force_reload)

    def get_gerrit_dashboard_config(self) -> dict[str, Any]:
        raw = dict(self.load_config().get("gerrit_dashboard") or {})
        encrypted = str(raw.pop("rest_password_encrypted", "") or "")
        # 不信任配置中的明文密码，仅加载加密值。
        raw["rest_password"] = decrypt_secret(encrypted) if encrypted else ""
        return normalize_gerrit_dashboard_config(raw)

    def save_gerrit_dashboard_config(
        self,
        payload: dict[str, Any],
    ) -> bool:
        current = self.load_config().get("gerrit_dashboard") or {}
        normalized = denormalize_gerrit_dashboard_config(
            {**current, **(payload or {})}
        )
        password = str(normalized.pop("rest_password", "") or "")
        if password:
            normalized["rest_password_encrypted"] = encrypt_secret(password)
        saved = self.manager.save_runtime({"gerrit_dashboard": normalized})
        if saved:
            self.runtime_config_path.chmod(0o600)
        return saved

    def get_redmine_dashboard_config(self) -> dict[str, Any]:
        return dict(self.load_config().get("redmine_dashboard") or {})


config_manager = GerritConfig()
