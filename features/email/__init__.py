"""通用邮件发送 feature。

提供 ``POST /api/email/send``，SMTP 凭证复用 ``redmine_dashboard.email``。
"""
from __future__ import annotations

from . import api
from .api import router

__all__ = ["router", "api"]
