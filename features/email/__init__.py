"""通用邮件发送 feature。

提供 ``POST /api/email/send``，SMTP 凭证复用 ``redmine_dashboard.email``。
"""
from __future__ import annotations

from . import api
from .api import configure_manager_provider, router
from .service import send_email


__all__ = ["api", "configure_manager_provider", "router", "send_email"]
