"""共享的看板配置工具函数。

Gerrit / Redmine 两套看板配置模块（gerrit_dashboard_config.py、
redmine_dashboard_config.py）原本各自维护了一份逐字相同的辅助函数，
这里把它们集中到一处，避免后续两边漂移。
"""

from __future__ import annotations

import re
from typing import Any


# 用于把任意字符串压成合法的 profile/project id（仅保留字母数字、下划线、连字符）。
PROFILE_ID_RE = re.compile(r"[^a-zA-Z0-9_-]+")


def bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    """把 value 解析为 int 并钳制到 [minimum, maximum]，解析失败时回落 default。"""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def profile_id(value: Any) -> str:
    """把名称转换为合法的 profile/project id；空值回落 'profile'。"""
    normalized = PROFILE_ID_RE.sub("-", str(value or "").strip()).strip("-").lower()
    return normalized or "profile"
