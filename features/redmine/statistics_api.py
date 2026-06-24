from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from .api import (
    _DEPARTMENT_OVERDUE_CACHE,
    _PROJECT_STATS_CACHE,
    _WORKLOAD_STATS_CACHE,
    _check_ttl_cache,
    _empty_user_stats,
    _get_redmine_stats_config,
    _resolve_owner_names,
    _update_ttl_cache,
    config_manager,
    redmine_service,
)
from .dashboard import (
    filter_users_for_profile,
    issue_id_list,
    merge_resolved_trends,
    select_redmine_dashboard_profile,
    summarize_project_issues,
)
from .repository import (
    USER_MAP_PATH,
    compute_user_overdue_stats,
    display_names_from_mapping,
    find_user_mapping,
    load_redmine_user_map,
)


router = APIRouter()

@router.get("/statistics/workload")
async def get_workload_statistics(
    stale_days: int = Query(20, ge=1, le=30),
    list_limit: int = Query(30, ge=1, le=100),
    name: str = Query(""),
):
    # Check cache
    stats_cfg = _get_redmine_stats_config()
    cache_key = f"{stale_days}:{list_limit}:{name}"
    now_ts = datetime.now().timestamp()
    cached = _check_ttl_cache(_WORKLOAD_STATS_CACHE, cache_key, stats_cfg["cache_ttl"], now_ts)
    if cached is not None:
        return {"success": True, "data": {**cached, "cache_hit": True}}

    owner_names = []
    display_names = []
    live_counts: dict[str, int] = {}
    if name:
        mapped = find_user_mapping(name)
        if mapped:
            owner_names = display_names_from_mapping(mapped)
            display_names = owner_names
            client = redmine_service.agent._make_client()
            try:
                live_counts = await client.count_issues_by_assignee(int(mapped["id"]))
                live_counts.update(await client.resolved_trends_by_assignee(int(mapped["id"])))
            except Exception:
                live_counts = {}
            finally:
                await client.close()
        else:
            owner_names = [name]
            display_names = [name]
    if not owner_names:
        owner_names = await _resolve_owner_names()
        display_names = owner_names
    # Collect extra names from run history for matching only, not display
    extra_names = []
    if not name:
        try:
            for run in redmine_service.repository.list_runs(10):
                assigned_to = str(run.get("assigned_to") or "").strip()
                if assigned_to:
                    extra_names.append(assigned_to)
        except Exception:
            pass
    all_names = owner_names + [n for n in extra_names if n]
    data = redmine_service.repository.get_workload_statistics(
        owner_names=all_names,
        stale_days=stale_days,
        list_limit=list_limit,
        display_names=display_names,
        window_days=stats_cfg["window_days"],
    )
    if live_counts:
        data.update(live_counts)
    data["generated_at"] = datetime.now().isoformat(timespec="seconds")
    _update_ttl_cache(_WORKLOAD_STATS_CACHE, cache_key, now_ts, data)
    return {"success": True, "data": data}


async def _department_user_overdue(client, user: dict[str, Any], stale_days: int, issue_limit: int, window_days: int = 0) -> dict[str, Any]:
    try:
        return await compute_user_overdue_stats(client, redmine_service.repository, user, stale_days, issue_limit, window_days)
    except Exception as exc:
        return _empty_user_stats(user, error=str(exc))


@router.get("/statistics/resolved-by-date")
async def get_resolved_issues_by_date(
    start: str = Query("", description="起始日期 YYYY-MM-DD（含）"),
    end: str = Query("", description="结束日期 YYYY-MM-DD（不含，即次日）"),
    names: str = Query("", description="指派人姓名列表，逗号分隔；为空则不过滤"),
    profile_id: str = Query("", description="部门看板 profile_id；传入时按部门配置展开成员和别名"),
    limit: int = Query(500, ge=1, le=2000),
):
    """按日期范围查询已解决的 Redmine issue（供趋势柱状图点击查看明细）。"""
    owner_names = [n.strip() for n in names.split(",") if n.strip()] if names else []
    profile_key = str(profile_id or "").strip()
    profile_users: list[dict[str, Any]] = []
    if profile_key:
        dashboard_cfg = config_manager.get_redmine_dashboard_config()
        profile = select_redmine_dashboard_profile(dashboard_cfg, profile_key)
        if str(profile.get("id") or "") == profile_key:
            profile_users = list(filter_users_for_profile(load_redmine_user_map(), profile))
            for user in profile_users:
                owner_names.extend(display_names_from_mapping(user))
    owner_names = list(dict.fromkeys(name for name in owner_names if name))
    try:
        if profile_users:
            # Live fetch per assignee so the drill-down reflects the whole
            # department, independent of which issues were synced to the local
            # DB (the DB only holds issues assigned to the configured sync user).
            client = redmine_service.agent._make_client()
            semaphore = asyncio.Semaphore(4)

            async def _user_issues(user: dict[str, Any]) -> list[dict[str, Any]]:
                async with semaphore:
                    return await client.fetch_resolved_issues_by_assignee(
                        assignee_id=int(user["id"]),
                        start=start.strip(),
                        end=end.strip(),
                        limit=limit,
                    )

            try:
                batches = await asyncio.gather(*[_user_issues(u) for u in profile_users])
            finally:
                await client.close()
            seen: set[int] = set()
            issues: list[dict[str, Any]] = []
            for batch in batches:
                for item in batch:
                    iid = int(item.get("issue_id") or 0)
                    if iid and iid not in seen:
                        seen.add(iid)
                        issues.append(item)
            issues.sort(key=lambda i: (i.get("resolved_on") or "", i.get("issue_id") or 0), reverse=True)
            issues = issues[:limit]
        else:
            issues = redmine_service.repository.get_resolved_issues_by_date(
                owner_names=owner_names or None, start=start.strip(), end=end.strip(), limit=limit,
            )
    except Exception as exc:
        return {"success": False, "error": str(exc)}
    items = [
        {
            "issue_id": i.get("issue_id"),
            "subject": i.get("subject"),
            "status_name": i.get("status_name"),
            "priority_name": i.get("priority_name"),
            "assigned_to_name": i.get("assigned_to_name"),
            "closed_on": (i.get("resolved_on") or i.get("closed_on") or "")[:10],
            "resolved_on": (i.get("resolved_on") or i.get("closed_on") or "")[:19],
            "updated_on": (i.get("updated_on") or "")[:19],
            "tracker_name": i.get("tracker_name"),
            "category": i.get("category"),
        }
        for i in issues
    ]
    return {"success": True, "data": {"start": start, "end": end, "count": len(items), "items": items}}


@router.get("/statistics/department-overdue")
async def get_department_overdue_statistics(
    stale_days: int | None = Query(None, ge=1, le=30),
    list_limit: int | None = Query(None, ge=1, le=500),
    issue_limit: int | None = Query(None, ge=1, le=2000),
    profile_id: str = Query(""),
    refresh: bool = Query(False),
):
    now_ts = datetime.now().timestamp()
    stats_cfg = _get_redmine_stats_config()
    dashboard_cfg = config_manager.get_redmine_dashboard_config()
    profile = select_redmine_dashboard_profile(dashboard_cfg, profile_id)
    effective_stale_days = int(stale_days or profile.get("stale_days") or stats_cfg["stale_days"])
    effective_list_limit = int(list_limit or profile.get("list_limit") or dashboard_cfg["defaults"]["list_limit"])
    effective_issue_limit = int(issue_limit or profile.get("issue_limit") or dashboard_cfg["defaults"]["issue_limit"])
    cache_key = (
        f"{profile.get('id')}:{effective_stale_days}:{effective_list_limit}:{effective_issue_limit}:"
        f"{USER_MAP_PATH.stat().st_mtime if USER_MAP_PATH.exists() else 0}"
    )
    cached = _check_ttl_cache(_DEPARTMENT_OVERDUE_CACHE, cache_key, stats_cfg["cache_ttl"], now_ts, refresh=refresh)
    if cached is not None:
        return {"success": True, "data": {**cached, "cache_hit": True}}

    users = filter_users_for_profile(load_redmine_user_map(), profile)
    try:
        client = redmine_service.agent._make_client()
    except Exception as exc:
        return {"success": False, "error": f"Redmine client unavailable: {exc}"}
    window_days = int(profile.get("window_days") or stats_cfg["window_days"])
    semaphore = asyncio.Semaphore(4)

    async def _safe_user(user: dict[str, Any]) -> dict[str, Any]:
        async with semaphore:
            return await _department_user_overdue(client, user, effective_stale_days, effective_issue_limit, window_days)

    try:
        if users:
            results = await asyncio.gather(*[_safe_user(user) for user in users])
        else:
            results = []
        for item in results:
            item["overdue_issues"] = item.get("overdue_issues", [])[:effective_list_limit]
            item["overdue_issue_ids"] = issue_id_list(item.get("overdue_issues", []))
        summary = {
            "user_count": len(results),
            "open_count": sum(int(item.get("open_count") or 0) for item in results),
            "waiting_my_reply": sum(int(item.get("waiting_my_reply") or 0) for item in results),
            "no_reply_3_days": sum(int(item.get("no_reply_3_days") or 0) for item in results),
            "waiting_customer_reply": sum(int(item.get("waiting_customer_reply") or 0) for item in results),
            "customer_no_reply_3_days": sum(int(item.get("customer_no_reply_3_days") or 0) for item in results),
            "rk_colleague_no_reply_3_days": sum(int(item.get("rk_colleague_no_reply_3_days") or 0) for item in results),
            "total_owned": sum(int(item.get("total_owned") or 0) for item in results),
        }
        data = {
            "summary": summary,
            "users": results,
            "trends": merge_resolved_trends(results),
            "profile": profile,
            "available_profiles": dashboard_cfg["profiles"],
            "stale_days": effective_stale_days,
            "window_days": window_days,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "cache_hit": False,
        }
        _update_ttl_cache(_DEPARTMENT_OVERDUE_CACHE, cache_key, now_ts, data)
        return {"success": True, "data": data}
    except Exception as exc:
        return {"success": False, "error": f"department overdue statistics failed: {exc}"}
    finally:
        await client.close()


@router.get("/statistics/project")
async def get_project_statistics(
    profile_id: str = Query(""),
    refresh: bool = Query(False),
):
    now_ts = datetime.now().timestamp()
    stats_cfg = _get_redmine_stats_config()
    dashboard_cfg = config_manager.get_redmine_dashboard_config()
    profiles = dashboard_cfg.get("project_profiles") or []
    if not profiles:
        return JSONResponse(status_code=404, content={"success": False, "error": "project dashboard is not configured"})
    requested = str(profile_id or "").strip()
    profile = next((item for item in profiles if item.get("id") == requested or item.get("project_id") == requested), profiles[0])
    project_id = str(profile.get("project_id") or profile.get("id") or "").strip()
    issue_limit = int(profile.get("issue_limit") or dashboard_cfg["defaults"]["issue_limit"])
    list_limit = int(profile.get("list_limit") or dashboard_cfg["defaults"]["list_limit"])
    cache_key = f"{project_id}:{issue_limit}:{list_limit}"
    cached = _check_ttl_cache(_PROJECT_STATS_CACHE, cache_key, stats_cfg["cache_ttl"], now_ts, refresh=refresh)
    if cached is not None:
        return {"success": True, "data": {**cached, "cache_hit": True}}

    client = None
    try:
        client = redmine_service.agent._make_client()
        issues = await client.fetch_project_issues(project_id=project_id, status_id="*", limit=issue_limit)
        data = summarize_project_issues(issues, list_limit=list_limit)
        data.update({
            "profile": profile,
            "available_profiles": profiles,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "cache_hit": False,
        })
        _update_ttl_cache(_PROJECT_STATS_CACHE, cache_key, now_ts, data)
        return {"success": True, "data": data}
    except Exception as exc:
        return {"success": False, "error": f"project statistics failed: {exc}"}
    finally:
        if client is not None:
            await client.close()
