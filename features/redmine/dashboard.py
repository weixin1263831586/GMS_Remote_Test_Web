"""Configuration helpers for Redmine dashboard profiles and aggregates."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime
from typing import Any


DEFAULT_REDMINE_STATS = {
    "stale_days": 3,
    "window_days": 60,
    "cache_ttl": 600,
    "chart_date_ranges": {},
}

DEFAULT_DEPARTMENT_DASHBOARD = {
    "list_limit": 50,
    "issue_limit": 500,
}

DEFAULT_REDMINE_BASE_URL = "https://redmine.rock-chips.com"


def _bounded_int(value, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _profile_id(value: Any, fallback: str = "") -> str:
    text = str(value or fallback).strip().lower()
    return "".join(
        char if char.isalnum() or char in "-_" else "-"
        for char in text
    ).strip("-")


def normalize_redmine_stats_config(raw: dict[str, Any] | None) -> dict[str, int]:
    """Normalize runtime-editable Redmine statistics settings."""
    raw = raw or {}
    chart_date_ranges = {}
    for key, value in (raw.get("chart_date_ranges") or {}).items():
        if not isinstance(value, dict):
            continue
        start = _date_text(value.get("start"))
        end = _date_text(value.get("end"))
        if start and end and start > end:
            start, end = end, start
        if start or end:
            chart_date_ranges[str(key or "").strip()] = {
                **({"start": start} if start else {}),
                **({"end": end} if end else {}),
            }
    return {
        "stale_days": _bounded_int(raw.get("stale_days"), DEFAULT_REDMINE_STATS["stale_days"], 1, 30),
        "window_days": _bounded_int(raw.get("window_days"), DEFAULT_REDMINE_STATS["window_days"], 0, 365),
        "cache_ttl": _bounded_int(raw.get("cache_ttl"), DEFAULT_REDMINE_STATS["cache_ttl"], 0, 3600),
        "chart_date_ranges": chart_date_ranges,
    }


def normalize_redmine_dashboard_profiles(raw: dict[str, Any] | None) -> dict[str, Any]:
    """Normalize configurable department dashboard profiles.

    Config shape:
      redmine_dashboard:
        department_defaults: {list_limit, issue_limit}
        dashboard_profiles:
          - {id, name, user_ids, stale_days, window_days, email_to}
    """
    raw = raw or {}
    defaults_raw = raw.get("department_defaults") or {}
    defaults = {
        "list_limit": _bounded_int(defaults_raw.get("list_limit"), DEFAULT_DEPARTMENT_DASHBOARD["list_limit"], 1, 500),
        "issue_limit": _bounded_int(defaults_raw.get("issue_limit"), DEFAULT_DEPARTMENT_DASHBOARD["issue_limit"], 1, 2000),
    }

    profiles = []
    for idx, item in enumerate(raw.get("dashboard_profiles") or []):
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or f"部门看板 {idx + 1}").strip()
        profile_id = _profile_id(item.get("id") or name or f"profile-{idx + 1}")
        user_ids = [str(user_id).strip() for user_id in (item.get("user_ids") or []) if str(user_id).strip()]
        aliases = [str(alias).strip() for alias in (item.get("aliases") or []) if str(alias).strip()]
        profile = {
            "id": profile_id,
            "name": name,
            "user_ids": list(dict.fromkeys(user_ids)),
            "aliases": list(dict.fromkeys(aliases)),
            "stale_days": _bounded_int(item.get("stale_days"), DEFAULT_REDMINE_STATS["stale_days"], 1, 30),
            "window_days": _bounded_int(item.get("window_days"), DEFAULT_REDMINE_STATS["window_days"], 0, 365),
            "list_limit": _bounded_int(item.get("list_limit"), defaults["list_limit"], 1, 500),
            "issue_limit": _bounded_int(item.get("issue_limit"), defaults["issue_limit"], 1, 2000),
            "email_to": str(item.get("email_to") or "").strip(),
        }
        profiles.append(profile)

    if not profiles:
        profiles.append({
            "id": "all",
            "name": "全部部门",
            "user_ids": [],
            "aliases": [],
            "stale_days": DEFAULT_REDMINE_STATS["stale_days"],
            "window_days": DEFAULT_REDMINE_STATS["window_days"],
            "list_limit": defaults["list_limit"],
            "issue_limit": defaults["issue_limit"],
            "email_to": "",
        })

    project_profiles = []
    for idx, item in enumerate(raw.get("project_profiles") or []):
        if not isinstance(item, dict):
            continue
        project_id = _project_id(item.get("project_id") or item.get("id") or "")
        if not project_id:
            continue
        name = str(item.get("name") or project_id).strip()
        project_profiles.append({
            "id": _profile_id(item.get("id") or project_id),
            "name": name or f"项目看板 {idx + 1}",
            "project_id": project_id,
            "issue_limit": _bounded_int(item.get("issue_limit"), defaults["issue_limit"], 1, 5000),
            "list_limit": _bounded_int(item.get("list_limit"), defaults["list_limit"], 1, 500),
        })

    return {
        "defaults": defaults,
        "profiles": profiles,
        "project_profiles": project_profiles,
        "email": dict(raw.get("email") or {}),
    }


def select_redmine_dashboard_profile(config: dict[str, Any], profile_id: str = "") -> dict[str, Any]:
    """Return the selected profile or the first configured profile."""
    profiles = (config or {}).get("profiles") or []
    if not profiles:
        return normalize_redmine_dashboard_profiles({})["profiles"][0]
    requested = str(profile_id or "").strip()
    for profile in profiles:
        if profile.get("id") == requested:
            return profile
    return profiles[0]


def with_department_profiles_from_users(
    config: dict[str, Any],
    users: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    """Return dashboard config with department profiles derived from user_map.

    The Redmine department dashboard is an organization-level view. Older
    deployments often have only ``configs/redmine_user_map.json`` populated and
    no runtime ``redmine_dashboard`` section. In that case the UI can still
    select department ids such as ``system-2`` if we expose profiles derived
    from the user map.
    """
    normalized = normalize_redmine_dashboard_profiles(config or {})
    profiles = list(normalized.get("profiles") or [])
    existing = {str(item.get("id") or "").strip() for item in profiles}
    defaults = normalized.get("defaults") or DEFAULT_DEPARTMENT_DASHBOARD
    departments: dict[str, str] = {}
    for user in users or []:
        department_id = str(user.get("department_id") or "").strip()
        department_name = str(user.get("department") or department_id).strip()
        if not department_id:
            continue
        departments.setdefault(department_id, department_name or department_id)
    for department_id, department_name in sorted(departments.items(), key=lambda item: item[1]):
        profile_id = _profile_id(department_id)
        if not profile_id or profile_id in existing:
            continue
        profiles.append({
            "id": profile_id,
            "name": department_name,
            "user_ids": [],
            "aliases": [],
            "stale_days": DEFAULT_REDMINE_STATS["stale_days"],
            "window_days": DEFAULT_REDMINE_STATS["window_days"],
            "list_limit": defaults["list_limit"],
            "issue_limit": defaults["issue_limit"],
            "email_to": "",
        })
        existing.add(profile_id)
    return {**normalized, "profiles": profiles}


def filter_users_for_profile(users: list[dict[str, Any]], profile: dict[str, Any]) -> list[dict[str, Any]]:
    """Filter Redmine user map entries according to a dashboard profile."""
    user_ids = {str(user_id) for user_id in (profile or {}).get("user_ids") or []}
    aliases = {str(alias).strip() for alias in (profile or {}).get("aliases") or [] if str(alias).strip()}
    profile_id = str((profile or {}).get("id") or "").strip()
    profile_name = str((profile or {}).get("name") or "").strip()
    if not user_ids and not aliases:
        if profile_id == "all":
            return list(users)
        department_users = [
            user for user in users
            if str(user.get("department_id") or "").strip() == profile_id
            or str(user.get("department") or "").strip() == profile_name
        ]
        return department_users
    selected = []
    for user in users:
        candidates = {str(user.get("id") or ""), str(user.get("name") or "")}
        candidates.update(str(alias) for alias in (user.get("aliases") or []))
        if candidates & user_ids or candidates & aliases:
            selected.append(user)
    return selected


def add_department_profile(config: dict[str, Any], name: str, profile_id: str = "") -> dict[str, Any]:
    """Return config with a new empty department profile appended."""
    normalized = normalize_redmine_dashboard_profiles(_to_raw_dashboard_config(config))
    clean_name = str(name or "").strip()
    if not clean_name:
        raise ValueError("department name is required")
    existing_ids = {str(item.get("id") or "") for item in normalized["profiles"]}
    desired_id = _profile_id(profile_id or clean_name)
    if desired_id == "profile" and not profile_id:
        index = len(existing_ids) + 1
        desired_id = f"dept-{index}"
        while desired_id in existing_ids:
            index += 1
            desired_id = f"dept-{index}"
    if desired_id in existing_ids:
        raise ValueError(f"department profile {desired_id} already exists")
    defaults = normalized["defaults"]
    normalized["profiles"].append({
        "id": desired_id,
        "name": clean_name,
        "user_ids": [],
        "aliases": [],
        "stale_days": DEFAULT_REDMINE_STATS["stale_days"],
        "window_days": DEFAULT_REDMINE_STATS["window_days"],
        "list_limit": defaults["list_limit"],
        "issue_limit": defaults["issue_limit"],
        "email_to": "",
    })
    return normalized


def assign_user_to_profiles(config: dict[str, Any], user_id: Any, profile_ids: Iterable[Any]) -> dict[str, Any]:
    """Return config with user_id added to selected department profiles."""
    normalized = normalize_redmine_dashboard_profiles(_to_raw_dashboard_config(config))
    clean_user_id = str(user_id or "").strip()
    selected_ids = {str(profile_id or "").strip() for profile_id in profile_ids or [] if str(profile_id or "").strip()}
    if not clean_user_id:
        raise ValueError("user id is required")
    for profile in normalized["profiles"]:
        if profile.get("id") not in selected_ids:
            continue
        users = [str(uid).strip() for uid in profile.get("user_ids") or [] if str(uid).strip()]
        users.append(clean_user_id)
        profile["user_ids"] = list(dict.fromkeys(users))
    return normalized


def add_project_profile(config: dict[str, Any], name: str, project_id: str, profile_id: str = "") -> dict[str, Any]:
    """Return config with a new Redmine project dashboard profile appended."""
    normalized = normalize_redmine_dashboard_profiles(_to_raw_dashboard_config(config))
    project_id = _project_id(project_id)
    clean_name = str(name or project_id).strip()
    if not project_id:
        raise ValueError("project id is required")
    if not clean_name:
        raise ValueError("project name is required")
    existing_ids = {str(item.get("id") or "") for item in normalized["project_profiles"]}
    existing_projects = {str(item.get("project_id") or "") for item in normalized["project_profiles"]}
    desired_id = _profile_id(profile_id or project_id)
    if desired_id in existing_ids or project_id in existing_projects:
        raise ValueError(f"project profile {project_id} already exists")
    defaults = normalized["defaults"]
    normalized["project_profiles"].append({
        "id": desired_id,
        "name": clean_name,
        "project_id": project_id,
        "issue_limit": defaults["issue_limit"],
        "list_limit": 15,
    })
    return normalized


def merge_resolved_trends(items: Iterable[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Merge per-user daily/weekly/monthly/yearly Redmine resolved trends."""
    specs = (
        ("resolved_daily", "date", 90),
        ("resolved_weekly", "week", 52),
        ("resolved_monthly", "month", 24),
        ("resolved_yearly", "year", 10),
    )
    merged: dict[str, list[dict[str, Any]]] = {}
    for trend_key, label_key, limit in specs:
        buckets: dict[str, int] = {}
        for item in items:
            for row in item.get(trend_key) or []:
                label = str(row.get(label_key) or "").strip()
                if not label:
                    continue
                buckets[label] = buckets.get(label, 0) + int(row.get("count") or 0)
        merged[trend_key] = [
            {label_key: label, "count": count}
            for label, count in sorted(buckets.items())[-limit:]
        ]
    return merged


def issue_id_list(issues: Iterable[dict[str, Any]]) -> list[str]:
    """Return stable Redmine issue id strings for copy/email actions."""
    ids = [str(item.get("issue_id") or "").strip() for item in issues or []]
    return [issue_id for issue_id in ids if issue_id]


def issue_url_list(issues: Iterable[dict[str, Any]], base_url: str) -> list[str]:
    """Return stable Redmine issue URLs for copy/email actions."""
    root = str(base_url or "").strip().rstrip("/") or DEFAULT_REDMINE_BASE_URL
    return [f"{root}/issues/{issue_id}" for issue_id in issue_id_list(issues)]


def issue_url_text(issues: Iterable[dict[str, Any]], base_url: str) -> str:
    """Return newline-separated Redmine issue URLs."""
    return "\n".join(issue_url_list(issues, base_url))


def denormalize_redmine_dashboard_config(config: dict[str, Any]) -> dict[str, Any]:
    """Convert normalized dashboard config back to config.json/runtime shape."""
    normalized = normalize_redmine_dashboard_profiles(_to_raw_dashboard_config(config))
    result = {
        "department_defaults": normalized["defaults"],
        "dashboard_profiles": normalized["profiles"],
        "project_profiles": normalized["project_profiles"],
    }
    if isinstance(config, dict) and isinstance(config.get("email"), dict):
        result["email"] = config["email"]
    elif normalized.get("email"):
        result["email"] = normalized["email"]
    return result


def _to_raw_dashboard_config(config: dict[str, Any]) -> dict[str, Any]:
    if "profiles" not in (config or {}):
        return config or {}
    return {
        "department_defaults": (config or {}).get("defaults") or {},
        "dashboard_profiles": (config or {}).get("profiles") or [],
        "project_profiles": (config or {}).get("project_profiles") or [],
        "email": (config or {}).get("email") or {},
    }


def _project_id(value: Any) -> str:
    return str(value or "").strip().strip("/")


def _date_text(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        date.fromisoformat(text)
    except ValueError:
        return ""
    return text


def summarize_project_issues(issues: Iterable[Any], list_limit: int = 15) -> dict[str, Any]:
    """Aggregate live Redmine project issues by assignee."""
    list_limit = max(1, min(int(list_limit or 15), 500))
    assignees: dict[str, dict[str, Any]] = {}
    issue_count = 0
    open_count = 0
    closed_count = 0
    open_issues: list[dict[str, Any]] = []

    for issue in issues or []:
        issue_count += 1
        status_name = _attr_name(issue, "status")
        is_closed = _is_closed_status(status_name) or bool(getattr(issue, "closed_on", None))
        if is_closed:
            closed_count += 1
        else:
            open_count += 1

        assigned = getattr(issue, "assigned_to", None)
        assignee_id = str(getattr(assigned, "id", "") or "unassigned")
        assignee_name = str(getattr(assigned, "name", "") or "未指派")
        row = assignees.setdefault(assignee_id, {
            "id": assignee_id,
            "name": assignee_name,
            "total_owned": 0,
            "open_count": 0,
            "closed_count": 0,
            "issues": [],
        })
        row["total_owned"] += 1
        if is_closed:
            row["closed_count"] += 1
        else:
            row["open_count"] += 1
            summary = _project_issue_summary(issue, status_name)
            row["issues"].append(summary)
            open_issues.append(summary)

    users = sorted(
        assignees.values(),
        key=lambda item: str(item.get("name") or "").casefold(),
    )
    for row in users:
        row["issues"] = sorted(row["issues"], key=lambda item: item.get("updated_on") or "", reverse=True)[:list_limit]

    return {
        "summary": {
            "issue_count": issue_count,
            "assignee_count": len(users),
            "open_count": open_count,
            "closed_count": closed_count,
        },
        "assignees": users,
        "open_issues": sorted(open_issues, key=lambda item: item.get("updated_on") or "", reverse=True)[:list_limit],
    }


def _attr_name(issue: Any, attr: str) -> str:
    value = getattr(issue, attr, None)
    return str(getattr(value, "name", "") or value or "")


def _is_closed_status(status_name: str) -> bool:
    return str(status_name or "").strip() in {"已关闭", "已解决", "Closed", "Resolved"}


def _project_issue_summary(issue: Any, status_name: str) -> dict[str, Any]:
    return {
        "issue_id": int(getattr(issue, "id", 0) or 0),
        "subject": str(getattr(issue, "subject", "") or ""),
        "status_name": status_name,
        "priority_name": _attr_name(issue, "priority"),
        "assigned_to_name": _attr_name(issue, "assigned_to") or "未指派",
        "updated_on": _iso_text(getattr(issue, "updated_on", "")),
        "created_on": _iso_text(getattr(issue, "created_on", "")),
        "closed_on": _iso_text(getattr(issue, "closed_on", "")),
    }


def _iso_text(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    return str(value or "")
