from __future__ import annotations

from typing import Any

from features.automation import AutomationService


async def poll_automation_from_gerrit(
    service: AutomationService,
    *,
    limit: int = 100,
) -> dict[str, Any]:
    return await service.poll_gerrit_changes(limit=limit)


def handle_automation_gerrit_event(
    service: AutomationService,
    payload: dict[str, Any],
) -> dict[str, Any]:
    return service.handle_gerrit_webhook(payload)
