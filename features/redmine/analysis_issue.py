"""RedmineAgent: nightly Redmine triage and report generation."""

from __future__ import annotations

import logging
from typing import Any

from features.redmine.analysis_common import _truncate
from features.redmine.utils import to_iso8601


logger = logging.getLogger(__name__)


def _iso(value: Any) -> str:
    # Delegate to the shared normalizer so all mixins format Redmine
    # timestamps identically (handles datetime + space-separated strings).
    return to_iso8601(value)


def _obj_name(value: Any) -> str:
    if value is None:
        return ""
    return str(getattr(value, "name", "") or value)


def _obj_email(value: Any) -> str:
    if value is None:
        return ""
    return str(
        getattr(value, "mail", "")
        or getattr(value, "email", "")
        or getattr(value, "login", "")
        or ""
    )


class IssueAnalysisMixin:
    def _issue_payload(self, issue: Any, run_id: str, journals: list[dict[str, Any]], attachment_records: list[dict[str, Any]], failures: list[dict[str, Any]]) -> dict[str, Any]:
        fixed_version = _obj_name(getattr(issue, "fixed_version", None))
        return {
            "issue_id": int(issue.id),
            "run_id": run_id,
            "subject": str(issue.subject or ""),
            "status_name": _obj_name(getattr(issue, "status", None)),
            "priority_name": _obj_name(getattr(issue, "priority", None)),
            "project_name": _obj_name(getattr(issue, "project", None)),
            "tracker_name": _obj_name(getattr(issue, "tracker", None)),
            "author_name": _obj_name(getattr(issue, "author", None)),
            "assigned_to_name": _obj_name(getattr(issue, "assigned_to", None)),
            "created_on": _iso(issue.created_on),
            "updated_on": _iso(issue.updated_on),
            "description": str(issue.description or ""),
            "fixed_version": fixed_version,
            "component": self._extract_custom_field(issue, "Component_fae"),
            "soc_platform": self._parse_soc_platform(
                str(issue.subject or ""),
                str(issue.description or ""),
                fixed_version,
            ),
            "android_version": self._parse_android_version(
                str(issue.subject or ""),
                str(issue.description or ""),
                fixed_version,
            ),
            "start_date": _iso(issue.start_date),
            "due_date": _iso(issue.due_date),
            "closed_on": _iso(issue.closed_on),
            "done_ratio": int(getattr(issue, "done_ratio", 0) or 0),
            "journals_json": journals,
            "attachments_json": attachment_records,
            "failures_json": failures[:50],
        }

    def _extract_journals(self, journals: list[Any]) -> list[dict[str, Any]]:
        result = []
        for item in journals:
            details = []
            for detail in getattr(item, "details", []):
                details.append({
                    "property": str(getattr(detail, "property", "")),
                    "name": str(getattr(detail, "name", "")),
                    "old_value": str(getattr(detail, "old_value", "")),
                    "new_value": str(getattr(detail, "new_value", "")),
                })
            result.append({
                "id": str(getattr(item, "id", "")),
                "user": _obj_name(getattr(item, "user", None)),
                "user_email": _obj_email(getattr(item, "user", None)),
                "created_on": _iso(getattr(item, "created_on", "")),
                "notes": _truncate(str(getattr(item, "notes", "")), 2000),
                "details": details,
            })
        return result
