"""Report ownership checks shared by list, analysis, download, and delete APIs."""

from __future__ import annotations

from typing import Any

from features.auth import CurrentUser, get_authenticated_user


def report_request_user(request: Any) -> CurrentUser | None:
    if request is None:
        return None
    try:
        return get_authenticated_user(request)
    except (AttributeError, TypeError):
        return None


def report_owner_id(report: dict[str, Any]) -> str:
    """Return the immutable platform account id stored on a report."""

    return str(report.get("owner_id") or "").strip()


def can_access_report(request: Any, report: dict[str, Any] | None) -> bool:
    if not report:
        return False
    user = report_request_user(request)
    if not user:
        return False
    if user.role == "admin":
        return True
    owner_id = report_owner_id(report)
    return bool(owner_id and owner_id == user.id)


def filter_accessible_reports(
    request: Any,
    reports: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    return [report for report in reports if can_access_report(request, report)]


def get_accessible_report_by_timestamp(
    repository: Any,
    request: Any,
    timestamp: str,
) -> dict[str, Any] | None:
    """Resolve a timestamp inside the caller's owner partition.

    Timestamps are display metadata rather than globally unique resource IDs.
    Scoping the query itself prevents a same-timestamp record owned by another
    user from shadowing the caller's report.
    """
    user = report_request_user(request)
    if not user:
        return None
    owner_id = None if user.role == "admin" else user.id
    report = repository.get_report_by_timestamp(
        timestamp,
        owner_id=owner_id,
        include_all=user.role == "admin",
    )
    return report if can_access_report(request, report) else None
