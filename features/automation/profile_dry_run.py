"""Pure profile matching preview used by AutomationService."""

from __future__ import annotations

from typing import Any

from .gerrit_trigger import normalize_gerrit_event, profile_matches_event


def dry_run_profile(
    service: Any,
    profile_id: str,
    request: dict[str, Any],
    *,
    not_found_error: type[Exception],
) -> dict[str, Any]:
    profile = next(
        (
            item
            for item in service.list_profiles()
            if item.get("id") == profile_id
        ),
        None,
    )
    if profile is None:
        raise not_found_error("Automation profile not found")
    event = normalize_gerrit_event(
        {
            "type": "dry-run",
            "change": {
                "project": request.get("project", ""),
                "branch": request.get("branch", ""),
                "number": request.get("change_id")
                or request.get("number")
                or "",
                "subject": request.get("subject", ""),
                "owner": {"email": request.get("owner", "")},
            },
            "patchSet": {
                "number": request.get("patchset", ""),
                "revision": request.get("revision", ""),
            },
        }
    )
    matched = profile_matches_event(profile, event)
    run_request = (
        service._run_request_from_gerrit_event(event, profile).model_dump()
        if matched
        else {}
    )
    return {
        "matched": matched,
        "event": event,
        "profile": profile,
        "run_request": run_request,
    }
