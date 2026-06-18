from __future__ import annotations

from collections.abc import Callable


_redmine_users_provider: Callable[[], list[dict]] | None = None


def configure_redmine_users_provider(
    provider: Callable[[], list[dict]],
) -> None:
    global _redmine_users_provider
    _redmine_users_provider = provider


def list_redmine_users() -> list[dict]:
    if _redmine_users_provider is None:
        return []
    return list(_redmine_users_provider() or [])
