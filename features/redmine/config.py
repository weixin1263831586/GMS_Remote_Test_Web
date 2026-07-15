from __future__ import annotations

import base64
import hashlib
import json
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from foundation.config import ConfigManager, settings

from .dashboard import (
    denormalize_redmine_dashboard_config,
    normalize_redmine_dashboard_profiles,
    normalize_redmine_stats_config,
)
from .users import owner_runtime_config_path


class RedmineConfig:
    """Feature-owned configuration facade backed by runtime config files."""

    def __init__(
        self,
        project_root: Path | None = None,
        *,
        base_dir: str | None = None,
    ):
        if base_dir is not None:
            self.manager = ConfigManager(base_dir=base_dir)
        else:
            self.manager = ConfigManager(project_root=project_root)
        self.project_root = self.manager.project_root

    @property
    def config_path(self) -> Path:
        return self.manager.config_path

    @config_path.setter
    def config_path(self, value: Path) -> None:
        self.manager.config_path = Path(value)

    @property
    def runtime_config_path(self) -> Path:
        return self.manager.runtime_config_path

    @runtime_config_path.setter
    def runtime_config_path(self, value: Path) -> None:
        self.manager.runtime_config_path = Path(value)

    def invalidate_cache(self) -> None:
        self.manager.invalidate_cache()

    def for_owner(self, owner_id: str) -> RedmineConfig:
        manager = RedmineConfig(self.project_root)
        runtime_path = owner_runtime_config_path(owner_id)
        runtime_path.parent.mkdir(parents=True, exist_ok=True)
        manager.runtime_config_path = runtime_path
        return manager

    def load_config(self, force_reload: bool = False) -> dict[str, Any]:
        return self.manager.load_config(force_reload=force_reload)

    def get_redmine_base_url(
        self,
        config: dict[str, Any] | None = None,
    ) -> str:
        config = config or self.load_config()
        redmine = config.get("redmine") or {}
        return str(redmine.get("base_url") or "").strip().rstrip("/")

    def get_redmine_config(self) -> dict[str, Any]:
        config = self.load_config()
        redmine = dict(config.get("redmine") or {})
        redmine["base_url"] = self.get_redmine_base_url(config)
        if not redmine["base_url"]:
            raise ValueError("Redmine 未配置，请设置 configs/config.json 的 redmine.base_url")
        redmine.setdefault("domain", urlparse(redmine["base_url"]).netloc)
        return redmine

    def load_redmine_credentials(self) -> dict[str, str]:
        runtime = self.manager.get_runtime_config()
        saved = runtime.get("redmine_auth") or {}
        if not saved:
            saved = (self.manager.load_config(force_reload=True).get("redmine_auth") or {})
        encrypted = saved.get("encrypted_password")
        if encrypted:
            try:
                from cryptography.fernet import Fernet

                key = base64.urlsafe_b64encode(
                    hashlib.sha256(b"gms_remote_test_redmine_2024").digest()
                )
                password = Fernet(key).decrypt(
                    str(encrypted).encode()
                ).decode()
                return {
                    "username": str(saved.get("username") or ""),
                    "password": password,
                }
            except Exception:
                return {}
        return {
            "username": str(saved.get("username") or ""),
            "password": str(saved.get("password") or ""),
        }

    def save_redmine_credentials(
        self,
        username: str,
        password: str,
    ) -> bool:
        from cryptography.fernet import Fernet

        key = base64.urlsafe_b64encode(
            hashlib.sha256(b"gms_remote_test_redmine_2024").digest()
        )
        runtime = self.manager.get_runtime_config()
        runtime["redmine_auth"] = {
            "username": username,
            "encrypted_password": Fernet(key).encrypt(
                password.encode()
            ).decode(),
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        return self.manager.save_runtime(runtime)

    def get_redmine_stats_config(self) -> dict[str, Any]:
        return normalize_redmine_stats_config(
            self.load_config().get("redmine_stats") or {}
        )

    def save_redmine_stats_config(self, payload: dict[str, Any]) -> bool:
        return self._save_runtime_section(
            "redmine_stats",
            normalize_redmine_stats_config(payload),
        )

    def get_redmine_dashboard_config(self) -> dict[str, Any]:
        return normalize_redmine_dashboard_profiles(
            self.load_config().get("redmine_dashboard") or {}
        )

    def save_redmine_dashboard_config(
        self,
        payload: dict[str, Any],
    ) -> bool:
        return self._save_runtime_section(
            "redmine_dashboard",
            denormalize_redmine_dashboard_config(payload),
        )

    def get_gerrit_dashboard_config(self) -> dict[str, Any]:
        return dict(self.load_config().get("gerrit_dashboard") or {})

    def save_gerrit_dashboard_config(
        self,
        payload: dict[str, Any],
    ) -> bool:
        normalized = dict(payload)
        if normalized.get("base_url"):
            normalized["base_url"] = str(normalized["base_url"]).rstrip("/")
        return self._save_runtime_section("gerrit_dashboard", normalized)

    def _save_runtime_section(
        self,
        name: str,
        payload: dict[str, Any],
    ) -> bool:
        runtime_path = Path(self.manager.runtime_config_path)
        try:
            runtime = json.loads(runtime_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            runtime = {}
        if not isinstance(runtime, dict):
            runtime = {}
        runtime[name] = payload
        self.manager.save_runtime(runtime)
        return True


config_manager = RedmineConfig(settings.project_root)
