"""Gerrit dashboard HTTP API and page."""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from features.auth.service import require_authenticated_user
from features.gerrit.config import (
    add_gerrit_department_profile,
    add_gerrit_personal_profile,
    assign_owner_to_gerrit_department,
    filter_gerrit_changes_by_created_date,
    remove_owner_from_gerrit_department,
    select_gerrit_department_profile,
    select_gerrit_personal_profile,
    summarize_gerrit_changes,
    summarize_gerrit_department_results,
    sync_gerrit_members_from_redmine_users,
)
from features.gerrit.service import (
    _effective_history_limit,
    _extract_query_limit,
    _owners_for_department_profile,
    _query_gerrit_dual_mode,
    _select_profile,
)
from features.gerrit.settings import config_manager
from features.redmine.users import load_redmine_user_map_for_owner


router = APIRouter(prefix="/api/gerrit-dashboard")
page_router = APIRouter()
_STATS_CACHE: dict[str, Any] = {}


def _request_user_id(request: Request) -> str:
    return require_authenticated_user(request).id


def _config_for_request(request: Request):
    # Gerrit 看板配置统一读写 configs/config_runtime.json。
    return config_manager.for_owner(_request_user_id(request))


def _redmine_users_for_request(request: Request) -> list[dict[str, Any]]:
    # 部门成员邮箱→中文名映射读登录用户的 user_map（与 Redmine 看板一致）。
    return load_redmine_user_map_for_owner(_request_user_id(request))


def _dashboard_config_for_request(request: Request) -> dict[str, Any]:
    cfg = _config_for_request(request).get_gerrit_dashboard_config()
    return sync_gerrit_members_from_redmine_users(
        cfg,
        _redmine_users_for_request(request),
    )


@router.get("/config")
async def get_gerrit_dashboard_config(request: Request):
    manager = _config_for_request(request)
    return {"success": True, "data": _public_config(_dashboard_config_for_request(request), manager=manager)}


@router.post("/config")
async def update_gerrit_dashboard_config(request: Request):
    body = await request.json()
    manager = _config_for_request(request)
    current = manager.get_gerrit_dashboard_config()
    updates: dict[str, Any] = {}
    for key in ("base_url", "rest_username", "ssh_host", "ssh_user", "default_owner"):
        if key in body:
            updates[key] = str(body.get(key) or "").strip()
    if "rest_password" in body:
        password = str(body.get("rest_password") or "").strip()
        if password and password != "***":
            updates["rest_password"] = password
        else:
            updates["rest_password"] = current.get("rest_password") or ""
    if "rest_verify_ssl" in body:
        updates["rest_verify_ssl"] = bool(body.get("rest_verify_ssl"))
    if "ssh_port" in body:
        try:
            updates["ssh_port"] = int(body.get("ssh_port") or 29418)
        except (TypeError, ValueError):
            updates["ssh_port"] = 29418
    if "ssh_identity_file" in body:
        updates["ssh_identity_file"] = str(body.get("ssh_identity_file") or "").strip()
    if "cache_ttl" in body:
        try:
            updates["cache_ttl"] = int(body.get("cache_ttl") or 600)
        except (TypeError, ValueError):
            updates["cache_ttl"] = 600
    if "department_defaults" in body and isinstance(body.get("department_defaults"), dict):
        updates["department_defaults"] = body["department_defaults"]
    if "department_profiles" in body and isinstance(body.get("department_profiles"), list):
        updates["department_profiles"] = body["department_profiles"]
    if "personal_profiles" in body and isinstance(body.get("personal_profiles"), list):
        updates["personal_profiles"] = body["personal_profiles"]
    if "chart_date_ranges" in body and isinstance(body.get("chart_date_ranges"), dict):
        updates["chart_date_ranges"] = body["chart_date_ranges"]
    merged = {**current, **updates}
    if not manager.save_gerrit_dashboard_config(merged):
        return JSONResponse(status_code=500, content={"success": False, "error": "failed to save Gerrit dashboard config"})
    _STATS_CACHE.clear()
    return {"success": True, "data": _public_config(_dashboard_config_for_request(request), manager=manager)}


@router.post("/personal-profiles")
async def create_gerrit_personal_profile(request: Request):
    body = await request.json()
    name = str(body.get("name") or "").strip()
    owner = str(body.get("owner") or "").strip()
    profile_id = str(body.get("id") or "").strip()
    department_id = str(body.get("department_id") or "").strip()
    department_name = str(body.get("department") or "").strip()
    manager = _config_for_request(request)
    current_cfg = manager.get_gerrit_dashboard_config()
    if department_id:
        current_cfg = _ensure_gerrit_department_profile(current_cfg, department_id, department_name or department_id)
    try:
        dashboard_cfg = add_gerrit_personal_profile(
            current_cfg,
            name,
            owner,
            profile_id,
            department_id=department_id,
        )
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"success": False, "error": str(exc)})
    if not manager.save_gerrit_dashboard_config(dashboard_cfg):
        return JSONResponse(status_code=500, content={"success": False, "error": "failed to save Gerrit personal profile"})
    _STATS_CACHE.clear()
    return {"success": True, "data": {"dashboard": _public_config(dashboard_cfg, manager=manager), "profile": dashboard_cfg["personal_profiles"][-1]}}


@router.post("/department-profiles")
async def create_gerrit_department_profile(request: Request):
    body = await request.json()
    name = str(body.get("name") or "").strip()
    profile_id = str(body.get("id") or "").strip()
    raw_owners = body.get("owners") or []
    if isinstance(raw_owners, str):
        raw_owners = [item.strip() for item in raw_owners.replace(";", ",").split(",")]
    manager = _config_for_request(request)
    try:
        dashboard_cfg = add_gerrit_department_profile(
            manager.get_gerrit_dashboard_config(),
            name,
            profile_id,
            owners=raw_owners,
        )
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"success": False, "error": str(exc)})
    if not manager.save_gerrit_dashboard_config(dashboard_cfg):
        return JSONResponse(status_code=500, content={"success": False, "error": "failed to save Gerrit department profile"})
    _STATS_CACHE.clear()
    return {"success": True, "data": {"dashboard": _public_config(dashboard_cfg, manager=manager), "profile": dashboard_cfg["department_profiles"][-1]}}


@router.post("/department-profiles/{profile_id}/owners")
async def add_gerrit_department_owner(profile_id: str, request: Request):
    body = await request.json()
    owner = str(body.get("owner") or "").strip()
    manager = _config_for_request(request)
    try:
        dashboard_cfg = assign_owner_to_gerrit_department(manager.get_gerrit_dashboard_config(), profile_id, owner)
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"success": False, "error": str(exc)})
    if not manager.save_gerrit_dashboard_config(dashboard_cfg):
        return JSONResponse(status_code=500, content={"success": False, "error": "failed to save Gerrit department owner"})
    _STATS_CACHE.clear()
    profile = select_gerrit_department_profile(dashboard_cfg, profile_id)
    return {"success": True, "data": {"dashboard": _public_config(dashboard_cfg, manager=manager), "profile": profile}}


@router.delete("/department-profiles/{profile_id}/owners")
async def delete_gerrit_department_owner(profile_id: str, request: Request):
    body = await request.json()
    owner = str(body.get("owner") or "").strip()
    manager = _config_for_request(request)
    try:
        dashboard_cfg = remove_owner_from_gerrit_department(manager.get_gerrit_dashboard_config(), profile_id, owner)
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"success": False, "error": str(exc)})
    if not manager.save_gerrit_dashboard_config(dashboard_cfg):
        return JSONResponse(status_code=500, content={"success": False, "error": "failed to remove Gerrit department owner"})
    _STATS_CACHE.clear()
    profile = select_gerrit_department_profile(dashboard_cfg, profile_id)
    return {"success": True, "data": {"dashboard": _public_config(dashboard_cfg, manager=manager), "profile": profile}}


@router.post("/sync-redmine-members")
async def sync_gerrit_redmine_members(request: Request):
    manager = _config_for_request(request)
    dashboard_cfg = sync_gerrit_members_from_redmine_users(
        manager.get_gerrit_dashboard_config(),
        _redmine_users_for_request(request),
    )
    if not manager.save_gerrit_dashboard_config(dashboard_cfg):
        return JSONResponse(status_code=500, content={"success": False, "error": "failed to sync Redmine members to Gerrit dashboard"})
    _STATS_CACHE.clear()
    return {"success": True, "data": _public_config(_dashboard_config_for_request(request), manager=manager)}


@router.get("/changes")
async def list_gerrit_changes(request: Request, profile_id: str = Query(""), query: str = Query("")):
    cfg = _dashboard_config_for_request(request)
    profile = _select_profile(cfg, profile_id)
    effective_query = (query or profile.get("query") or "status:open").strip()
    if "limit:" not in effective_query:
        effective_query = f"{effective_query} limit:{cfg['query_limit']}"
    if not cfg.get("base_url") and not cfg.get("ssh_host"):
        return {
            "success": True,
            "data": {
                "configured": False,
                "config": cfg,
                "profile": profile,
                "query": effective_query,
                "items": [],
                "message": "Gerrit 看板不需要服务端插件；配置 gerrit_dashboard.base_url 可走 REST，或配置 ssh_host/ssh_user 走 SSH。",
            },
        }
    result = await _query_gerrit_dual_mode(cfg, effective_query, max_changes=_extract_query_limit(effective_query) or cfg["query_limit"])
    return {"success": True, "data": {**result, "configured": True, "config": _public_config(cfg, manager=_config_for_request(request)), "profile": profile, "query": effective_query}}


@router.get("/changes-by-date")
async def list_gerrit_changes_by_date(
    request: Request,
    owners: str = Query(""),
    start: str = Query(""),
    end: str = Query(""),
    scope: str = Query(""),
    profile_id: str = Query(""),
    limit: int = Query(500, ge=1, le=2000),
):
    cfg = _dashboard_config_for_request(request)
    owner_list = [item.strip() for item in str(owners or "").split(",") if item.strip()]
    if not owner_list:
        return {"success": False, "error": "owners is required"}
    defaults = cfg.get("defaults") or {}
    if scope == "department":
        profile = select_gerrit_department_profile(cfg, profile_id)
    else:
        profile = select_gerrit_personal_profile(cfg, profile_id, owner_list[0] if len(owner_list) == 1 else "")
    query_limit = _effective_history_limit(profile, defaults)
    page_size = int(profile.get("query_page_size") or defaults.get("query_page_size") or 500)
    if not cfg.get("base_url") and not cfg.get("ssh_host"):
        return {
            "success": True,
            "data": {
                "configured": False,
                "profile": profile,
                "items": [],
                "source": "",
                "error": "Gerrit REST/SSH 未配置",
            },
        }

    raw_items: list[dict[str, Any]] = []
    sources: list[str] = []
    errors: list[str] = []
    rest_errors: list[str] = []
    for owner_text in owner_list:
        query = f"owner:{owner_text} status:any"
        result = await _query_gerrit_dual_mode(cfg, query, max_changes=query_limit, page_size=page_size)
        raw_items.extend(result.get("items") or [])
        if result.get("source"):
            sources.append(str(result.get("source")))
        if result.get("error"):
            errors.append(f"{owner_text}: {result.get('error')}")
        if result.get("rest_error"):
            rest_errors.append(f"{owner_text}: {result.get('rest_error')}")
    items = filter_gerrit_changes_by_created_date(raw_items, start, end, limit=limit)
    return {
        "success": True,
        "data": {
            "configured": True,
            "profile": profile,
            "owners": owner_list,
            "start": start,
            "end": end,
            "items": items,
            "source": ",".join(dict.fromkeys(sources)),
            "error": "; ".join(errors),
            "rest_error": "; ".join(rest_errors),
        },
    }


@router.get("/statistics/personal")
async def get_gerrit_personal_statistics(
    request: Request,
    profile_id: str = Query(""),
    owner: str = Query(""),
    refresh: bool = Query(False),
):
    cfg = _dashboard_config_for_request(request)
    profile = select_gerrit_personal_profile(cfg, profile_id, owner)
    owner_text = str(owner or profile.get("owner") or cfg.get("default_owner") or "").strip()
    if not owner_text:
        return {"success": False, "error": "owner is required"}
    list_limit = int(profile.get("list_limit") or cfg["defaults"]["list_limit"])
    query_limit = _effective_history_limit(profile, cfg["defaults"])
    page_size = int(profile.get("query_page_size") or cfg["defaults"].get("query_page_size") or 500)
    cache_key = f"{_request_user_id(request)}:personal:{owner_text}:{list_limit}:{query_limit}:{page_size}"
    cached = _get_cache(cache_key, cfg["cache_ttl"], refresh)
    if cached is not None:
        return {"success": True, "data": {**cached, "cache_hit": True}}

    query = f"owner:{owner_text} status:any"
    result = await _query_gerrit_dual_mode(cfg, query, max_changes=query_limit, page_size=page_size)
    stats = summarize_gerrit_changes(result.get("items") or [], list_limit=list_limit)
    data = {
        **stats,
        "owner": owner_text,
        "profile": profile,
        "available_profiles": cfg.get("personal_profiles") or [],
        "query": query,
        "source": result.get("source") or "",
        "error": result.get("error") or "",
        "rest_error": result.get("rest_error") or "",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "cache_hit": False,
    }
    if not data.get("error"):
        _set_cache(cache_key, data)
    return {"success": True, "data": data}


@router.get("/statistics/department")
async def get_gerrit_department_statistics(
    request: Request,
    profile_id: str = Query(""),
    refresh: bool = Query(False),
):
    cfg = _dashboard_config_for_request(request)
    profile = select_gerrit_department_profile(cfg, profile_id)
    owners = _owners_for_department_profile(cfg, profile)
    if not owners:
        return {"success": True, "data": {
            "summary": {"total_count": 0, "merged_count": 0, "open_count": 0, "abandoned_count": 0, "pending_review_count": 0},
            "trends": {"daily": [], "weekly": [], "monthly": [], "yearly": []},
            "users": [],
            "profile": profile,
            "available_profiles": cfg.get("department_profiles") or [],
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "cache_hit": False,
        }}
    list_limit = int(profile.get("list_limit") or cfg["defaults"]["list_limit"])
    query_limit = _effective_history_limit(profile, cfg["defaults"])
    page_size = int(profile.get("query_page_size") or cfg["defaults"].get("query_page_size") or 500)
    cache_key = f"{_request_user_id(request)}:department:{profile.get('id')}:{','.join(owners)}:{list_limit}:{query_limit}:{page_size}"
    cached = _get_cache(cache_key, cfg["cache_ttl"], refresh)
    if cached is not None:
        return {"success": True, "data": {**cached, "cache_hit": True}}

    semaphore = asyncio.Semaphore(4)

    # owner 邮箱 → 中文姓名映射：redmine_user_map 优先，personal_profiles.name 次之，
    # 最后回退邮箱前缀。供部门成员表显示中文姓名。
    owner_names: dict[str, str] = {}
    try:
        for entry in _redmine_users_for_request(request):
            email = str(entry.get("email") or "").strip().lower()
            name = str(entry.get("name") or "").strip()
            if email and name:
                owner_names[email] = name
    except Exception:
        pass
    for p in cfg.get("personal_profiles") or []:
        email = str(p.get("owner") or "").strip().lower()
        name = str(p.get("name") or "").strip()
        if email and name and email not in owner_names:
            owner_names[email] = name

    def _display_name(owner_text: str) -> str:
        key = str(owner_text or "").strip().lower()
        return owner_names.get(key) or str(owner_text or "").split("@")[0] or owner_text or "-"

    async def _owner_stats(owner_text: str) -> dict[str, Any]:
        async with semaphore:
            query = f"owner:{owner_text} status:any"
            result = await _query_gerrit_dual_mode(cfg, query, max_changes=query_limit, page_size=page_size)
            stats = summarize_gerrit_changes(result.get("items") or [], list_limit=list_limit)
            return {
                "owner": owner_text,
                "name": _display_name(owner_text),
                "summary": stats["summary"],
                "trends": stats["trends"],
                "lists": stats["lists"],
                "query": query,
                "source": result.get("source") or "",
                "error": result.get("error") or "",
                "rest_error": result.get("rest_error") or "",
            }

    users = await asyncio.gather(*[_owner_stats(owner_text) for owner_text in owners])
    merged = summarize_gerrit_department_results(users)
    data = {
        **merged,
        "profile": profile,
        "available_profiles": cfg.get("department_profiles") or [],
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "cache_hit": False,
    }
    if not any(user.get("error") for user in users):
        _set_cache(cache_key, data)
    return {"success": True, "data": data}


def _public_config(cfg: dict[str, Any], manager=None) -> dict[str, Any]:
    public = dict(cfg or {})
    if public.get("rest_password"):
        public["rest_password"] = "***"
    try:
        public["redmine_departments"] = _redmine_department_options(manager=manager)
    except Exception:
        public["redmine_departments"] = []
    return public


def _redmine_department_options(manager=None) -> list[dict[str, str]]:
    rows: dict[str, str] = {}
    cfg = manager.get_gerrit_dashboard_config() if manager is not None else {}
    for profile in (cfg.get("department_profiles") or []):
        profile_id = str(profile.get("id") or "").strip()
        if not profile_id or profile_id == "all":
            continue
        rows.setdefault(profile_id, str(profile.get("name") or profile_id).strip() or profile_id)
    return [
        {"id": profile_id, "name": name}
        for profile_id, name in sorted(rows.items(), key=lambda item: item[1])
    ]


def _ensure_gerrit_department_profile(cfg: dict[str, Any], department_id: str, department_name: str = "") -> dict[str, Any]:
    clean_id = str(department_id or "").strip()
    if not clean_id:
        return cfg
    for profile in cfg.get("department_profiles") or []:
        if profile.get("id") == clean_id:
            return cfg
    return add_gerrit_department_profile(cfg, department_name or clean_id, profile_id=clean_id)


def _get_cache(cache_key: str, ttl: int, refresh: bool) -> dict[str, Any] | None:
    if refresh or ttl <= 0:
        return None
    cached = _STATS_CACHE.get(cache_key)
    now = datetime.now().timestamp()
    if cached and now - cached.get("cached_at_ts", 0) < ttl:
        return cached.get("data")
    return None


def _set_cache(cache_key: str, data: dict[str, Any]) -> None:
    _STATS_CACHE.clear()
    _STATS_CACHE[cache_key] = {"cached_at_ts": datetime.now().timestamp(), "data": data}


@page_router.get("/gerrit-dashboard", response_class=HTMLResponse)
async def gerrit_dashboard_page():
    page_path = Path(__file__).with_name("ui") / "page.html"
    return HTMLResponse(page_path.read_text(encoding="utf-8"))
