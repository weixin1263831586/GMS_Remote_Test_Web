"""Runtime-editable Gerrit dashboard configuration helpers."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime, timezone
from typing import Any

from foundation.dashboard_config import (
    bounded_int as _bounded_int,
)
from foundation.dashboard_config import (
    profile_id as _profile_id,
)


# Gerrit 分页查询每页大小上限（配置默认每页 500，可调高至该值以减少翻页）。
# 历史总量上限 max_history_changes=0 表示无上限（拉取全部历史）。
MAX_QUERY_PAGE_SIZE = 2000

DEFAULT_GERRIT_DASHBOARD = {
    "base_url": "",
    "rest_username": "",
    "rest_password": "",
    "rest_verify_ssl": False,
    "ssh_host": "",
    "ssh_user": "",
    "ssh_port": 29418,
    "ssh_identity_file": "",
    "query_limit": 100,
    "cache_ttl": 600,
    "default_owner": "",
    "chart_date_ranges": {},
    "department_defaults": {
        "list_limit": 50,
        "query_limit": 500,
        "query_page_size": 500,
        "max_history_changes": 0,
    },
    "dashboard_profiles": [
        {"id": "open", "name": "打开的变更", "query": "status:open limit:100"},
        {"id": "merged", "name": "最近合入", "query": "status:merged limit:100"},
    ],
    "personal_profiles": [],
    "department_profiles": [],
}


def normalize_gerrit_dashboard_config(raw: dict[str, Any] | None) -> dict[str, Any]:
    """Normalize Gerrit dashboard settings without requiring server plugins."""
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
    defaults_raw = raw.get("department_defaults") or {}
    defaults = {
        "list_limit": _bounded_int(defaults_raw.get("list_limit"), DEFAULT_GERRIT_DASHBOARD["department_defaults"]["list_limit"], 1, 500),
        "query_limit": _bounded_int(defaults_raw.get("query_limit"), DEFAULT_GERRIT_DASHBOARD["department_defaults"]["query_limit"], 1, 5000),
        "query_page_size": _bounded_int(defaults_raw.get("query_page_size"), DEFAULT_GERRIT_DASHBOARD["department_defaults"]["query_page_size"], 1, MAX_QUERY_PAGE_SIZE),
        "max_history_changes": _bounded_int(defaults_raw.get("max_history_changes"), DEFAULT_GERRIT_DASHBOARD["department_defaults"]["max_history_changes"], 0, 1000000),
    }

    profiles = []
    for idx, item in enumerate(raw.get("dashboard_profiles") or DEFAULT_GERRIT_DASHBOARD["dashboard_profiles"]):
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or f"Gerrit 看板 {idx + 1}").strip()
        query = str(item.get("query") or "status:open limit:100").strip()
        profile_id = _profile_id(item.get("id") or name or f"profile-{idx + 1}")
        profiles.append({"id": profile_id, "name": name, "query": query})
    if not profiles:
        profiles = list(DEFAULT_GERRIT_DASHBOARD["dashboard_profiles"])

    personal_profiles = []
    for idx, item in enumerate(raw.get("personal_profiles") or DEFAULT_GERRIT_DASHBOARD["personal_profiles"]):
        if not isinstance(item, dict):
            continue
        owner = str(item.get("owner") or "").strip()
        name = str(item.get("name") or owner or f"个人看板 {idx + 1}").strip()
        if not owner:
            continue
        personal_profiles.append({
            "id": _profile_id(item.get("id") or owner or name),
            "name": name,
            "owner": owner,
            "department_id": str(item.get("department_id") or "").strip(),
            "department": str(item.get("department") or "").strip(),
            "list_limit": _bounded_int(item.get("list_limit"), defaults["list_limit"], 1, 500),
            "query_limit": _bounded_int(item.get("query_limit"), defaults["query_limit"], 1, 5000),
            "query_page_size": _bounded_int(item.get("query_page_size"), defaults["query_page_size"], 1, MAX_QUERY_PAGE_SIZE),
            "max_history_changes": _bounded_int(item.get("max_history_changes"), defaults["max_history_changes"], 0, 1000000),
        })

    department_profiles = []
    for idx, item in enumerate(raw.get("department_profiles") or DEFAULT_GERRIT_DASHBOARD["department_profiles"]):
        if not isinstance(item, dict):
            continue
        owners = [str(owner or "").strip() for owner in (item.get("owners") or []) if str(owner or "").strip()]
        name = str(item.get("name") or f"部门看板 {idx + 1}").strip()
        department_profiles.append({
            "id": _profile_id(item.get("id") or name or f"department-{idx + 1}"),
            "name": name,
            "owners": list(dict.fromkeys(owners)),
            "list_limit": _bounded_int(item.get("list_limit"), defaults["list_limit"], 1, 500),
            "query_limit": _bounded_int(item.get("query_limit"), defaults["query_limit"], 1, 5000),
            "query_page_size": _bounded_int(item.get("query_page_size"), defaults["query_page_size"], 1, MAX_QUERY_PAGE_SIZE),
            "max_history_changes": _bounded_int(item.get("max_history_changes"), defaults["max_history_changes"], 0, 1000000),
        })
    return {
        "base_url": str(raw.get("base_url") or DEFAULT_GERRIT_DASHBOARD["base_url"]).rstrip("/"),
        "rest_username": str(raw.get("rest_username") or DEFAULT_GERRIT_DASHBOARD["rest_username"]).strip(),
        "rest_password": str(raw.get("rest_password") or DEFAULT_GERRIT_DASHBOARD["rest_password"]).strip(),
        "rest_verify_ssl": bool(raw.get("rest_verify_ssl", DEFAULT_GERRIT_DASHBOARD["rest_verify_ssl"])),
        "ssh_host": str(raw.get("ssh_host") or DEFAULT_GERRIT_DASHBOARD["ssh_host"]).strip(),
        "ssh_user": str(raw.get("ssh_user") or DEFAULT_GERRIT_DASHBOARD["ssh_user"]).strip(),
        "ssh_port": _bounded_int(raw.get("ssh_port"), DEFAULT_GERRIT_DASHBOARD["ssh_port"], 1, 65535),
        "ssh_identity_file": str(raw.get("ssh_identity_file") or DEFAULT_GERRIT_DASHBOARD["ssh_identity_file"]).strip(),
        "query_limit": _bounded_int(raw.get("query_limit"), DEFAULT_GERRIT_DASHBOARD["query_limit"], 1, 500),
        "cache_ttl": _bounded_int(raw.get("cache_ttl"), DEFAULT_GERRIT_DASHBOARD["cache_ttl"], 0, 3600),
        "default_owner": str(raw.get("default_owner") or DEFAULT_GERRIT_DASHBOARD["default_owner"]).strip(),
        "chart_date_ranges": chart_date_ranges,
        "defaults": defaults,
        "dashboard_profiles": profiles,
        "personal_profiles": personal_profiles,
        "department_profiles": department_profiles,
    }


def denormalize_gerrit_dashboard_config(config: dict[str, Any]) -> dict[str, Any]:
    """Convert normalized Gerrit config back to runtime config shape."""
    normalized = normalize_gerrit_dashboard_config(_to_raw_dashboard_config(config))
    return {
        "base_url": normalized["base_url"],
        "rest_username": normalized["rest_username"],
        "rest_password": normalized["rest_password"],
        "rest_verify_ssl": normalized["rest_verify_ssl"],
        "ssh_host": normalized["ssh_host"],
        "ssh_user": normalized["ssh_user"],
        "ssh_port": normalized["ssh_port"],
        "ssh_identity_file": normalized["ssh_identity_file"],
        "query_limit": normalized["query_limit"],
        "cache_ttl": normalized["cache_ttl"],
        "default_owner": normalized["default_owner"],
        "chart_date_ranges": normalized["chart_date_ranges"],
        "department_defaults": normalized["defaults"],
        "dashboard_profiles": normalized["dashboard_profiles"],
        "personal_profiles": normalized["personal_profiles"],
        "department_profiles": normalized["department_profiles"],
    }


def select_gerrit_personal_profile(config: dict[str, Any], profile_id: str = "", owner: str = "") -> dict[str, Any]:
    profiles = (config or {}).get("personal_profiles") or []
    requested = str(profile_id or "").strip()
    for profile in profiles:
        if profile.get("id") == requested:
            return profile
    owner_text = str(owner or "").strip()
    if owner_text:
        return {"id": _profile_id(owner_text), "name": owner_text, "owner": owner_text, **((config or {}).get("defaults") or {})}
    defaults = (config or {}).get("defaults") or {}
    default_owner = str((config or {}).get("default_owner") or "").strip()
    if default_owner:
        return {"id": _profile_id(default_owner), "name": default_owner, "owner": default_owner, **defaults}
    return profiles[0] if profiles else {"id": "", "name": "", "owner": "", **defaults}


def select_gerrit_department_profile(config: dict[str, Any], profile_id: str = "") -> dict[str, Any]:
    profiles = (config or {}).get("department_profiles") or []
    requested = str(profile_id or "").strip()
    for profile in profiles:
        if profile.get("id") == requested:
            return profile
    defaults = (config or {}).get("defaults") or {}
    return profiles[0] if profiles else {"id": "", "name": "", "owners": [], **defaults}


def add_gerrit_personal_profile(
    config: dict[str, Any],
    name: str,
    owner: str,
    profile_id: str = "",
    department_id: str = "",
) -> dict[str, Any]:
    """Return config with a Gerrit personal dashboard profile appended."""
    normalized = normalize_gerrit_dashboard_config(_to_raw_dashboard_config(config))
    clean_owner = str(owner or "").strip()
    clean_name = str(name or clean_owner).strip()
    clean_department_id = str(department_id or "").strip()
    if not clean_owner:
        raise ValueError("owner is required")
    if not clean_name:
        raise ValueError("profile name is required")
    existing_ids = {str(item.get("id") or "") for item in normalized["personal_profiles"]}
    existing_owners = {str(item.get("owner") or "") for item in normalized["personal_profiles"]}
    desired_id = _profile_id(profile_id or clean_owner or clean_name)
    if desired_id in existing_ids or clean_owner in existing_owners:
        raise ValueError(f"Gerrit personal profile {clean_owner} already exists")
    defaults = normalized["defaults"]
    department_name = ""
    if clean_department_id:
        for department in normalized["department_profiles"]:
            if department.get("id") == clean_department_id:
                department_name = str(department.get("name") or "")
                owners = [str(item or "").strip() for item in department.get("owners") or [] if str(item or "").strip()]
                owners.append(clean_owner)
                department["owners"] = list(dict.fromkeys(owners))
                break
        if not department_name:
            raise ValueError(f"Gerrit department profile {clean_department_id} not found")
    normalized["personal_profiles"].append({
        "id": desired_id,
        "name": clean_name,
        "owner": clean_owner,
        "department_id": clean_department_id,
        "department": department_name,
        "list_limit": defaults["list_limit"],
        "query_limit": defaults["query_limit"],
        "query_page_size": defaults["query_page_size"],
        "max_history_changes": defaults["max_history_changes"],
    })
    return normalized


def add_gerrit_department_profile(
    config: dict[str, Any],
    name: str,
    profile_id: str = "",
    owners: Iterable[Any] | None = None,
) -> dict[str, Any]:
    """Return config with a Gerrit department dashboard profile appended."""
    normalized = normalize_gerrit_dashboard_config(_to_raw_dashboard_config(config))
    clean_name = str(name or "").strip()
    if not clean_name:
        raise ValueError("department name is required")
    existing_ids = {str(item.get("id") or "") for item in normalized["department_profiles"]}
    desired_id = _profile_id(profile_id or clean_name)
    if desired_id == "profile" and not profile_id:
        desired_id = f"dept-{len(existing_ids) + 1}"
    if desired_id in existing_ids:
        raise ValueError(f"Gerrit department profile {desired_id} already exists")
    owner_list = [str(owner or "").strip() for owner in owners or [] if str(owner or "").strip()]
    defaults = normalized["defaults"]
    normalized["department_profiles"].append({
        "id": desired_id,
        "name": clean_name,
        "owners": list(dict.fromkeys(owner_list)),
        "list_limit": defaults["list_limit"],
        "query_limit": defaults["query_limit"],
        "query_page_size": defaults["query_page_size"],
        "max_history_changes": defaults["max_history_changes"],
    })
    return normalized


def assign_owner_to_gerrit_department(config: dict[str, Any], profile_id: str, owner: str) -> dict[str, Any]:
    """Return config with owner added to a Gerrit department profile."""
    normalized = normalize_gerrit_dashboard_config(_to_raw_dashboard_config(config))
    clean_owner = str(owner or "").strip()
    clean_profile_id = str(profile_id or "").strip()
    if not clean_owner:
        raise ValueError("owner is required")
    for profile in normalized["department_profiles"]:
        if profile.get("id") == clean_profile_id:
            owners = [str(item or "").strip() for item in profile.get("owners") or [] if str(item or "").strip()]
            owners.append(clean_owner)
            profile["owners"] = list(dict.fromkeys(owners))
            return normalized
    raise ValueError(f"Gerrit department profile {clean_profile_id} not found")


def remove_owner_from_gerrit_department(config: dict[str, Any], profile_id: str, owner: str) -> dict[str, Any]:
    """Return config with owner removed from a Gerrit department profile."""
    normalized = normalize_gerrit_dashboard_config(_to_raw_dashboard_config(config))
    clean_owner = str(owner or "").strip()
    clean_profile_id = str(profile_id or "").strip()
    if not clean_owner:
        raise ValueError("owner is required")
    for profile in normalized["department_profiles"]:
        if profile.get("id") == clean_profile_id:
            profile["owners"] = [
                str(item or "").strip()
                for item in profile.get("owners") or []
                if str(item or "").strip() and str(item or "").strip() != clean_owner
            ]
            return normalized
    raise ValueError(f"Gerrit department profile {clean_profile_id} not found")


def sync_gerrit_members_from_redmine_users(config: dict[str, Any], users: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Return config updated with Gerrit departments and owners from Redmine user mappings."""
    normalized = normalize_gerrit_dashboard_config(_to_raw_dashboard_config(config))
    defaults = normalized["defaults"]
    departments_by_id = {str(item.get("id") or ""): item for item in normalized["department_profiles"]}
    personal_by_owner = {str(item.get("owner") or ""): item for item in normalized["personal_profiles"]}
    for user in users or []:
        email = str(user.get("email") or "").strip()
        department_id = _profile_id(user.get("department_id") or "")
        department_name = str(user.get("department") or department_id).strip()
        name = str(user.get("name") or email).strip()
        if not email or not department_id:
            continue
        department = departments_by_id.get(department_id)
        if not department:
            department = {
                "id": department_id,
                "name": department_name or department_id,
                "owners": [],
                "list_limit": defaults["list_limit"],
                "query_limit": defaults["query_limit"],
                "query_page_size": defaults["query_page_size"],
                "max_history_changes": defaults["max_history_changes"],
            }
            normalized["department_profiles"].append(department)
            departments_by_id[department_id] = department
        owners = [str(item or "").strip() for item in department.get("owners") or [] if str(item or "").strip()]
        owners.append(email)
        department["owners"] = list(dict.fromkeys(owners))
        personal = personal_by_owner.get(email)
        if personal:
            # Redmine 用户映射是姓名的权威来源。
            personal["name"] = name
            personal["department_id"] = department_id
            personal["department"] = department.get("name") or department_name
        else:
            personal = {
                "id": _profile_id(email),
                "name": name,
                "owner": email,
                "department_id": department_id,
                "department": department.get("name") or department_name,
                "list_limit": defaults["list_limit"],
                "query_limit": defaults["query_limit"],
                "query_page_size": defaults["query_page_size"],
                "max_history_changes": defaults["max_history_changes"],
            }
            normalized["personal_profiles"].append(personal)
            personal_by_owner[email] = personal
    return normalized


def summarize_gerrit_changes(changes: Iterable[dict[str, Any]], list_limit: int = 50) -> dict[str, Any]:
    """Aggregate Gerrit changes by status and creation date."""
    list_limit = max(1, min(int(list_limit or 50), 500))
    summary = {
        "total_count": 0,
        "merged_count": 0,
        "open_count": 0,
        "abandoned_count": 0,
        "pending_review_count": 0,
    }
    buckets = {
        "daily": {},
        "weekly": {},
        "monthly": {},
        "yearly": {},
    }
    lists = {"merged": [], "open": [], "pending_review": [], "abandoned": []}
    for raw in changes or []:
        change = normalize_gerrit_change(raw)
        summary["total_count"] += 1
        status = change["status"]
        if status == "MERGED":
            summary["merged_count"] += 1
            lists["merged"].append(change)
        elif status == "ABANDONED":
            summary["abandoned_count"] += 1
            lists["abandoned"].append(change)
        else:
            summary["open_count"] += 1
            lists["open"].append(change)
            if is_pending_review(raw):
                summary["pending_review_count"] += 1
                lists["pending_review"].append(change)
        created = _parse_gerrit_datetime(change.get("created") or raw.get("createdOn") or raw.get("created"))
        if created:
            day = created.date().isoformat()
            iso = created.isocalendar()
            labels = {
                "daily": day,
                "weekly": f"{iso.year}-W{iso.week:02d}",
                "monthly": day[:7],
                "yearly": day[:4],
            }
            for key, label in labels.items():
                buckets[key][label] = buckets[key].get(label, 0) + 1

    for key in lists:
        lists[key] = sorted(lists[key], key=lambda item: item.get("updated") or item.get("created") or "", reverse=True)[:list_limit]

    return {
        "summary": summary,
        "trends": {
            "daily": _bucket_rows(buckets["daily"], "date", 3660),
            "weekly": _bucket_rows(buckets["weekly"], "week", 520),
            "monthly": _bucket_rows(buckets["monthly"], "month", 120),
            "yearly": _bucket_rows(buckets["yearly"], "year", 50),
        },
        "lists": lists,
    }


def filter_gerrit_changes_by_created_date(
    changes: Iterable[dict[str, Any]],
    start: Any,
    end: Any,
    limit: int = 500,
) -> list[dict[str, Any]]:
    """Return normalized changes whose created time is in [start, end)."""
    start_dt = _parse_gerrit_datetime(start)
    end_dt = _parse_gerrit_datetime(end)
    row_limit = max(1, min(int(limit or 500), 2000))
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in changes or []:
        change = normalize_gerrit_change(raw)
        created = _parse_gerrit_datetime(change.get("created") or raw.get("created") or raw.get("createdOn"))
        if not created:
            continue
        if start_dt and created < start_dt:
            continue
        if end_dt and created >= end_dt:
            continue
        key = change.get("number") or change.get("id") or change.get("change_id")
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        items.append(change)
    items.sort(key=lambda item: (item.get("created") or "", item.get("number") or ""), reverse=True)
    return items[:row_limit]


def summarize_gerrit_department_results(results: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Merge per-owner Gerrit dashboard stats."""
    summary = {
        "total_count": 0,
        "merged_count": 0,
        "open_count": 0,
        "abandoned_count": 0,
        "pending_review_count": 0,
    }
    trend_buckets = {
        "daily": {},
        "weekly": {},
        "monthly": {},
        "yearly": {},
    }
    for item in results or []:
        item_summary = item.get("summary") or {}
        for key in summary:
            summary[key] += int(item_summary.get(key) or 0)
        trends = item.get("trends") or {}
        for trend_key, label_key in (("daily", "date"), ("weekly", "week"), ("monthly", "month"), ("yearly", "year")):
            for row in trends.get(trend_key) or []:
                label = str(row.get(label_key) or "").strip()
                if label:
                    trend_buckets[trend_key][label] = trend_buckets[trend_key].get(label, 0) + int(row.get("count") or 0)
    return {
        "summary": summary,
        "trends": {
            "daily": _bucket_rows(trend_buckets["daily"], "date", 3660),
            "weekly": _bucket_rows(trend_buckets["weekly"], "week", 520),
            "monthly": _bucket_rows(trend_buckets["monthly"], "month", 120),
            "yearly": _bucket_rows(trend_buckets["yearly"], "year", 50),
        },
        "users": list(results or []),
    }


def normalize_gerrit_change(raw: dict[str, Any]) -> dict[str, Any]:
    number = raw.get("_number") or raw.get("number") or raw.get("id") or ""
    owner = raw.get("owner") or {}
    created = _parse_gerrit_datetime(raw.get("created") or raw.get("createdOn"))
    updated = _parse_gerrit_datetime(raw.get("updated") or raw.get("lastUpdated") or raw.get("created") or raw.get("createdOn"))
    status = str(raw.get("status") or "").upper() or "NEW"
    return {
        "number": str(number),
        "id": str(raw.get("id") or raw.get("change_id") or raw.get("changeId") or ""),
        "change_id": str(raw.get("change_id") or raw.get("changeId") or ""),
        "subject": str(raw.get("subject") or ""),
        "project": str(raw.get("project") or ""),
        "branch": str(raw.get("branch") or ""),
        "topic": str(raw.get("topic") or ""),
        "status": status,
        "owner": {
            "name": str(owner.get("name") or ""),
            "email": str(owner.get("email") or ""),
            "username": str(owner.get("username") or ""),
        } if isinstance(owner, dict) else {},
        "created": created.isoformat(timespec="seconds") if created else str(raw.get("created") or raw.get("createdOn") or ""),
        "updated": updated.isoformat(timespec="seconds") if updated else str(raw.get("updated") or raw.get("lastUpdated") or ""),
        "url": str(raw.get("url") or ""),
        "wip": bool(raw.get("work_in_progress") or raw.get("wip")),
    }


def is_pending_review(raw: dict[str, Any]) -> bool:
    status = str(raw.get("status") or "").upper()
    if status not in {"", "NEW"}:
        return False
    if raw.get("work_in_progress") or raw.get("wip"):
        return False
    for record in raw.get("submitRecords") or raw.get("submit_records") or []:
        if str(record.get("status") or "").upper() == "OK":
            return False
    labels = raw.get("labels") or {}
    if isinstance(labels, dict):
        code_review = labels.get("Code-Review") or labels.get("code-review") or {}
        if isinstance(code_review, dict):
            approved = code_review.get("approved") or {}
            if approved:
                return False
            for vote in code_review.get("all") or []:
                try:
                    if int(vote.get("value") or 0) >= 2:
                        return False
                except (TypeError, ValueError):
                    continue
    return True


def _to_raw_dashboard_config(config: dict[str, Any]) -> dict[str, Any]:
    if "defaults" not in (config or {}):
        return config or {}
    return {
        **(config or {}),
        "department_defaults": (config or {}).get("defaults") or {},
        "chart_date_ranges": (config or {}).get("chart_date_ranges") or {},
    }


def _parse_gerrit_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc).replace(tzinfo=None)
    text = str(value or "").strip()
    if not text:
        return None
    if text.isdigit():
        return datetime.fromtimestamp(int(text), tz=timezone.utc).replace(tzinfo=None)
    for candidate in (text, text.replace("Z", "+00:00")):
        try:
            parsed = datetime.fromisoformat(candidate)
            if parsed.tzinfo:
                parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
            return parsed
        except ValueError:
            pass
    trimmed = text.split(".", 1)[0]
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(trimmed, fmt)
        except ValueError:
            continue
    return None


def _bucket_rows(bucket: dict[str, int], label_key: str, limit: int) -> list[dict[str, Any]]:
    return [
        {label_key: label, "count": count}
        for label, count in sorted(bucket.items())[-limit:]
    ]


def _date_text(value: Any) -> str:
    text = str(value or "").strip()
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        return ""
