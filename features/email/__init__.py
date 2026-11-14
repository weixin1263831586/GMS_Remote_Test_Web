"""通用邮件发送 feature。

提供 ``POST /api/email/send``，SMTP 凭证复用 ``redmine_dashboard.email``。
"""
from __future__ import annotations

from . import api
from .api import (
    configure_attachment_resolver,
    configure_manager_provider,
)
from .service import normalize_email_addresses, send_email


__all__ = [
    "api",
    "configure_attachment_resolver",
    "configure_manager_provider",
    "normalize_email_addresses",
    "send_email",
]
