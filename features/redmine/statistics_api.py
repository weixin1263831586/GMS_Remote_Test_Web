from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Query, Request
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
    get_redmine_config_for_request,
    get_redmine_service_for_request,
)
from .dashboard import (
    filter_users_for_profile,
    issue_id_list,
    merge_resolved_trends,
    select_redmine_dashboard_profile,
    summarize_project_issues,
    with_department_profiles_from_users,
)
from .repository import (
    compute_user_overdue_stats,
    display_names_from_mapping,
    find_user_mapping_for_names,
    load_redmine_user_map_for_owner,
    owner_user_map_path,
    refresh_assignee_issue_snapshots,
)
from features.auth.service import require_authenticated_user
from features.users.clients import get_client_id_from_request


router = APIRouter()


def _request_user_id(request: Request | None) -> str:
    if request is None:
        return "legacy"
    try:
        return require_authenticated_user(request).id
    except Exception:
        # 未登录时回退到 state.current_user（内部直接调用场景），再退到 legacy。
        user = getattr(getattr(request, "state", None), "current_user", None)
        return getattr(user, "id", "") or get_client_id_from_request(request) or "legacy"


# 看板/统计数据仍按登录用户隔离；配置统一读写 configs/config_runtime.json 和
# configs/redmine_user_map.json，避免生成额外 per-user 配置目录。
def _service_for_request(request: Request | None):
    return get_redmine_service_for_request(request)


def _config_for_request(request: Request | None):
    return get_redmine_config_for_request(request)


def _missing_credentials_payload() -> dict[str, Any]:
    return {
        "success": True,
        "data": {
            "configured": False,
            "error": "Redmine credentials not configured",
            "message": "请先在 Redmine 看板设置中保存 Redmine 账号和密码/API 密码。",
        },
    }


def _has_redmine_credentials(request: Request | None) -> bool:
    try:
        creds = _config_for_request(request).load_redmine_credentials() or {}
    except Exception:
        return False
    return bool(str(creds.get("username") or "").strip() and str(creds.get("password") or "").strip())


def _user_map_for_request(request: Request | None) -> list[dict[str, Any]]:
    return load_redmine_user_map_for_owner(_request_user_id(request))


def _dashboard_config_for_request(request: Request | None) -> dict[str, Any]:
    return with_department_profiles_from_users(
        _config_for_request(request).get_redmine_dashboard_config(),
        _user_map_for_request(request),
    )


def _user_map_mtime_for_request(request: Request | None) -> float:
    path = owner_user_map_path(_request_user_id(request))
    return path.stat().st_mtime if path.exists() else 0


async def _live_stats_for_user(service, user_id: int) -> dict[str, Any]:
    """Fetch full issue counts + resolved trends from Redmine for a user id.

    The local DB only holds issues synced for the configured sync user, so a
    personal dashboard's resolved-by-day/week/month/year bars would otherwise
    under-count anyone whose closed issues were never synced. We pull the full
    closed-issue trend live from Redmine (same channel the department view uses)
    so the bars match Gerrit's "show everything" behaviour. Trends are cached
    client-side (``_ASSIGNEE_TREND_CACHE``); on any failure we fall back to the
    local-DB trends the repository already returned.
    """
    client = service.agent._make_client()
    try:
        data: dict[str, Any] = {}
        try:
            data.update(await client.count_issues_by_assignee(user_id))
        except Exception:
            pass
        try:
            data.update(await client.resolved_trends_by_assignee(user_id))
        except Exception:
            pass
        return data
    finally:
        await client.close()


def _redmine_user_names(user: Any) -> list[str]:
    first = str(getattr(user, "firstname", "") or "").strip()
    last = str(getattr(user, "lastname", "") or "").strip()
    login = str(getattr(user, "login", "") or "").strip()
    mail = str(getattr(user, "mail", "") or getattr(user, "email", "") or "").strip()
    names = [
        f"{first} {last}".strip(),
        f"{last} {first}".strip(),
        mail,
        login,
    ]
    return list(dict.fromkeys(name for name in names if name))


async def _current_redmine_user(service) -> Any | None:
    client = service.agent._make_client()
    try:
        return await client.get_current_user()
    except Exception:
        return None
    finally:
        await client.close()


async def _current_redmine_user_mapping(service) -> dict[str, Any] | None:
    user = await _current_redmine_user(service)
    if user is None:
        return None
    try:
        user_id = int(getattr(user, "id"))
    except (TypeError, ValueError):
        return None
    names = _redmine_user_names(user)
    return {
        "id": user_id,
        "name": names[0] if names else str(user_id),
        "aliases": names[1:],
        "email": str(getattr(user, "mail", "") or getattr(user, "email", "") or "").strip(),
    }


@router.get("/statistics/workload")
async def get_workload_statistics(
    request: Request = None,
    stale_days: int | None = Query(None, ge=1, le=30),
    list_limit: int = Query(30, ge=1, le=100),
    name: str = Query(""),
    refresh: bool = Query(False),
):
    if not _has_redmine_credentials(request):
        return _missing_credentials_payload()
    service = _service_for_request(request)
    # Check cache
    stats_cfg = _get_redmine_stats_config(request)
    stale_days = int(stale_days or stats_cfg["stale_days"])
    cache_key = f"{_request_user_id(request)}:{stale_days}:{list_limit}:{name}"
    now_ts = datetime.now().timestamp()
    cached = _check_ttl_cache(_WORKLOAD_STATS_CACHE, cache_key, stats_cfg["cache_ttl"], now_ts, refresh=refresh)
    if cached is not None:
        return {"success": True, "data": {**cached, "cache_hit": True}}

    # Resolve the target user once: a selected name, else the current login user.
    # When the name maps to a user_map entry we also fetch live Redmine counts +
    # resolved trends (the local DB only holds partial snapshots), so the
    # personal dashboard's closed/total/resolved-trend numbers are accurate.
    user_map = _user_map_for_request(request)
    live_stats: dict[str, Any] = {}
    candidate_names = [name] if name else []
    if not candidate_names:
        candidate_names = await _resolve_owner_names(request, service)
    mapped = find_user_mapping_for_names(user_map, candidate_names) if candidate_names else None
    if mapped:
        owner_names = display_names_from_mapping(mapped)
        display_names = owner_names
        live_stats = await _live_stats_for_user(service, int(mapped["id"]))
        if refresh:
            try:
                client = service.agent._make_client()
                try:
                    await refresh_assignee_issue_snapshots(
                        client,
                        service.repository,
                        int(mapped["id"]),
                        issue_limit=max(list_limit, 100),
                        window_days=stats_cfg["window_days"],
                    )
                finally:
                    await client.close()
            except Exception:
                pass
    else:
        current_user = None if name else await _current_redmine_user_mapping(service)
        if current_user:
            owner_names = display_names_from_mapping(current_user)
            display_names = owner_names
            live_stats = await _live_stats_for_user(service, int(current_user["id"]))
        else:
            owner_names = [n for n in candidate_names if n]
            display_names = owner_names
    # Collect extra names from run history for matching only, not display
    extra_names = []
    if not name:
        try:
            for run in service.repository.list_runs(10):
                assigned_to = str(run.get("assigned_to") or "").strip()
                if assigned_to:
                    extra_names.append(assigned_to)
        except Exception:
            pass
    all_names = owner_names + [n for n in extra_names if n]
    data = service.repository.get_workload_statistics(
        owner_names=all_names,
        stale_days=stale_days,
        list_limit=list_limit,
        display_names=display_names,
        window_days=stats_cfg["window_days"],
    )
    if live_stats:
        # Live total/open/closed counters always win (the local DB snapshot is
        # incomplete for non-sync users). Resolved trends only override the
        # local-DB bars when the live fetch actually returned them, so a
        # Redmine outage falls back to whatever the DB has instead of blanks.
        data.update(live_stats)
    data["generated_at"] = datetime.now().isoformat(timespec="seconds")
    _update_ttl_cache(_WORKLOAD_STATS_CACHE, cache_key, now_ts, data)
    return {"success": True, "data": data}


async def _department_user_overdue(
    client,
    repository,
    user: dict[str, Any],
    stale_days: int,
    issue_limit: int,
    window_days: int = 0,
    force_refresh: bool = False,
) -> dict[str, Any]:
    try:
        return await compute_user_overdue_stats(
            client,
            repository,
            user,
            stale_days,
            issue_limit,
            window_days,
            force_refresh=force_refresh,
        )
    except Exception as exc:
        return _empty_user_stats(user, error=str(exc))


@router.get("/statistics/resolved-by-date")
async def get_resolved_issues_by_date(
    request: Request = None,
    start: str = Query("", description="起始日期 YYYY-MM-DD（含）"),
    end: str = Query("", description="结束日期 YYYY-MM-DD（不含，即次日）"),
    names: str = Query("", description="指派人姓名列表，逗号分隔；为空则不过滤"),
    profile_id: str = Query("", description="部门看板 profile_id；传入时按部门配置展开成员和别名"),
    limit: int = Query(500, ge=1, le=2000),
):
    if not _has_redmine_credentials(request):
        return _missing_credentials_payload()
    service = _service_for_request(request)
    """按日期范围查询已解决的 Redmine issue（供趋势柱状图点击查看明细）。"""
    owner_names = [n.strip() for n in names.split(",") if n.strip()] if names else []
    profile_key = str(profile_id or "").strip()
    profile_users: list[dict[str, Any]] = []
    if profile_key:
        dashboard_cfg = _dashboard_config_for_request(request)
        profile = select_redmine_dashboard_profile(dashboard_cfg, profile_key)
        if str(profile.get("id") or "") == profile_key:
            profile_users = list(filter_users_for_profile(_user_map_for_request(request), profile))
            for user in profile_users:
                owner_names.extend(display_names_from_mapping(user))
    owner_names = list(dict.fromkeys(name for name in owner_names if name))
    try:
        if profile_users:
            # Live fetch per assignee so the drill-down reflects the whole
            # department, independent of which issues were synced to the local
            # DB (the DB only holds issues assigned to the configured sync user).
            client = service.agent._make_client()
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
            issues = service.repository.get_resolved_issues_by_date(
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
    request: Request = None,
    stale_days: int | None = Query(None, ge=1, le=30),
    list_limit: int | None = Query(None, ge=1, le=500),
    issue_limit: int | None = Query(None, ge=1, le=2000),
    profile_id: str = Query(""),
    refresh: bool = Query(False),
):
    if not _has_redmine_credentials(request):
        return _missing_credentials_payload()
    service = _service_for_request(request)
    now_ts = datetime.now().timestamp()
    stats_cfg = _get_redmine_stats_config(request)
    dashboard_cfg = _dashboard_config_for_request(request)
    profile = select_redmine_dashboard_profile(dashboard_cfg, profile_id)
    effective_stale_days = int(stale_days or profile.get("stale_days") or stats_cfg["stale_days"])
    effective_list_limit = int(list_limit or profile.get("list_limit") or dashboard_cfg["defaults"]["list_limit"])
    effective_issue_limit = int(issue_limit or profile.get("issue_limit") or dashboard_cfg["defaults"]["issue_limit"])
    cache_key = (
        f"{profile.get('id')}:{effective_stale_days}:{effective_list_limit}:{effective_issue_limit}:"
        f"{_user_map_mtime_for_request(request)}:"
        f"{_request_user_id(request)}"
    )
    cached = _check_ttl_cache(_DEPARTMENT_OVERDUE_CACHE, cache_key, stats_cfg["cache_ttl"], now_ts, refresh=refresh)
    if cached is not None:
        return {"success": True, "data": {**cached, "cache_hit": True}}

    users = filter_users_for_profile(_user_map_for_request(request), profile)
    if not users and str(profile.get("id") or "") == "all":
        current_user = await _current_redmine_user_mapping(service)
        if current_user:
            users = [current_user]
    try:
        client = service.agent._make_client()
    except Exception as exc:
        return {"success": False, "error": f"Redmine client unavailable: {exc}"}
    window_days = int(profile.get("window_days") or stats_cfg["window_days"])
    semaphore = asyncio.Semaphore(4)

    async def _safe_user(user: dict[str, Any]) -> dict[str, Any]:
        async with semaphore:
            return await _department_user_overdue(
                client,
                service.repository,
                user,
                effective_stale_days,
                effective_issue_limit,
                window_days,
                force_refresh=refresh,
            )

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
    request: Request = None,
    profile_id: str = Query(""),
    refresh: bool = Query(False),
):
    if not _has_redmine_credentials(request):
        return _missing_credentials_payload()
    service = _service_for_request(request)
    now_ts = datetime.now().timestamp()
    manager = _config_for_request(request)
    stats_cfg = _get_redmine_stats_config(request)
    dashboard_cfg = manager.get_redmine_dashboard_config()
    profiles = dashboard_cfg.get("project_profiles") or []
    if not profiles:
        return JSONResponse(status_code=404, content={"success": False, "error": "project dashboard is not configured"})
    requested = str(profile_id or "").strip()
    profile = next((item for item in profiles if item.get("id") == requested or item.get("project_id") == requested), profiles[0])
    project_id = str(profile.get("project_id") or profile.get("id") or "").strip()
    issue_limit = int(profile.get("issue_limit") or dashboard_cfg["defaults"]["issue_limit"])
    list_limit = int(profile.get("list_limit") or dashboard_cfg["defaults"]["list_limit"])
    cache_key = f"{_request_user_id(request)}:{project_id}:{issue_limit}:{list_limit}"
    cached = _check_ttl_cache(_PROJECT_STATS_CACHE, cache_key, stats_cfg["cache_ttl"], now_ts, refresh=refresh)
    if cached is not None:
        return {"success": True, "data": {**cached, "cache_hit": True}}

    client = None
    try:
        client = service.agent._make_client()
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
