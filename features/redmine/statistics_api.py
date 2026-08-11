from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from features.users import owner_id_from_request

from .api import (
    _DEPARTMENT_OVERDUE_CACHE,
    _PROJECT_STATS_CACHE,
    _WORKLOAD_STATS_CACHE,
    _check_ttl_cache,
    _empty_user_stats,
    _get_redmine_stats_config,
    _update_ttl_cache,
    get_redmine_config_for_request,
    get_redmine_service_for_request,
    resolve_owner_names,
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


logger = logging.getLogger(__name__)

router = APIRouter()


def _request_user_id(request: Request | None) -> str:
    return owner_id_from_request(request)


# 看板数据按登录用户隔离；全局配置统一读写 configs/config_runtime.json
def _service_for_request(request: Request | None):
    return get_redmine_service_for_request(request)


def _config_for_request(request: Request | None):
    return get_redmine_config_for_request(request)


def _missing_credentials_payload(message: str | None = None) -> dict[str, Any]:
    return {
        "success": True,
        "data": {
            "configured": False,
            "error": "Redmine credentials not configured",
            "message": message or "请先在 Redmine 看板设置中保存 Redmine 地址、账号和密码/API 密码。",
        },
    }


def _has_redmine_credentials(request: Request | None) -> bool:
    try:
        manager = _config_for_request(request)
        base_url = manager.get_redmine_base_url()
        parsed = urlparse(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return False
        creds = manager.load_redmine_credentials() or {}
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


async def _live_stats_for_user(service, user_id: int, freshness_days: int = 180) -> dict[str, Any]:
    """从 Redmine 实时拉取用户工单计数和已解决趋势。

    本地 DB 只保存同步用户的工单快照，个人看板的已解决趋势需实时拉取。
    近 freshness_days 天的趋势实时拉取，更早的走长期缓存。
    """
    try:
        client = service.agent._make_client()
    except Exception as exc:
        logger.warning("Redmine live stats client unavailable for user %s: %s", user_id, exc)
        return {}
    try:
        # 两个实时接口共用同一 Session（非线程安全），必须串行调用
        data: dict[str, Any] = {}
        try:
            data.update(await client.count_issues_by_assignee(user_id))
        except Exception as exc:
            logger.warning("Redmine live count_issues_by_assignee failed for user %s: %s", user_id, exc, exc_info=True)
        try:
            data.update(await client.resolved_trends_by_assignee(user_id, freshness_days=freshness_days))
        except Exception as exc:
            logger.warning("Redmine live resolved_trends_by_assignee failed for user %s: %s", user_id, exc, exc_info=True)
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
    except Exception as exc:
        logger.warning("Failed to load current Redmine user: %s", exc)
        return None
    finally:
        await client.close()


async def _current_redmine_user_mapping(service) -> dict[str, Any] | None:
    user = await _current_redmine_user(service)
    if user is None:
        return None
    try:
        user_id = int(user.id)
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
    request: Request,
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
    freshness_days = int(stats_cfg.get("freshness_days") or 180)
    cache_key = f"{_request_user_id(request)}:{stale_days}:{list_limit}:{name}"
    now_ts = datetime.now().timestamp()
    cached = _check_ttl_cache(_WORKLOAD_STATS_CACHE, cache_key, stats_cfg["cache_ttl"], now_ts, refresh=refresh)
    if cached is not None:
        return {"success": True, "data": {**cached, "cache_hit": True}}

    # 解析目标用户：指定姓名或当前登录用户。映射到 user_map 时拉取实时数据
    user_map = _user_map_for_request(request)
    live_stats: dict[str, Any] = {}
    snapshot_user_id: int | None = None
    candidate_names = [name] if name else []
    if not candidate_names:
        candidate_names = await resolve_owner_names(request, service)
    mapped = find_user_mapping_for_names(user_map, candidate_names) if candidate_names else None
    if mapped:
        snapshot_user_id = int(mapped["id"])
        owner_names = display_names_from_mapping(mapped)
        display_names = owner_names
        live_stats = await _live_stats_for_user(
            service,
            snapshot_user_id,
            freshness_days=freshness_days,
        )
    else:
        # /users 会为当前 Redmine 用户插入合成选项，前端回传其显示名。
        # 将其视为当前 Redmine 账号而非回退到不完整的本地快照。
        current_user = await _current_redmine_user_mapping(service)
        selected_is_current = bool(
            current_user
            and (
                not name
                or find_user_mapping_for_names([current_user], candidate_names)
            )
        )
        if selected_is_current:
            snapshot_user_id = int(current_user["id"])
            owner_names = display_names_from_mapping(current_user)
            display_names = owner_names
            live_stats = await _live_stats_for_user(
                service,
                snapshot_user_id,
                freshness_days=freshness_days,
            )
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
        except Exception as exc:
            logger.debug("Failed to load recent Redmine runs for owner matching: %s", exc, exc_info=True)
    all_names = owner_names + [n for n in extra_names if n]
    data = service.repository.get_workload_statistics(
        owner_names=all_names,
        stale_days=stale_days,
        list_limit=list_limit,
        display_names=display_names,
        window_days=stats_cfg["window_days"],
        organization_user_map=user_map,
    )
    snapshot_refresh_needed = bool(
        snapshot_user_id is not None
        and (
            refresh
            or (
                int(live_stats.get("open_count") or 0) > 0
                and int(data.get("open_count") or 0) == 0
            )
        )
    )
    if snapshot_refresh_needed:
        try:
            client = service.agent._make_client()
            try:
                changed = await refresh_assignee_issue_snapshots(
                    client,
                    service.repository,
                    snapshot_user_id,
                    issue_limit=max(list_limit, 100),
                    window_days=stats_cfg["window_days"],
                )
            finally:
                await client.close()
            if changed:
                data = service.repository.get_workload_statistics(
                    owner_names=all_names,
                    stale_days=stale_days,
                    list_limit=list_limit,
                    display_names=display_names,
                    window_days=stats_cfg["window_days"],
                    organization_user_map=user_map,
                )
        except Exception as exc:
            logger.warning(
                "Failed to refresh Redmine snapshots for user %s: %s",
                snapshot_user_id,
                exc,
            )
    if refresh:
        stale_items = list((data.get("lists") or {}).get("no_reply_3_days") or [])
        refresh_one = getattr(service, "refresh_issue_metadata", None)
        if callable(refresh_one) and stale_items:
            refreshed = False
            refresh_errors: list[str] = []
            for item in stale_items[: min(list_limit, 30)]:
                try:
                    issue_id = int(item.get("issue_id") or 0)
                except (TypeError, ValueError):
                    issue_id = 0
                if not issue_id:
                    continue
                try:
                    await refresh_one(issue_id)
                    refreshed = True
                except Exception as exc:
                    refresh_errors.append(f"#{issue_id}: {exc}")
                    logger.debug("Personal workload metadata refresh failed for #%s", issue_id, exc_info=True)
            if refreshed:
                data = service.repository.get_workload_statistics(
                    owner_names=all_names,
                    stale_days=stale_days,
                    list_limit=list_limit,
                    display_names=display_names,
                    window_days=stats_cfg["window_days"],
                    organization_user_map=user_map,
                )
            if refresh_errors:
                data["refresh_warning"] = "部分工单未能从 Redmine 刷新：" + "；".join(refresh_errors[:3])
    if live_stats:
        # 实时计数始终优先（本地快照不完整）；趋势仅在有实时数据时覆盖
        data.update(live_stats)
    data.setdefault("meta", {})["count_source"] = "redmine_live" if live_stats else "local_snapshot"
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
    organization_user_map: list[dict[str, Any]] | None = None,
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
            organization_user_map=organization_user_map,
        )
    except Exception as exc:
        return _empty_user_stats(user, error=str(exc))


@router.get("/statistics/resolved-by-date")
async def get_resolved_issues_by_date(
    request: Request,
    start: str = Query("", description="起始日期 YYYY-MM-DD（含）"),
    end: str = Query("", description="结束日期 YYYY-MM-DD（不含，即次日）"),
    names: str = Query("", description="指派人姓名列表，逗号分隔；为空则不过滤"),
    profile_id: str = Query("", description="部门看板 profile_id；传入时按部门配置展开成员和别名"),
    limit: int = Query(500, ge=1, le=2000),
):
    service = _service_for_request(request)
    """按日期范围查询已解决的 Redmine issue（供趋势柱状图查看明细）。"""
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
            # 按指派人实时拉取，使部门明细不依赖本地 DB 同步范围
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
    request: Request,
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
    organization_user_map = _user_map_for_request(request)
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
                organization_user_map=organization_user_map,
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
    request: Request,
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
