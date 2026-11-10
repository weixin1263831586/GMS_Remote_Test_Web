"""SMTP 配置解析：全局默认 + 兼容兜底 + owner 覆盖，与密码读取。

``resolve_email_config`` 的合并规则是单一事实来源：automation/ops/redmine
等链路不传 manager 时自动回落全局 ``smtp`` 块，owner 传入 manager 时按
非空字段覆盖（ADR 记录见 test_email_config_resolution.py 的合并矩阵）。
"""

from __future__ import annotations

import logging
from typing import Any


logger = logging.getLogger(__name__)


def _merge_non_empty(target: dict[str, Any], source: dict[str, Any]) -> None:
    """非空字段才参与合并：空串/缺省的覆盖层不遮挡下层配置。"""
    for key, value in (source or {}).items():
        if isinstance(value, str) and not value.strip():
            continue
        if value is None:
            continue
        target[key] = value


def resolve_email_config(manager: Any = None) -> dict[str, Any]:
    """解析生效的 SMTP 配置，无需每个 owner 重复配置账号。

    合并顺序（低 → 高，均为非空字段覆盖）：

    1. 全局配置中的兼容段 ``redmine_dashboard.email``；
    2. 全局默认 ``smtp`` 块；
    3. owner 运行时显式保存的 ``redmine_dashboard.email``。

    owner 的静态默认值不属于覆盖层。全局关闭邮件时，owner 不能重新开启。
    """
    from foundation.config import config_manager

    merged: dict[str, Any] = {}
    try:
        global_config = config_manager.load_config()
    except Exception:
        global_config = {}
    _merge_non_empty(merged, (global_config.get("redmine_dashboard") or {}).get("email") or {})
    _merge_non_empty(merged, global_config.get("smtp") or {})
    globally_disabled = merged.get("enabled") is False
    if manager is not None:
        try:
            owner_config = manager.get_runtime_config() or {}
        except Exception:
            owner_config = {}
        _merge_non_empty(
            merged, (owner_config.get("redmine_dashboard") or {}).get("email") or {}
        )
    if globally_disabled:
        merged["enabled"] = False
    return merged


def load_email_config(manager) -> dict[str, Any]:
    """从 redmine_dashboard.email 读取 SMTP 配置（兼容旧调用）。"""
    return resolve_email_config(manager)


def email_password(email_cfg: dict[str, Any]) -> str:
    """SMTP 密码优先取密文解密；遗留明文仅作旧部署兼容读取。"""
    encrypted = str(email_cfg.get("encrypted_password") or "").strip()
    if encrypted:
        try:
            from foundation.secrets import decrypt_secret

            return decrypt_secret(encrypted).strip()
        except Exception:
            return ""
    legacy = str(email_cfg.get("password") or "").strip()
    if legacy:
        # 迁移告警：新写入已走平台密钥加密（email/api.py），明文分支只为
        # 旧部署兼容。设定迁移截止点：所有部署改写为 encrypted_password
        # 后移除本分支（migration mapping: scripts/sanitize_tracked_config.py）。
        logger.warning(
            "SMTP 密码仍以明文配置读取（遗留部署兼容）；请重新保存邮箱配置"
            "以迁移为加密存储，明文兼容分支将在后续版本移除"
        )
    return legacy
