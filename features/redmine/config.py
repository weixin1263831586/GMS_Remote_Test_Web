from __future__ import annotations

import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from foundation.config import ConfigManager, settings
from foundation.config_paths import ensure_owner_config_dir
from foundation.secrets import decrypt_secret, encrypt_secret

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

    def get_runtime_config(self) -> dict[str, Any]:
        """Expose the owner-scoped runtime document to feature services."""
        return self.manager.get_runtime_config()

    def save_runtime(self, runtime: dict[str, Any]) -> bool:
        """Persist the complete owner-scoped runtime document."""
        return bool(self.manager.save_runtime(runtime))

    def for_owner(self, owner_id: str) -> RedmineConfig:
        manager = RedmineConfig(self.project_root)
        runtime_path = ensure_owner_config_dir(owner_runtime_config_path(owner_id))
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
            raise ValueError("Redmine 未配置，请设置 configs/local/config.json 的 redmine.base_url")
        redmine.setdefault("domain", urlparse(redmine["base_url"]).netloc)
        return redmine

    def save_redmine_base_url(self, base_url: str) -> bool:
        """Persist the per-owner Redmine URL used by the statistics client."""
        normalized = str(base_url or "").strip().rstrip("/")
        if normalized:
            parsed = urlparse(normalized)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError("Redmine 地址必须是完整的 http(s) URL")
        return self._save_runtime_section(
            "redmine",
            {
                "base_url": normalized,
                "domain": urlparse(normalized).netloc if normalized else "",
            },
        )

    def load_redmine_credentials(self) -> dict[str, str]:
        runtime = self.manager.get_runtime_config()
        saved = runtime.get("redmine_auth") or {}
        encrypted = saved.get("encrypted_password")
        if encrypted:
            try:
                password = decrypt_secret(str(encrypted))
                return {
                    "username": str(saved.get("username") or ""),
                    "password": password,
                }
            except Exception:
                return {}
        return {}

    def load_redmine_api_key(self) -> str:
        """读取加密保存的 Redmine API Key（不进入任何日志/响应）。"""
        saved = self.manager.get_runtime_config().get("redmine_auth") or {}
        encrypted = saved.get("encrypted_api_key")
        if not encrypted:
            return ""
        try:
            return decrypt_secret(str(encrypted))
        except Exception:
            return ""

    def save_redmine_api_key(self, api_key: str) -> bool:
        """加密保存 Redmine API Key；文件权限 0600，与密码凭据共存。"""
        api_key = str(api_key or "").strip()
        runtime = self.manager.get_runtime_config()
        saved = dict(runtime.get("redmine_auth") or {})
        if api_key:
            saved["encrypted_api_key"] = encrypt_secret(api_key)
        else:
            saved.pop("encrypted_api_key", None)
        saved["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        runtime["redmine_auth"] = saved
        persisted = self.manager.save_runtime(runtime)
        if persisted:
            Path(self.manager.runtime_config_path).chmod(0o600)
        return persisted

    def save_redmine_credentials(
        self,
        username: str,
        password: str,
    ) -> bool:
        """加密保存 Redmine 用户名/密码；已保存的 API Key 保持共存。"""
        runtime = self.manager.get_runtime_config()
        saved = dict(runtime.get("redmine_auth") or {})
        saved["username"] = username
        saved["encrypted_password"] = encrypt_secret(password)
        saved["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        runtime["redmine_auth"] = saved
        persisted = self.manager.save_runtime(runtime)
        if persisted:
            Path(self.manager.runtime_config_path).chmod(0o600)
        return persisted

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
        # save_runtime merges top-level keys, so pass only the changed
        # section: no stale full-document read, and the real persistence
        # result is propagated instead of an unconditional True.
        return bool(self.manager.save_runtime({name: payload}))


config_manager = RedmineConfig(settings.project_root)
