"""Gerrit event normalization and automation profile matching."""

from __future__ import annotations

import re
from typing import Any, Dict, List


def _first_text(*values: Any) -> str:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def normalize_gerrit_event(payload: Dict[str, Any]) -> Dict[str, Any]:
    payload = payload or {}
    change = payload.get("change") if isinstance(payload.get("change"), dict) else {}
    patchset = payload.get("patchSet") or payload.get("patchset") or {}
    if not isinstance(patchset, dict):
        patchset = {}
    owner = change.get("owner") if isinstance(change.get("owner"), dict) else {}

    project = _first_text(change.get("project"), payload.get("project"))
    branch = _first_text(change.get("branch"), payload.get("branch"))
    change_id = _first_text(change.get("number"), change.get("_number"), change.get("id"), payload.get("change_id"))
    patchset_number = _first_text(patchset.get("number"), patchset.get("patchset"), payload.get("patchset"))
    event = {
        "source_type": "gerrit_webhook",
        "event_type": _first_text(payload.get("type"), payload.get("event_type")),
        "project": project,
        "branch": branch,
        "change_id": change_id,
        "patchset": patchset_number,
        "subject": _first_text(change.get("subject"), payload.get("subject")),
        "owner": _first_text(owner.get("email"), owner.get("username"), payload.get("owner")),
        "revision": _first_text(patchset.get("revision"), payload.get("revision")),
    }
    event["source_key"] = f"gerrit:{project}:{change_id}:{patchset_number}"
    return event


def _regex_matches(pattern: str, value: str) -> bool:
    if not pattern:
        return True
    try:
        return re.search(pattern, value or "") is not None
    except re.error:
        return False


def profile_matches_event(profile: Dict[str, Any], event: Dict[str, Any]) -> bool:
    if not profile.get("enabled", True):
        return False
    gerrit = profile.get("gerrit") if isinstance(profile.get("gerrit"), dict) else {}
    return (
        _regex_matches(str(gerrit.get("project_regex") or ""), event.get("project", ""))
        and _regex_matches(str(gerrit.get("branch_regex") or ""), event.get("branch", ""))
        and _regex_matches(str(gerrit.get("owner_regex") or ""), event.get("owner", ""))
        and _regex_matches(str(gerrit.get("subject_regex") or ""), event.get("subject", ""))
    )


def match_profiles(event: Dict[str, Any], profiles: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [profile for profile in profiles or [] if profile_matches_event(profile, event)]
