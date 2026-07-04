"""Persistence for RedmineAgent nightly triage."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from foundation.config import settings


DB_PATH = settings.data_root / "redmine/redmine.sqlite3"
DOCS_DIR = settings.data_root / "redmine/docs"
USER_MAP_PATH = settings.project_root / "configs/redmine_user_map.json"


def owner_redmine_root(owner_id: str) -> Path:
    safe_owner = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in str(owner_id or "").strip())
    return settings.data_root / "redmine/by_user" / (safe_owner or "anonymous")


def owner_db_path(owner_id: str) -> Path:
    return owner_redmine_root(owner_id) / "redmine.sqlite3"


def owner_docs_dir(owner_id: str) -> Path:
    return owner_redmine_root(owner_id) / "docs"


def owner_attachments_dir(owner_id: str) -> Path:
    return owner_redmine_root(owner_id) / "attachments"


def owner_runtime_config_path(owner_id: str) -> Path:
    return settings.project_root / "configs/config_runtime.json"


def owner_user_map_path(owner_id: str) -> Path:
    return USER_MAP_PATH


def owner_knowledge_db_path(owner_id: str) -> Path:
    """Per-user knowledge base sqlite (case_facts / mature_cases / reference_outputs ...)."""
    return owner_redmine_root(owner_id) / "knowledge.sqlite3"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


RESOLVED_STATUS_NAMES = {"已关闭", "Closed", "已解决", "Resolved", "关闭", "解决"}
NON_ACTIONABLE_STATUS_NAMES = {
    "HangUp",
    "Hang Up",
    "挂起",
    "已挂起",
    "暂停",
    "待关闭",
    "Pending Close",
}
REPORT_ATTACHMENT_RE = (
    "report",
    "test_result",
    "test-result",
    "testresult",
    "tradefed",
    "cts",
    "gts",
    "vts",
    "gms",
    "result",
    "测试报告",
    "测试结果",
)
REPORT_ATTACHMENT_EXTENSIONS = (".zip", ".7z", ".rar", ".tar", ".tgz", ".gz", ".xml", ".html", ".htm", ".log", ".txt")


def _parse_dt(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                parsed = datetime.strptime(text[:19 if " " in fmt else 10], fmt)
                break
            except ValueError:
                parsed = None
        if parsed is None:
            return None
    if parsed.tzinfo is not None:
        return parsed.astimezone().replace(tzinfo=None)
    return parsed


def _time_key(value: Any, granularity: str = "day") -> str:
    """Generate date key at given granularity: day/week/month/year."""
    parsed = _parse_dt(value)
    if not parsed:
        return ""
    if granularity == "day":
        return parsed.date().isoformat()
    if granularity == "week":
        iso = parsed.isocalendar()
        return f"{iso.year}-W{iso.week:02d}"
    if granularity == "month":
        return f"{parsed.year}-{parsed.month:02d}"
    if granularity == "year":
        return str(parsed.year)
    return ""


def _norm_name(value: Any) -> str:
    return " ".join(str(value or "").strip().lower().replace("@rock-chips.com", "").split())


def _name_keys(value: Any) -> set:
    normalized = _norm_name(value)
    if not normalized:
        return set()
    compact = normalized.replace(" ", "")
    keys = {normalized, compact}
    if "@" in normalized:
        keys.add(normalized.split("@", 1)[0])
    parts = [part for part in normalized.split() if part]
    if len(parts) > 1:
        keys.add(" ".join(reversed(parts)))
        keys.add(" ".join(sorted(parts)))
        keys.add("".join(reversed(parts)))
        keys.add("".join(sorted(parts)))
    return keys


def _identity_compacts(value: Any) -> set:
    normalized = _norm_name(value)
    if not normalized:
        return set()
    values = {normalized}
    for marker in ("（", "(", "【", "["):
        if marker in normalized:
            values.add(normalized.split(marker, 1)[0].strip())
    return {item.replace(" ", "") for item in values if item}


def _name_matches_keys(value: Any, owner_keys: set) -> bool:
    if not owner_keys:
        return True
    value_keys = _name_keys(value)
    if value_keys and value_keys.intersection(owner_keys):
        return True
    compacts = _identity_compacts(value)
    for key in owner_keys:
        compact_key = str(key or "").replace(" ", "")
        if len(compact_key) >= 2 and any(compact_key in compact for compact in compacts):
            return True
    return False


# ------------------------------------------------------------------
# User-map helpers (shared by executor and router)
# ------------------------------------------------------------------

_user_map_cache: tuple = (0.0, [])  # (mtime, parsed_list)


def _name_display_variants(value: Any) -> list[str]:
    text = str(value or "").strip()
    if not text:
        return []
    variants = [text]
    compact = text.replace(" ", "")
    if compact and compact != text:
        variants.append(compact)
    if " " not in text and len(text) >= 3 and all("\u4e00" <= ch <= "\u9fff" for ch in text):
        variants.append(text[0] + " " + text[1:])
    return list(dict.fromkeys(item for item in variants if item))


def _flatten_departments(payload: Any) -> list[dict[str, Any]]:
    """Return department member rows from the current departments user-map."""
    if not isinstance(payload, dict):
        return []
    result: list[dict[str, Any]] = []
    for dept in payload.get("departments") or []:
        if not isinstance(dept, dict):
            continue
        dept_id = str(dept.get("department_id") or "").strip()
        dept_name = str(dept.get("department") or "").strip()
        for member in dept.get("members") or []:
            if isinstance(member, dict) and member.get("id"):
                flat = dict(member)
                flat.setdefault("department_id", dept_id)
                flat.setdefault("department", dept_name)
                result.append(flat)
    return result


def load_redmine_user_map() -> list[dict[str, Any]]:
    global _user_map_cache
    if not USER_MAP_PATH.exists():
        _user_map_cache = (0.0, [])
        return []
    try:
        mtime = USER_MAP_PATH.stat().st_mtime
        if _user_map_cache[0] == mtime:
            return _user_map_cache[1]
        payload = json.loads(USER_MAP_PATH.read_text(encoding="utf-8"))
        result = _flatten_departments(payload)
        _user_map_cache = (mtime, result)
        return result
    except Exception:
        return []


def _load_user_map_payload_from(path) -> dict[str, Any]:
    """Load the raw user-map JSON payload (for mutation + save round-trips)."""
    if not path.exists():
        return {"departments": []}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            payload.setdefault("departments", [])
            return payload
    except Exception:
        pass
    return {"departments": []}


def _save_user_map_payload_to(path, payload: dict[str, Any]) -> None:
    """Write the raw user-map JSON payload to disk."""
    global _user_map_cache
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    if path == USER_MAP_PATH:
        _user_map_cache = (0.0, [])


def load_user_map_payload() -> dict[str, Any]:
    return _load_user_map_payload_from(USER_MAP_PATH)


def save_user_map_payload(payload: dict[str, Any]) -> None:
    _save_user_map_payload_to(USER_MAP_PATH, payload)


def load_redmine_user_map_for_owner(owner_id: str) -> list[dict[str, Any]]:
    return _flatten_departments(load_user_map_payload_for_owner(owner_id))


def load_user_map_payload_for_owner(owner_id: str) -> dict[str, Any]:
    owner_path = owner_user_map_path(owner_id)
    if owner_path.exists():
        return _load_user_map_payload_from(owner_path)
    # 无 per-user 副本时直接返回全局 payload，不在此处落盘——避免任意
    # owner（含测试用假用户）首次访问就在 configs/ 下产生残留副本。
    # per-user 副本只在用户显式保存自己的 user_map 时由
    # save_user_map_payload_for_owner 写入。
    return _load_user_map_payload_from(USER_MAP_PATH)


def save_user_map_payload_for_owner(owner_id: str, payload: dict[str, Any]) -> None:
    _save_user_map_payload_to(owner_user_map_path(owner_id), payload)


def display_names_from_mapping(item: dict[str, Any]) -> list[str]:
    values = []
    values.extend(_name_display_variants(item.get("name") or ""))
    for alias in item.get("aliases") or []:
        values.extend(_name_display_variants(alias))
    email = str(item.get("email") or "").strip()
    if email:
        values.append(email)
    return list(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))


def find_user_mapping(name: str, user_map: list[dict[str, Any]] | None = None) -> dict[str, Any] | None:
    """Find the user_map entry matching ``name``.

    ``user_map`` defaults to the global map; pass a per-owner map to match
    against an isolated user list.
    """
    return find_user_mapping_for_names(user_map if user_map is not None else load_redmine_user_map(), [name])


def find_user_mapping_for_names(user_map: list[dict[str, Any]], names: list[str]) -> dict[str, Any] | None:
    """Match any of ``names`` against ``user_map``; returns the first hit."""
    keys: set[str] = set()
    for value in names:
        keys.update(_name_keys(value))
    for item in user_map:
        for value in display_names_from_mapping(item):
            if keys.intersection(_name_keys(value)):
                return item
    return None


def _sorted_slice(bucket: dict[str, int], key_name: str, limit: int) -> list[dict[str, Any]]:
    """Return [{key_name: k, count: v}] sorted by key ascending.

    If *limit* is positive, keep only the last *limit* items (the most recent
    non-empty buckets). If *limit* is zero or negative, return every bucket.
    """
    keys = sorted(bucket.keys())
    if limit > 0:
        keys = keys[-limit:]
    return [{key_name: k, "count": bucket[k]} for k in keys]


def _looks_like_report_attachment(attachment: dict[str, Any]) -> bool:
    filename = str(attachment.get("filename") or "").strip().lower()
    if not filename:
        return False
    analysis = attachment.get("analysis_json") or {}
    if isinstance(analysis, str):
        try:
            analysis = json.loads(analysis or "{}")
        except Exception:
            analysis = {}
    if analysis.get("parsed") or analysis.get("failures") or analysis.get("summary"):
        return True
    has_report_word = any(token in filename for token in REPORT_ATTACHMENT_RE)
    has_report_ext = filename.endswith(REPORT_ATTACHMENT_EXTENSIONS)
    return has_report_word or (has_report_ext and any(token in filename for token in ("log", "result", "report", "cts", "gts", "vts", "gms")))


def _looks_like_rk_actor(actor: Any) -> bool:
    if isinstance(actor, dict):
        email = str(actor.get("user_email") or actor.get("email") or actor.get("mail") or "").strip().lower()
        if email.endswith("@rock-chips.com"):
            return True
        name = actor.get("user") or actor.get("name") or ""
    else:
        name = actor
    text = str(name or "").strip()
    if not text:
        return False
    lowered = text.lower()
    if "rock-chips.com" in lowered or "rockchip" in lowered or lowered.startswith("rk "):
        return True
    if "fae" in lowered or "瑞芯" in text:
        return True
    actor_keys = _name_keys(text)
    for item in load_redmine_user_map():
        for value in display_names_from_mapping(item):
            value_keys = _name_keys(value)
            if actor_keys.intersection(value_keys) or _name_matches_keys(text, value_keys):
                return True
    return False


def _merge_issue_snapshot(db: Any, issue: dict[str, Any], *, resolved: bool = False) -> None:
    if not isinstance(issue, dict) or not issue.get("issue_id"):
        return
    payload = dict(db.get_issue(int(issue["issue_id"])) or {})
    payload.update({key: value for key, value in issue.items() if value is not None})
    if resolved:
        payload["is_resolved"] = 1
    db.upsert_issue(payload)


async def refresh_assignee_issue_snapshots(
    client: Any,
    db: Any,
    user_id: int,
    *,
    issue_limit: int = 500,
    window_days: int = 0,
) -> bool:
    """Refresh open and recently closed snapshots for workload statistics."""
    changed = False
    try:
        live_issues = await client.fetch_open_issue_snapshots_by_assignee(
            int(user_id),
            limit=issue_limit,
            window_days=window_days,
        )
    except Exception:
        live_issues = []
    for issue in live_issues or []:
        _merge_issue_snapshot(db, issue, resolved=False)
        changed = True

    start = ""
    if int(window_days or 0) > 0:
        start = (datetime.now() - timedelta(days=int(window_days))).date().isoformat()
    try:
        resolved_issues = await client.fetch_resolved_issues_by_assignee(
            int(user_id),
            start=start,
            end="",
            limit=issue_limit,
        )
    except Exception:
        resolved_issues = []
    for issue in resolved_issues or []:
        _merge_issue_snapshot(db, issue, resolved=True)
        changed = True
    return changed


async def compute_user_overdue_stats(
    client: Any,
    db: Any,
    user: dict[str, Any],
    stale_days: int,
    issue_limit: int = 500,
    window_days: int = 0,
    force_refresh: bool = False,
) -> dict[str, Any]:
    """Compute workload + overdue stats for a single mapped user.

    Shared by the router's department-overdue endpoint, the workload endpoint,
    and the agent executor. Returns a dict with counts and overdue issue lists.

    Args:
        window_days: If > 0, only count stale issues updated within this window.
    """
    owner_names = display_names_from_mapping(user)
    user_id = int(user["id"])
    counts = await client.count_issues_by_assignee(user_id)
    workload = db.get_workload_statistics(
        owner_names=owner_names,
        stale_days=stale_days,
        list_limit=min(issue_limit, 100),
        display_names=owner_names,
        window_days=window_days,
    )
    if force_refresh or (counts.get("open_count") and int(workload.get("open_count") or 0) == 0):
        changed = await refresh_assignee_issue_snapshots(
            client,
            db,
            user_id,
            issue_limit=issue_limit,
            window_days=window_days,
        )
        if changed:
            workload = db.get_workload_statistics(
                owner_names=owner_names,
                stale_days=stale_days,
                list_limit=min(issue_limit, 100),
                display_names=owner_names,
                window_days=window_days,
            )
    # Resolve trends live from Redmine (per assignee) so the department view
    # reflects every member, independent of which issues were synced to the
    # local DB (the DB only holds issues assigned to the configured sync user).
    # Fall back to the local-DB trends if the live fetch fails.
    try:
        live_trends = await client.resolved_trends_by_assignee(user_id)
    except Exception:
        live_trends = {}
    if live_trends:
        workload.update(live_trends)
    overdue = list((workload.get("lists") or {}).get("no_reply_3_days") or [])
    now = datetime.now()
    for item in overdue:
        last_dt = _parse_dt(item.get("last_external_reply_at"))
        item["unreplied_days"] = max(0, int((now - last_dt).total_seconds() // 86400)) if last_dt else 0
        item["stale"] = True
    overdue.sort(key=lambda item: (item.get("unreplied_days") or 0, item.get("last_external_reply_at") or ""), reverse=True)
    return {
        "id": user.get("id"),
        "name": user.get("name") or "",
        "aliases": user.get("aliases") or [],
        "owner_names": owner_names,
        "total_owned": counts.get("total_owned", 0),
        "open_count": counts.get("open_count", 0),
        "closed_count": counts.get("closed_count", 0),
        "scanned_open_count": workload.get("open_count", 0),
        "waiting_my_reply": workload.get("waiting_my_reply", 0),
        "no_reply_3_days": workload.get("no_reply_3_days", 0),
        "rk_no_reply_3_days": workload.get("rk_no_reply_3_days", workload.get("no_reply_3_days", 0)),
        "waiting_customer_reply": workload.get("waiting_customer_reply", 0),
        "customer_no_reply_3_days": workload.get("customer_no_reply_3_days", 0),
        "rk_colleague_no_reply_3_days": workload.get("rk_colleague_no_reply_3_days", 0),
        "max_unreplied_days": max([item.get("unreplied_days") or 0 for item in overdue] or [0]),
        "overdue_issues": overdue,
        "resolved_daily": workload.get("resolved_daily", []),
        "resolved_weekly": workload.get("resolved_weekly", []),
        "resolved_monthly": workload.get("resolved_monthly", []),
        "resolved_yearly": workload.get("resolved_yearly", []),
        "detail_source": "local_db",
    }
