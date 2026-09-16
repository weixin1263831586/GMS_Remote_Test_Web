"""Owner eligibility rules for Daily Brief execution."""

from __future__ import annotations

import logging

from features.auth import auth_service


logger = logging.getLogger(__name__)
ADMIN_OWNER_MESSAGE = "管理员账号不能运行每日晨报；请使用普通网页用户账号。"


def is_daily_brief_owner_eligible(owner_id: str) -> bool:
    """Allow ordinary owners, but never dispatch analysis for platform admins.

    Unauthenticated development owners have no platform-user record and remain
    eligible. If the account lookup fails, fail closed so a degraded auth store
    cannot accidentally dispatch an administrator's queued analysis.
    """
    try:
        user = auth_service.get_enabled_user(str(owner_id or "").strip())
    except Exception:
        logger.exception("unable to verify Daily Brief owner eligibility")
        return False
    return user is None or user.role != "admin"


__all__ = ["ADMIN_OWNER_MESSAGE", "is_daily_brief_owner_eligible"]
