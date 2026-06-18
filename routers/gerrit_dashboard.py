"""Gerrit dashboard APIs and page."""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime
from typing import Any
from urllib.parse import urlencode

import aiohttp

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from core.config import config_manager
from core.gerrit_dashboard_config import (
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
    MAX_QUERY_PAGE_SIZE,
)
from features.redmine.repository import load_redmine_user_map


router = APIRouter(prefix="/api/gerrit-dashboard")
page_router = APIRouter()
_STATS_CACHE: dict[str, Any] = {}


@router.get("/config")
async def get_gerrit_dashboard_config():
    return {"success": True, "data": _public_config(config_manager.get_gerrit_dashboard_config())}


@router.post("/config")
async def update_gerrit_dashboard_config(request: Request):
    body = await request.json()
    current = config_manager.get_gerrit_dashboard_config()
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
    if not config_manager.save_gerrit_dashboard_config(merged):
        return JSONResponse(status_code=500, content={"success": False, "error": "failed to save Gerrit dashboard config"})
    _STATS_CACHE.clear()
    return {"success": True, "data": _public_config(config_manager.get_gerrit_dashboard_config())}


@router.post("/personal-profiles")
async def create_gerrit_personal_profile(request: Request):
    body = await request.json()
    name = str(body.get("name") or "").strip()
    owner = str(body.get("owner") or body.get("email") or "").strip()
    profile_id = str(body.get("id") or body.get("profile_id") or "").strip()
    department_id = str(body.get("department_id") or body.get("departmentId") or "").strip()
    department_name = str(body.get("department") or body.get("department_name") or body.get("departmentName") or "").strip()
    current_cfg = config_manager.get_gerrit_dashboard_config()
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
    if not config_manager.save_gerrit_dashboard_config(dashboard_cfg):
        return JSONResponse(status_code=500, content={"success": False, "error": "failed to save Gerrit personal profile"})
    _STATS_CACHE.clear()
    return {"success": True, "data": {"dashboard": _public_config(dashboard_cfg), "profile": dashboard_cfg["personal_profiles"][-1]}}


@router.post("/department-profiles")
async def create_gerrit_department_profile(request: Request):
    body = await request.json()
    name = str(body.get("name") or "").strip()
    profile_id = str(body.get("id") or body.get("profile_id") or "").strip()
    raw_owners = body.get("owners") or []
    if isinstance(raw_owners, str):
        raw_owners = [item.strip() for item in raw_owners.replace(";", ",").split(",")]
    try:
        dashboard_cfg = add_gerrit_department_profile(
            config_manager.get_gerrit_dashboard_config(),
            name,
            profile_id,
            owners=raw_owners,
        )
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"success": False, "error": str(exc)})
    if not config_manager.save_gerrit_dashboard_config(dashboard_cfg):
        return JSONResponse(status_code=500, content={"success": False, "error": "failed to save Gerrit department profile"})
    _STATS_CACHE.clear()
    return {"success": True, "data": {"dashboard": _public_config(dashboard_cfg), "profile": dashboard_cfg["department_profiles"][-1]}}


@router.post("/department-profiles/{profile_id}/owners")
async def add_gerrit_department_owner(profile_id: str, request: Request):
    body = await request.json()
    owner = str(body.get("owner") or body.get("email") or "").strip()
    try:
        dashboard_cfg = assign_owner_to_gerrit_department(config_manager.get_gerrit_dashboard_config(), profile_id, owner)
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"success": False, "error": str(exc)})
    if not config_manager.save_gerrit_dashboard_config(dashboard_cfg):
        return JSONResponse(status_code=500, content={"success": False, "error": "failed to save Gerrit department owner"})
    _STATS_CACHE.clear()
    profile = select_gerrit_department_profile(dashboard_cfg, profile_id)
    return {"success": True, "data": {"dashboard": _public_config(dashboard_cfg), "profile": profile}}


@router.delete("/department-profiles/{profile_id}/owners")
async def delete_gerrit_department_owner(profile_id: str, request: Request):
    body = await request.json()
    owner = str(body.get("owner") or body.get("email") or "").strip()
    try:
        dashboard_cfg = remove_owner_from_gerrit_department(config_manager.get_gerrit_dashboard_config(), profile_id, owner)
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"success": False, "error": str(exc)})
    if not config_manager.save_gerrit_dashboard_config(dashboard_cfg):
        return JSONResponse(status_code=500, content={"success": False, "error": "failed to remove Gerrit department owner"})
    _STATS_CACHE.clear()
    profile = select_gerrit_department_profile(dashboard_cfg, profile_id)
    return {"success": True, "data": {"dashboard": _public_config(dashboard_cfg), "profile": profile}}


@router.post("/sync-redmine-members")
async def sync_gerrit_redmine_members():
    dashboard_cfg = sync_gerrit_members_from_redmine_users(
        config_manager.get_gerrit_dashboard_config(),
        load_redmine_user_map(),
    )
    if not config_manager.save_gerrit_dashboard_config(dashboard_cfg):
        return JSONResponse(status_code=500, content={"success": False, "error": "failed to sync Redmine members to Gerrit dashboard"})
    _STATS_CACHE.clear()
    return {"success": True, "data": _public_config(config_manager.get_gerrit_dashboard_config())}


@router.get("/changes")
async def list_gerrit_changes(profile_id: str = Query(""), query: str = Query("")):
    cfg = config_manager.get_gerrit_dashboard_config()
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
    return {"success": True, "data": {**result, "configured": True, "config": _public_config(cfg), "profile": profile, "query": effective_query}}


@router.get("/changes-by-date")
async def list_gerrit_changes_by_date(
    owners: str = Query(""),
    start: str = Query(""),
    end: str = Query(""),
    scope: str = Query(""),
    profile_id: str = Query(""),
    limit: int = Query(500, ge=1, le=2000),
):
    cfg = config_manager.get_gerrit_dashboard_config()
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
    profile_id: str = Query(""),
    owner: str = Query(""),
    refresh: bool = Query(False),
):
    cfg = config_manager.get_gerrit_dashboard_config()
    profile = select_gerrit_personal_profile(cfg, profile_id, owner)
    owner_text = str(owner or profile.get("owner") or cfg.get("default_owner") or "").strip()
    if not owner_text:
        return {"success": False, "error": "owner is required"}
    list_limit = int(profile.get("list_limit") or cfg["defaults"]["list_limit"])
    query_limit = _effective_history_limit(profile, cfg["defaults"])
    page_size = int(profile.get("query_page_size") or cfg["defaults"].get("query_page_size") or 500)
    cache_key = f"personal:{owner_text}:{list_limit}:{query_limit}:{page_size}"
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
    profile_id: str = Query(""),
    refresh: bool = Query(False),
):
    cfg = config_manager.get_gerrit_dashboard_config()
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
    cache_key = f"department:{profile.get('id')}:{','.join(owners)}:{list_limit}:{query_limit}:{page_size}"
    cached = _get_cache(cache_key, cfg["cache_ttl"], refresh)
    if cached is not None:
        return {"success": True, "data": {**cached, "cache_hit": True}}

    semaphore = asyncio.Semaphore(4)

    # owner 邮箱 → 中文姓名映射：redmine_user_map 优先，personal_profiles.name 次之，
    # 最后回退邮箱前缀。供部门成员表显示中文姓名。
    owner_names: dict[str, str] = {}
    try:
        for entry in load_redmine_user_map() or []:
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


def _select_profile(cfg: dict[str, Any], profile_id: str) -> dict[str, Any]:
    profiles = cfg.get("dashboard_profiles") or []
    for profile in profiles:
        if profile.get("id") == profile_id:
            return profile
    return profiles[0] if profiles else {"id": "open", "name": "打开的变更", "query": "status:open"}


def _owners_for_department_profile(cfg: dict[str, Any], profile: dict[str, Any]) -> list[str]:
    owners = [str(owner or "").strip() for owner in profile.get("owners") or [] if str(owner or "").strip()]
    if profile.get("id") == "all":
        for department in cfg.get("department_profiles") or []:
            if department.get("id") == "all":
                continue
            owners.extend(str(owner or "").strip() for owner in department.get("owners") or [] if str(owner or "").strip())
    return list(dict.fromkeys(owners))


async def _query_gerrit_via_ssh(cfg: dict[str, Any], query: str, max_changes: int | None = None, page_size: int = 500) -> dict[str, Any]:
    page_size = max(1, min(int(page_size or 500), MAX_QUERY_PAGE_SIZE))
    max_total = None if max_changes is None else max(1, int(max_changes))
    start = 0
    all_items: list[dict[str, Any]] = []
    last_stats: dict[str, Any] = {}
    while True:
        remaining = page_size if max_total is None else min(page_size, max_total - len(all_items))
        if remaining <= 0:
            break
        result = await _query_gerrit_via_ssh_once(cfg, _query_for_ssh(query, remaining, start=start))
        if result.get("error"):
            return {**result, "items": all_items, "stats": result.get("stats") or last_stats}
        items = result.get("items") or []
        stats = result.get("stats") or {}
        all_items.extend(items)
        last_stats = stats
        if not items or not stats.get("moreChanges"):
            break
        start += len(items)
    return {"items": all_items, "stats": {**last_stats, "rowCount": len(all_items)}, "error": "", "source": "ssh"}


async def _query_gerrit_via_ssh_once(cfg: dict[str, Any], query: str) -> dict[str, Any]:
    target = cfg["ssh_host"]
    if cfg.get("ssh_user"):
        target = f"{cfg['ssh_user']}@{target}"
    cmd = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
        "-p",
        str(cfg["ssh_port"]),
    ]
    identity_file = str(cfg.get("ssh_identity_file") or "").strip()
    if identity_file:
        cmd.extend(["-o", "IdentitiesOnly=yes", "-i", os.path.expanduser(identity_file)])
    cmd.extend([
        target,
        "gerrit",
        "query",
        "--format=JSON",
        "--submit-records",
        query,
    ])
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
    except asyncio.TimeoutError:
        proc.kill()
        stdout, stderr = await proc.communicate()
        return {"items": [], "stats": {}, "error": "Gerrit SSH query timed out", "source": "ssh"}
    if proc.returncode != 0:
        return {"items": [], "stats": {}, "error": stderr.decode("utf-8", errors="ignore").strip()}
    items: list[dict[str, Any]] = []
    stats: dict[str, Any] = {}
    error = ""
    for line in stdout.decode("utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("type") == "stats":
            stats = obj
        elif obj.get("type") == "error":
            error = str(obj.get("message") or "Gerrit query failed")
        else:
            items.append(obj)
    return {"items": items, "stats": stats, "error": error, "source": "ssh"}


async def _query_gerrit_dual_mode(
    cfg: dict[str, Any],
    query: str,
    max_changes: int | None = None,
    page_size: int | None = None,
) -> dict[str, Any]:
    rest_error = ""
    effective_page_size = int(page_size or cfg.get("query_page_size") or cfg.get("defaults", {}).get("query_page_size") or 500)
    if cfg.get("base_url"):
        result = await _query_gerrit_via_rest(cfg, query, max_changes=max_changes, page_size=effective_page_size)
        if not result.get("error"):
            return result
        rest_error = result.get("error") or ""
    if cfg.get("ssh_host"):
        result = await _query_gerrit_via_ssh(cfg, query, max_changes=max_changes, page_size=effective_page_size)
        if rest_error:
            result["rest_error"] = rest_error
        return result
    if rest_error and not cfg.get("ssh_host"):
        rest_error = f"{rest_error}; 未配置 SSH fallback，请配置 REST 账号/HTTP Password 或 ssh_host/ssh_user"
    return {"items": [], "stats": {}, "error": rest_error or "Gerrit REST/SSH 未配置", "rest_error": rest_error, "source": ""}


async def _query_gerrit_via_rest(
    cfg: dict[str, Any],
    query: str,
    max_changes: int | None = None,
    page_size: int = 500,
) -> dict[str, Any]:
    base_url = str(cfg.get("base_url") or "").rstrip("/")
    max_total = None if max_changes is None else max(1, int(max_changes))
    page_size = max(1, min(int(page_size or 500), MAX_QUERY_PAGE_SIZE))
    api_prefix = "/a" if cfg.get("rest_username") and cfg.get("rest_password") else ""
    auth = None
    if api_prefix:
        auth = aiohttp.BasicAuth(str(cfg["rest_username"]), str(cfg["rest_password"]))
    timeout = aiohttp.ClientTimeout(total=60)
    connector = aiohttp.TCPConnector(ssl=bool(cfg.get("rest_verify_ssl", False)))
    headers = {"Accept": "application/json"}
    items: list[dict[str, Any]] = []
    start = 0
    try:
        async with aiohttp.ClientSession(timeout=timeout, connector=connector, auth=auth, headers=headers) as session:
            while max_total is None or len(items) < max_total:
                remaining = page_size if max_total is None else min(page_size, max_total - len(items))
                if remaining <= 0:
                    break
                params = [
                    ("q", _query_without_limit(query)),
                    ("n", str(remaining)),
                    ("S", str(start)),
                    ("o", "DETAILED_ACCOUNTS"),
                    ("o", "LABELS"),
                    ("o", "SUBMITTABLE"),
                ]
                url = f"{base_url}{api_prefix}/changes/?{urlencode(params)}"
                async with session.get(url, allow_redirects=True) as response:
                    text = await response.text()
                    if response.status >= 400:
                        return {"items": [], "stats": {}, "error": f"REST {response.status}: {text[:300]}", "source": "rest"}
                    rows = _decode_gerrit_rest_json(text)
                if not isinstance(rows, list):
                    return {"items": [], "stats": {}, "error": "REST response is not a list", "source": "rest"}
                items.extend(rows)
                if not rows or not rows[-1].get("_more_changes"):
                    break
                start += len(rows)
    except Exception as exc:
        return {"items": [], "stats": {}, "error": str(exc), "source": "rest"}
    return {"items": items, "stats": {"rowCount": len(items)}, "error": "", "source": "rest"}


def _decode_gerrit_rest_json(text: str) -> Any:
    clean = text.lstrip()
    if clean.startswith(")]}'"):
        clean = clean.split("\n", 1)[1] if "\n" in clean else ""
    return json.loads(clean or "[]")


def _query_without_limit(query: str) -> str:
    return " ".join(part for part in str(query or "").split() if not part.lower().startswith("limit:")).strip()


def _query_with_limit(query: str, limit: int) -> str:
    clean = _query_without_limit(query)
    return f"{clean} limit:{max(1, min(int(limit or 100), 5000))}".strip()


def _query_for_ssh(query: str, limit: int, start: int = 0) -> str:
    parts = [
        part for part in str(query or "").split()
        if part.lower() != "status:any"
    ]
    clean = _query_with_limit(" ".join(parts), limit)
    if start > 0:
        clean = f"{clean} --start {int(start)}"
    return clean


def _effective_history_limit(profile: dict[str, Any], defaults: dict[str, Any]) -> int | None:
    raw = profile.get("max_history_changes")
    if raw in (None, ""):
        raw = defaults.get("max_history_changes")
    try:
        parsed = int(raw)
    except (TypeError, ValueError):
        parsed = int(profile.get("query_limit") or defaults.get("query_limit") or 500)
    if parsed <= 0:
        return None
    return parsed


def _extract_query_limit(query: str) -> int | None:
    for part in str(query or "").split():
        if part.lower().startswith("limit:"):
            try:
                return max(1, min(int(part.split(":", 1)[1]), 5000))
            except (TypeError, ValueError):
                return None
    return None


def _public_config(cfg: dict[str, Any]) -> dict[str, Any]:
    public = dict(cfg or {})
    if public.get("rest_password"):
        public["rest_password"] = "***"
    try:
        public["redmine_departments"] = _redmine_department_options()
    except Exception:
        public["redmine_departments"] = []
    return public


def _redmine_department_options() -> list[dict[str, str]]:
    redmine_cfg = config_manager.get_redmine_dashboard_config()
    rows = []
    for profile in redmine_cfg.get("profiles") or []:
        profile_id = str(profile.get("id") or "").strip()
        if not profile_id or profile_id == "all":
            continue
        rows.append({"id": profile_id, "name": str(profile.get("name") or profile_id).strip()})
    return rows


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
    return HTMLResponse(
        """
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Gerrit 看板</title>
  <style>
    :root { color-scheme: dark; --bg:#0b0d12; --panel:#131720; --panel2:#191f2b; --border:#2b3342; --text:#e8edf7; --muted:#96a1b5; --primary:#3b82f6; --ok:#22c55e; --warn:#f59e0b; --bad:#ef4444; }
    * { box-sizing:border-box; }
    body { margin:0; font-family:system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; background:var(--bg); color:var(--text); }
    html { scrollbar-gutter:stable; overflow-y:scroll; scroll-padding-top:86px; }
    header { position:sticky; top:0; z-index:10; display:grid; grid-template-columns:max-content minmax(0, 1fr) max-content; align-items:center; gap:14px; padding:8px 16px; background:var(--panel); border-bottom:1px solid var(--border); box-shadow:0 8px 18px rgba(0,0,0,.22); }
    h1 { font-size:18px; margin:0; white-space:nowrap; }
    select,input { height:30px; background:var(--panel2); color:var(--text); border:1px solid var(--border); border-radius:5px; padding:0 8px; font-size:13px; }
    input { min-width:260px; }
    button { height:30px; border:0; border-radius:6px; padding:0 10px; color:white; background:var(--primary); font-weight:650; cursor:pointer; font-size:13px; }
    button.secondary { background:#30394a; }
    main { padding:16px; }
    .tabs { display:flex; gap:0; }
    .tab { padding:8px 14px; cursor:pointer; font-size:13px; font-weight:650; color:var(--muted); border-bottom:2px solid transparent; }
    .tab.active { color:var(--primary); border-bottom-color:var(--primary); }
    .btn-group { display:flex; gap:6px; flex-wrap:wrap; justify-content:flex-end; }
    .toolbar { display:flex; align-items:center; gap:8px; flex-wrap:wrap; margin-bottom:14px; }
    .select-with-add { position:relative; display:inline-flex; align-items:center; }
    .select-with-add select { padding-right:28px; min-width:180px; }
    .select-add-btn { position:absolute; right:2px; top:50%; transform:translateY(-50%); background:none; border:0; color:var(--muted); font-size:15px; padding:0 6px; line-height:1; cursor:pointer; height:24px; min-width:22px; }
    .select-add-btn:hover { color:var(--text); background:transparent; }
    .tab-content { display:none; }
    .tab-content.active { display:block; }
    .stats-grid { display:grid; grid-template-columns:repeat(auto-fit, minmax(160px, 1fr)); gap:12px; margin-bottom:16px; }
    .stat-card { background:var(--panel); border:1px solid var(--border); border-radius:8px; padding:14px; text-align:center; }
    .stat-card .value { font-size:28px; font-weight:750; color:var(--primary); }
    .stat-card .label { color:var(--muted); font-size:12px; margin-top:4px; }
    .stat-card.ok .value { color:var(--ok); }
    .stat-card.warn .value { color:var(--warn); }
    .stat-card.bad .value { color:var(--bad); }
    .clickable-stat { cursor:pointer; transition:transform .15s,box-shadow .15s; }
    .clickable-stat:hover { transform:translateY(-2px); box-shadow:0 4px 12px rgba(0,0,0,.3); }
    .dashboard-summary-header { display:flex; justify-content:space-between; align-items:center; margin-bottom:10px; gap:12px; flex-wrap:wrap; min-height:30px; }
    .dashboard-summary-title { margin:0; font-size:15px; line-height:30px; }
    .dashboard-summary-controls { display:flex; align-items:center; gap:8px; margin-left:auto; flex-wrap:wrap; min-height:30px; }
    .dashboard-summary-meta { font-size:12px; min-height:30px; display:flex; align-items:center; }
    .trend-grid { display:grid; grid-template-columns:repeat(auto-fit, minmax(380px, 1fr)); gap:12px; margin-bottom:18px; }
    .trend-panel { background:var(--panel); border:1px solid var(--border); border-radius:8px; padding:12px; min-height:180px; display:flex; flex-direction:column; }
    .trend-panel h2, .list-section h2 { margin:0 0 10px; font-size:14px; }
    .trend-panel h2 { flex-shrink:0; line-height:30px; }
    .trend-body { overflow-y:auto; max-height:330px; flex:1; }
    .trend-body::-webkit-scrollbar { width:6px; }
    .trend-body::-webkit-scrollbar-track { background:var(--panel2); border-radius:3px; }
    .trend-body::-webkit-scrollbar-thumb { background:var(--border); border-radius:3px; }
    .trend-body::-webkit-scrollbar-thumb:hover { background:var(--muted); }
    .bar-row { display:flex; align-items:center; gap:6px; margin:4px 0; font-size:12px; padding-right:8px; min-height:18px; }
    .bar-label { width:86px; color:var(--muted); white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
    .bar-track { flex:1; height:8px; background:#202838; border-radius:999px; overflow:hidden; }
    .bar-fill { height:100%; min-width:3px; background:var(--primary); border-radius:999px; }
    .bar-count { width:32px; text-align:right; color:var(--muted); }
    .list-section { margin-bottom:16px; }
    .list-section[id], .owner-detail[id] { scroll-margin-top:86px; }
    .owner-detail { margin-bottom:16px; }
    .owner-title { display:flex; justify-content:space-between; align-items:center; gap:10px; margin:0 0 8px; }
    .owner-title h2 { margin:0; font-size:15px; }
    table { width:100%; border-collapse:collapse; min-width:820px; }
    th,td { border-bottom:1px solid var(--border); padding:8px 10px; text-align:left; font-size:13px; vertical-align:top; }
    th { color:var(--muted); background:var(--panel2); }
    a { color:#7bb0ff; text-decoration:none; font-weight:700; }
    a.member-link:hover { text-decoration:underline; }
    .muted { color:var(--muted); font-size:13px; line-height:1.6; }
    .wrap { overflow:auto; border:1px solid var(--border); border-radius:8px; }
    .dept-table-wrap { overflow:auto; border:1px solid var(--border); border-radius:8px; margin-bottom:18px; }
    .dept-table { width:100%; border-collapse:collapse; min-width:760px; font-size:13px; }
    .dept-table th { text-align:left; color:var(--muted); font-weight:600; font-size:12px; padding:8px 10px; background:var(--panel2); border-bottom:1px solid var(--border); white-space:nowrap; }
    .dept-table td { padding:9px 10px; border-bottom:1px solid var(--border); white-space:nowrap; }
    .dept-table tr:last-child td { border-bottom:0; }
    .dept-table tbody tr:hover { background:#111927; }
    .dept-action-btn { width:48px; min-width:48px; max-width:48px; padding:0; text-align:center; overflow:hidden; white-space:nowrap; }
    .change-title { max-width:420px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
    .change-title a { margin-right:8px; }
    .trend-title-row { display:flex; align-items:center; justify-content:space-between; gap:8px; margin-bottom:10px; min-height:30px; }
    .trend-title-row h2 { margin:0; }
    .trend-start-btn { width:30px; min-width:30px; padding:0; background:#30394a; color:var(--muted); }
    .trend-start-btn:hover { color:var(--text); }
    .error { color:#fca5a5; }
    .status-line { min-height:26px; margin-bottom:10px; }
    .modal { display:none; position:fixed; inset:0; z-index:100; background:rgba(0,0,0,.68); align-items:center; justify-content:center; padding:18px; }
    .modal.show { display:flex; }
    .modal-content { width:min(720px, 100%); max-height:85vh; display:flex; flex-direction:column; background:var(--panel); border:1px solid var(--border); border-radius:8px; overflow:hidden; box-shadow:0 18px 40px rgba(0,0,0,.4); }
    .modal-header { display:flex; justify-content:space-between; align-items:center; padding:12px 16px; background:var(--panel2); border-bottom:1px solid var(--border); }
    .modal-header h2 { margin:0; font-size:15px; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
    .modal-body { padding:16px; display:grid; gap:12px; overflow:auto; min-height:0; }
    #trendDetailBody { max-height:calc(85vh - 62px); }
    .form-grid { display:grid; grid-template-columns:repeat(2, minmax(0, 1fr)); gap:12px; }
    .field label { display:block; color:var(--muted); font-size:12px; margin-bottom:5px; }
    .field input { width:100%; min-width:0; }
    .field.full { grid-column:1 / -1; }
    .modal-actions { display:flex; justify-content:flex-end; gap:8px; padding:0 16px 16px; }
    @media (max-width:800px) { header { grid-template-columns:1fr; align-items:flex-start; } input { min-width:180px; width:100%; } .toolbar { align-items:stretch; } }
  </style>
</head>
<body>
<header>
  <h1>Gerrit 看板</h1>
  <div class="tabs">
    <div class="tab active" data-tab="personal" onclick="switchTab('personal')">个人看板</div>
    <div class="tab" data-tab="department" onclick="switchTab('department')">部门看板</div>
    <div class="tab" data-tab="query" onclick="switchTab('query')">查询</div>
  </div>
  <div class="btn-group">
    <button class="secondary" onclick="refreshCurrentTab()">刷新</button>
    <button class="secondary" onclick="showSettings()" title="设置">⚙️</button>
  </div>
</header>
<main>
  <section id="tab-personal" class="tab-content active">
    <div id="personalStatus" class="muted status-line">加载中...</div>
    <div id="personalContent"></div>
  </section>
  <section id="tab-department" class="tab-content">
    <div id="departmentStatus" class="muted status-line">加载中...</div>
    <div id="departmentContent"></div>
  </section>
  <section id="tab-query" class="tab-content">
    <div class="toolbar">
      <select id="profile" onchange="loadChanges()"></select>
      <input id="query" placeholder="Gerrit query，例如 status:open owner:self">
      <button onclick="loadChanges()">查询</button>
    </div>
    <div id="status" class="muted status-line">加载中...</div>
    <div class="wrap"><table><thead><tr><th>变更</th><th>项目</th><th>分支</th><th>状态</th><th>Owner</th><th>更新时间</th></tr></thead><tbody id="rows"></tbody></table></div>
  </section>
</main>
<div id="settingsModal" class="modal">
  <div class="modal-content">
    <div class="modal-header">
      <h2>Gerrit 连接设置</h2>
      <button class="secondary" onclick="hideSettings()">关闭</button>
    </div>
    <div class="modal-body">
      <div class="form-grid">
        <div class="field full"><label>REST 地址</label><input id="settingBaseUrl" placeholder="https://10.10.10.29"></div>
        <div class="field"><label>REST 用户名</label><input id="settingRestUser" placeholder="Gerrit 用户名"></div>
        <div class="field"><label>REST HTTP Password</label><input id="settingRestPass" type="password" placeholder="留空则保留原密码"></div>
        <div class="field"><label>SSH Host</label><input id="settingSshHost" placeholder="10.10.10.29"></div>
        <div class="field"><label>SSH User</label><input id="settingSshUser" placeholder="SSH 用户名"></div>
        <div class="field"><label>SSH Port</label><input id="settingSshPort" type="number" placeholder="29418"></div>
        <div class="field full"><label>SSH 私钥路径</label><input id="settingSshIdentity" placeholder="/home/hcq/.ssh/id_ed25519_gerrit_dashboard"></div>
        <div class="field full"><label>默认 Owner</label><input id="settingDefaultOwner" placeholder="chaoqun.huang@rock-chips.com"></div>
        <div class="field"><label>单页查询数量（≤2000）</label><input id="settingQueryPageSize" type="number" min="1" max="2000" placeholder="500"></div>
        <div class="field"><label>最大历史提交数（0=不限）</label><input id="settingMaxHistory" type="number" min="0" placeholder="0 表示拉取全部历史"></div>
      </div>
      <div class="muted">REST 返回 401 时，需要填写 Gerrit HTTP Password；如果 REST 不方便登录，可以填写 SSH Host/User，后端会自动 fallback 到 SSH。</div>
    </div>
    <div class="modal-actions">
      <button class="secondary" onclick="hideSettings()">取消</button>
      <button onclick="saveSettings()">保存</button>
    </div>
  </div>
</div>
<div id="addPersonalModal" class="modal">
  <div class="modal-content">
    <div class="modal-header"><h2>添加成员</h2><button class="secondary" onclick="hideAddPersonalModal()">关闭</button></div>
    <div class="modal-body">
      <div class="form-grid">
        <div class="field"><label>显示名</label><input id="addPersonalName" placeholder="例如：张三"></div>
        <div class="field"><label>Owner 邮箱/账号</label><input id="addPersonalOwner" placeholder="name@rock-chips.com"></div>
        <div class="field full"><label>所属部门</label><div class="select-with-add" style="width:100%"><select id="addPersonalDepartment" style="width:100%;min-width:0"></select><button class="select-add-btn" type="button" onclick="showAddDepartmentModal('addPersonalDepartment')" title="添加部门">＋</button></div></div>
      </div>
    </div>
    <div class="modal-actions"><button class="secondary" onclick="hideAddPersonalModal()">取消</button><button onclick="savePersonalProfile()">保存</button></div>
  </div>
</div>
<div id="addDepartmentModal" class="modal">
  <div class="modal-content">
    <div class="modal-header"><h2>添加部门看板</h2><button class="secondary" onclick="hideAddDepartmentModal()">关闭</button></div>
    <div class="modal-body">
      <div class="form-grid">
        <div class="field"><label>部门名称</label><input id="addDepartmentName" placeholder="例如：系统一部"></div>
        <div class="field"><label>部门 ID</label><input id="addDepartmentId" placeholder="可选，例如 system-1"></div>
        <div class="field full"><label>成员 Owner</label><input id="addDepartmentOwners" placeholder="多个成员用逗号分隔"></div>
      </div>
    </div>
    <div class="modal-actions"><button class="secondary" onclick="hideAddDepartmentModal()">取消</button><button onclick="saveDepartmentProfile()">保存</button></div>
  </div>
</div>
<div id="addDepartmentOwnerModal" class="modal">
  <div class="modal-content">
    <div class="modal-header"><h2>添加部门成员</h2><button class="secondary" onclick="hideAddDepartmentOwnerModal()">关闭</button></div>
    <div class="modal-body"><div class="field"><label>Owner 邮箱/账号</label><input id="addDepartmentOwnerValue" placeholder="name@rock-chips.com"></div></div>
    <div class="modal-actions"><button class="secondary" onclick="hideAddDepartmentOwnerModal()">取消</button><button onclick="saveDepartmentOwner()">保存</button></div>
  </div>
</div>
<div id="trendStartModal" class="modal">
  <div class="modal-content">
    <div class="modal-header"><h2 id="trendStartModalTitle">设置统计日期范围</h2><button class="secondary" onclick="hideTrendStartModal()">关闭</button></div>
    <div class="modal-body">
      <div class="form-grid">
        <div class="field"><label>开始日期</label><input type="date" id="trendStartDateInput"></div>
        <div class="field"><label>结束日期</label><input type="date" id="trendEndDateInput"></div>
      </div>
    </div>
    <div class="modal-actions"><button class="secondary" onclick="clearTrendStartDate()">清空</button><button onclick="saveTrendStartDate()">保存并刷新</button></div>
  </div>
</div>
<div id="trendDetailModal" class="modal">
  <div class="modal-content" style="width:min(900px,100%)">
    <div class="modal-header"><h2 id="trendDetailTitle">提交明细</h2><button class="secondary" onclick="hideModal('trendDetailModal')">关闭</button></div>
    <div class="modal-body" id="trendDetailBody"><div class="muted">加载中…</div></div>
  </div>
</div>
<script>
let config = {dashboard_profiles: [], personal_profiles: [], department_profiles: [], default_owner: ''};
let currentTab = 'personal';
let currentPersonalProfileId = '';
let currentDepartmentProfileId = '';
let requestedOwner = '';
// 趋势明细点击上下文：当前看板作用的 owner（个人）或 owners（部门）
let trendOwner = '';
let trendOwners = [];
let trendScope = '';
let trendProfileId = '';
let pendingDepartmentTargetSelect = 'departmentProfile';
let pendingTrendChartKey = '';
function esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
async function api(url, options) {
  const r = await fetch(url, options || {});
  const data = await r.json();
  if (!data.success) throw new Error(data.error || '请求失败');
  return data.data || data;
}
async function init() {
  config = await api('/api/gerrit-dashboard/config');
  fillSelect('profile', config.dashboard_profiles || []);
  fillSelect('personalProfile', config.personal_profiles || []);
  fillSelect('departmentProfile', config.department_profiles || []);
  const first = (config.dashboard_profiles || [])[0] || {};
  document.getElementById('query').value = first.query || 'status:open';
  const person = (config.personal_profiles || [])[0] || {};
  const ownerInput = document.getElementById('owner');
  if (ownerInput) ownerInput.value = person.owner || config.default_owner || 'chaoqun.huang@rock-chips.com';
  await loadPersonal(false);
}
function showSettings() {
  document.getElementById('settingBaseUrl').value = config.base_url || 'https://10.10.10.29';
  document.getElementById('settingRestUser').value = config.rest_username || '';
  document.getElementById('settingRestPass').value = '';
  document.getElementById('settingSshHost').value = config.ssh_host || '';
  document.getElementById('settingSshUser').value = config.ssh_user || '';
  document.getElementById('settingSshPort').value = config.ssh_port || 29418;
  document.getElementById('settingSshIdentity').value = config.ssh_identity_file || '';
  document.getElementById('settingDefaultOwner').value = config.default_owner || '';
  document.getElementById('settingQueryPageSize').value = ((config.defaults || {}).query_page_size || 500);
  document.getElementById('settingMaxHistory').value = ((config.defaults || {}).max_history_changes || 0);
  showModal('settingsModal');
}
function hideSettings() {
  hideModal('settingsModal');
}
async function saveSettings() {
  const body = {
    base_url: document.getElementById('settingBaseUrl').value,
    rest_username: document.getElementById('settingRestUser').value,
    rest_password: document.getElementById('settingRestPass').value,
    ssh_host: document.getElementById('settingSshHost').value,
    ssh_user: document.getElementById('settingSshUser').value,
    ssh_port: document.getElementById('settingSshPort').value,
    ssh_identity_file: document.getElementById('settingSshIdentity').value,
    default_owner: document.getElementById('settingDefaultOwner').value,
    department_defaults: Object.assign({}, config.defaults || {}, {
      query_page_size: Number(document.getElementById('settingQueryPageSize').value || 500),
      max_history_changes: Number(document.getElementById('settingMaxHistory').value || 0)
    })
  };
  try {
    config = await api('/api/gerrit-dashboard/config', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
    hideSettings();
    await init();
  } catch (e) {
    notifyUser('保存设置失败', e.message, 'error');
  }
}
function fillSelect(id, items) {
  const select = document.getElementById(id);
  if (!select) return;
  select.innerHTML = (items || []).map(p => '<option value="' + esc(p.id) + '">' + esc(p.name || p.owner || p.id) + '</option>').join('');
}
function scrollToSection(id) {
  const el = document.getElementById(id);
  if (!el) return;
  const header = document.querySelector('header');
  const offset = (header ? header.getBoundingClientRect().height : 0) + 14;
  window.scrollTo({top: Math.max(0, el.getBoundingClientRect().top + window.pageYOffset - offset), behavior:'smooth'});
}
document.addEventListener('keydown', function(e) {
  if (e.key === 'Escape') document.querySelectorAll('.modal.show').forEach(function(m) { m.classList.remove('show'); });
});
document.addEventListener('click', function(e) {
  if (e.target && e.target.classList && e.target.classList.contains('modal')) e.target.classList.remove('show');
});
function showModal(id) {
  const el = document.getElementById(id);
  if (el) el.classList.add('show');
}
function hideModal(id) {
  const el = document.getElementById(id);
  if (el) el.classList.remove('show');
}
function notifyUser(title, message, level) {
  level = level || 'info';
  try {
    if (window.parent && window.parent !== window) {
      window.parent.postMessage({type:'gms-dashboard-notification', title:title, message:message, level:level}, '*');
    }
  } catch (_) {}
  var old = document.getElementById('gerrit-local-toast');
  if (old) old.remove();
  var toast = document.createElement('div');
  toast.id = 'gerrit-local-toast';
  toast.textContent = title + (message ? ': ' + message : '');
  toast.style.cssText = 'position:fixed;right:16px;bottom:16px;z-index:10000;max-width:min(460px,calc(100vw - 32px));padding:10px 12px;border-radius:6px;background:#111827;color:#f8fafc;border:1px solid #334155;box-shadow:0 8px 24px rgba(0,0,0,.28);font-size:12px;';
  document.body.appendChild(toast);
  setTimeout(function(){ if (toast.parentNode) toast.remove(); }, 3600);
}
function switchTab(tab) {
  currentTab = tab;
  document.querySelectorAll('.tab').forEach(x => x.classList.toggle('active', x.dataset.tab === tab));
  document.querySelectorAll('.tab-content').forEach(x => x.classList.toggle('active', x.id === 'tab-' + tab));
  if (tab === 'personal') loadPersonal(false);
  if (tab === 'department') loadDepartment(false);
  if (tab === 'query') loadChanges();
}
function viewMemberInPersonal(owner) {
  owner = String(owner || '').trim();
  if (!owner) return;
  requestedOwner = owner;
  currentPersonalProfileId = '';
  switchTab('personal');
}
// 把趋势粒度+标签（如 week "2026-W24"、month "2026-06"）转成 Gerrit after:/before: 日期范围（闭区间，before 用次日）
function utcDateText(date) {
  return date.toISOString().slice(0, 10);
}
function utcDate(year, monthIndex, day) {
  return new Date(Date.UTC(year, monthIndex, day));
}
function trendLabelToDateRange(granularity, label) {
  label = String(label || '');
  if (granularity === 'date') {
    var parts = label.split('-').map(function(v) { return parseInt(v, 10); });
    if (parts.length !== 3 || !parts[0] || !parts[1] || !parts[2]) return null;
    var d = utcDate(parts[0], parts[1] - 1, parts[2]);
    if (isNaN(d.getTime())) return null;
    return [label, utcDateText(new Date(d.getTime() + 86400000))];
  }
  if (granularity === 'month') {
    var mp = label.split('-').map(function(v) { return parseInt(v, 10); });
    if (mp.length !== 2 || !mp[0] || !mp[1]) return null;
    var m = utcDate(mp[0], mp[1] - 1, 1);
    if (isNaN(m.getTime())) return null;
    return [label + '-01', utcDateText(utcDate(mp[0], mp[1], 1))];
  }
  if (granularity === 'year') {
    var y = parseInt(label, 10);
    if (!y) return null;
    return [y + '-01-01', (y + 1) + '-01-01'];
  }
  if (granularity === 'week') {
    var match = /^(\d{4})-W(\d{2})$/.exec(label);
    if (!match) return null;
    var year = parseInt(match[1], 10), week = parseInt(match[2], 10);
    var jan4 = utcDate(year, 0, 4);
    var dow = (jan4.getUTCDay() + 6) % 7;
    var week1Monday = new Date(jan4.getTime() - dow * 86400000);
    var ws = new Date(week1Monday.getTime() + (week - 1) * 7 * 86400000);
    return [utcDateText(ws), utcDateText(new Date(ws.getTime() + 7 * 86400000))];
  }
  return null;
}
function displayTrendRange(range) {
  var parts = String(range[1] || '').split('-').map(function(v) { return parseInt(v, 10); });
  var end = parts.length === 3 && parts[0] && parts[1] && parts[2] ? utcDate(parts[0], parts[1] - 1, parts[2]) : null;
  var displayEnd = end ? utcDateText(new Date(end.getTime() - 86400000)) : range[1];
  return range[0] + ' 至 ' + displayEnd;
}
async function showGerritTrendDetail(granularity, label) {
  var owners = (trendOwners && trendOwners.length) ? trendOwners : (trendOwner ? [trendOwner] : []);
  if (!owners.length) { notifyUser('无法查看提交明细', '未获取到当前看板的 owner', 'warning'); return; }
  var range = trendLabelToDateRange(granularity, label);
  if (!range) { notifyUser('无法解析时段', label, 'warning'); return; }
  var modal = document.getElementById('trendDetailModal');
  var title = document.getElementById('trendDetailTitle');
  var body = document.getElementById('trendDetailBody');
  if (!modal || !title || !body) return;
  title.textContent = '提交明细：' + label + '（' + displayTrendRange(range) + '）';
  body.innerHTML = '<div class="muted">查询中…</div>';
  showModal('trendDetailModal');
  try {
    var params = new URLSearchParams({
      owners: owners.join(','),
      start: range[0],
      end: range[1],
      scope: trendScope || currentTab || '',
      profile_id: trendProfileId || (currentTab === 'department' ? currentDepartmentProfileId : currentPersonalProfileId) || ''
    });
    var data = await api('/api/gerrit-dashboard/changes-by-date?' + params.toString());
    var items = (data && data.items) || [];
    if (!items.length) { body.innerHTML = '<div class="muted">该时段无提交记录。</div>'; return; }
    body.innerHTML = '<div class="muted" style="margin-bottom:8px">共 ' + items.length + ' 条</div><div class="wrap"><table><thead><tr><th>变更</th><th>项目</th><th>分支</th><th>状态</th><th>Owner</th><th>更新时间</th></tr></thead><tbody>'
      + items.slice(0, 200).map(renderRow).join('') + '</tbody></table></div>';
  } catch (e) {
    body.innerHTML = '<span class="error">' + esc(e.message) + '</span>';
  }
}
async function refreshCurrentTab() {
  var btn = null;
  document.querySelectorAll('header .btn-group .secondary').forEach(function(b) {
    if (b.textContent.indexOf('刷新') >= 0) btn = b;
  });
  if (btn) { btn.disabled = true; btn.textContent = '⏳ 刷新中...'; }
  try {
    if (currentTab === 'personal') await loadPersonal(true);
    else if (currentTab === 'department') await loadDepartment(true);
    else await loadChanges();
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '刷新'; }
  }
}
function onPersonalProfileChange() {
  const select = document.getElementById('personalProfile');
  const id = select ? select.value : '';
  currentPersonalProfileId = id;
  const profile = (config.personal_profiles || []).find(x => x.id === id) || {};
  if (profile.owner) document.getElementById('owner').value = profile.owner;
  loadPersonal(true);
}
async function loadPersonal(refresh) {
  if (currentTab !== 'personal') return;
  const ownerInput = document.getElementById('owner');
  const owner = requestedOwner || (ownerInput ? ownerInput.value : '') || config.default_owner || '';
  const profileSelect = document.getElementById('personalProfile');
  const profileId = (profileSelect ? profileSelect.value : '') || currentPersonalProfileId || '';
  currentPersonalProfileId = profileId;
  document.getElementById('personalStatus').textContent = '统计中...';
  try {
    const data = await api('/api/gerrit-dashboard/statistics/personal?profile_id=' + encodeURIComponent(profileId) + '&owner=' + encodeURIComponent(owner) + '&refresh=' + (refresh ? 'true' : 'false'));
    document.getElementById('personalStatus').innerHTML = metaLine(data);
    document.getElementById('personalContent').innerHTML = renderDashboard(data);
    trendOwner = owner;
    trendOwners = owner ? [owner] : [];
    trendScope = 'personal';
    trendProfileId = profileId;
    requestedOwner = '';
  } catch (e) {
    requestedOwner = '';
    document.getElementById('personalStatus').innerHTML = '<span class="error">' + esc(e.message) + '</span>';
  }
}
async function loadDepartment(refresh) {
  if (currentTab !== 'department') return;
  const profileSelect = document.getElementById('departmentProfile');
  const profileId = (profileSelect ? profileSelect.value : '') || currentDepartmentProfileId || '';
  currentDepartmentProfileId = profileId;
  document.getElementById('departmentStatus').textContent = '统计中...';
  try {
    const data = await api('/api/gerrit-dashboard/statistics/department?profile_id=' + encodeURIComponent(profileId) + '&refresh=' + (refresh ? 'true' : 'false'));
    document.getElementById('departmentStatus').innerHTML = metaLine(data);
    document.getElementById('departmentContent').innerHTML = renderDepartmentDashboard(data);
    trendOwners = (data.users || []).map(function(u) { return u.owner; }).filter(Boolean);
    trendOwner = '';
    trendScope = 'department';
    trendProfileId = profileId;
  } catch (e) {
    document.getElementById('departmentStatus').innerHTML = '<span class="error">' + esc(e.message) + '</span>';
  }
}
async function loadChanges() {
  if (currentTab !== 'query') return;
  const profileId = document.getElementById('profile').value || '';
  const query = document.getElementById('query').value || '';
  document.getElementById('status').textContent = '查询中...';
  try {
    const data = await api('/api/gerrit-dashboard/changes?profile_id=' + encodeURIComponent(profileId) + '&query=' + encodeURIComponent(query));
    document.getElementById('status').innerHTML = data.error ? '<span class="error">' + esc(data.error) + '</span>' : esc((data.source || 'unknown') + ' / 查询: ' + data.query + '，结果 ' + (data.items || []).length + ' 条');
    document.getElementById('rows').innerHTML = (data.items || []).map(renderRow).join('') || '<tr><td colspan="6" class="muted">无记录</td></tr>';
  } catch (e) {
    document.getElementById('status').innerHTML = '<span class="error">' + esc(e.message) + '</span>';
  }
}
function metaLine(data) {
  const parts = [
    '来源: ' + (data.source || '-'),
    '生成: ' + (data.generated_at || '-'),
    data.cache_hit ? '缓存命中' : '',
    data.rest_error ? 'REST失败后回退: ' + data.rest_error : '',
    data.error ? '错误: ' + data.error : ''
  ].filter(Boolean);
  return esc(parts.join(' / '));
}
function renderSummaryHeader(title, controlsHtml, metaHtml) {
  return '<div class="dashboard-summary-header"><h2 class="dashboard-summary-title">' + esc(title) + '</h2><div class="dashboard-summary-controls">' + (controlsHtml || '') + '</div><div class="muted dashboard-summary-meta">' + (metaHtml || '') + '</div></div>';
}
function renderDashboard(data) {
  const isPersonal = currentTab === 'personal';
  const controls = isPersonal ? personalControlsHtml() : '';
  const title = isPersonal ? 'Gerrit 个人提交汇总' : 'Gerrit 提交汇总';
  const s = data.summary || {};
  return '<section class="list-section">'
    + renderSummaryHeader(title, controls, '来源: ' + esc(data.source || '-') + ' | 缓存: ' + (data.cache_hit ? '是' : '否'))
    + renderCards([
    {label:'历史提交', value:s.total_count || 0},
    {label:'已合并', value:s.merged_count || 0, className:'ok clickable-stat', onclick:"scrollToSection('sec-merged')"},
    {label:'未合并', value:s.open_count || 0, className:'warn clickable-stat', onclick:"scrollToSection('sec-open')"},
    {label:'待评审', value:s.pending_review_count || 0, className:'bad clickable-stat', onclick:"scrollToSection('sec-pending-review')"},
    {label:'已废弃', value:s.abandoned_count || 0, className:'clickable-stat', onclick:"scrollToSection('sec-abandoned')"}
  ]) + '</section><div class="trend-grid">'
    + renderTrend('每天提交', (data.trends || {}).daily || [], 'date', 'personal_daily')
    + renderTrend('每周提交', (data.trends || {}).weekly || [], 'week', 'personal_weekly')
    + renderTrend('每月提交', (data.trends || {}).monthly || [], 'month', 'personal_monthly')
    + renderTrend('每年提交', (data.trends || {}).yearly || [], 'year', 'personal_yearly')
    + '</div>' + renderLists(data.lists || {});
}
function renderCards(cards) {
  return '<div class="stats-grid">' + cards.map(card => '<div class="stat-card ' + esc(card.className || '') + '"' + (card.onclick ? ' onclick="' + card.onclick + '"' : '') + '><div class="value">' + esc(card.value) + '</div><div class="label">' + esc(card.label) + '</div></div>').join('') + '</div>';
}
function renderTrend(title, rows, key, chartKey) {
  chartKey = chartKey || title;
  const filtered = filterTrendItems(rows || [], key, chartKey);
  const sorted = filtered.slice().sort(function(a, b) {
    return String(b[key] || '').localeCompare(String(a[key] || ''));
  });
  const max = Math.max(1, ...sorted.map(x => Number(x.count || 0)));
  const bars = sorted.map(row => {
    const count = Number(row.count || 0);
    const label = row[key];
    const clickable = count > 0 ? ' style="cursor:pointer" onclick="showGerritTrendDetail(&quot;' + esc(key) + '&quot;,&quot;' + esc(String(label)) + '&quot;)" title="点击查看该时段提交明细"' : '';
    return '<div class="bar-row"' + clickable + '><div class="bar-label">' + esc(label) + '</div><div class="bar-track"><div class="bar-fill" style="width:' + Math.max(5, Math.round(count * 100 / max)) + '%"></div></div><div class="bar-count">' + esc(count) + '</div></div>';
  }).join('') || '<div class="muted">无数据</div>';
  const range = trendDateRange(chartKey);
  const tip = (range.start || range.end) ? ('范围: ' + (range.start || '不限') + ' 至 ' + (range.end || '不限')) : '设置统计日期范围';
  return '<section class="trend-panel"><div class="trend-title-row"><h2>' + esc(title) + '</h2><button class="trend-start-btn" onclick="setTrendStartDate(&quot;' + esc(chartKey) + '&quot;,&quot;' + esc(title) + '&quot;)" title="' + esc(tip) + '">⚙</button></div><div class="trend-body">' + bars + '</div></section>';
}
function trendDateRange(chartKey) {
  return ((config.chart_date_ranges || {})[chartKey] || {});
}
function trendStartDate(chartKey) {
  return String((trendDateRange(chartKey) || {}).start || '').trim();
}
function trendEndDate(chartKey) {
  return String((trendDateRange(chartKey) || {}).end || '').trim();
}
function filterTrendItems(items, keyName, chartKey) {
  var start = trendStartDate(chartKey);
  var end = trendEndDate(chartKey);
  if (!start && !end) return items || [];
  return (items || []).filter(function(item) {
    var label = String(item[keyName] || '');
    var minLabel = start;
    var maxLabel = end;
    if (keyName === 'week') {
      minLabel = start ? start.slice(0, 4) + '-W' + startWeekNumber(start) : '';
      maxLabel = end ? end.slice(0, 4) + '-W' + startWeekNumber(end) : '';
    } else if (keyName === 'month') {
      minLabel = start ? start.slice(0, 7) : '';
      maxLabel = end ? end.slice(0, 7) : '';
    } else if (keyName === 'year') {
      minLabel = start ? start.slice(0, 4) : '';
      maxLabel = end ? end.slice(0, 4) : '';
    }
    return (!minLabel || label >= minLabel) && (!maxLabel || label <= maxLabel);
  });
}
function startWeekNumber(dateText) {
  var d = new Date(dateText + 'T00:00:00');
  if (isNaN(d.getTime())) return '01';
  d.setHours(0,0,0,0);
  d.setDate(d.getDate() + 3 - (d.getDay() + 6) % 7);
  var week1 = new Date(d.getFullYear(), 0, 4);
  var week = 1 + Math.round(((d - week1) / 86400000 - 3 + (week1.getDay() + 6) % 7) / 7);
  return String(week).padStart(2, '0');
}
function setTrendStartDate(chartKey, title) {
  pendingTrendChartKey = chartKey || '';
  document.getElementById('trendStartModalTitle').textContent = title + ' 日期范围';
  document.getElementById('trendStartDateInput').value = trendStartDate(chartKey);
  document.getElementById('trendEndDateInput').value = trendEndDate(chartKey);
  showModal('trendStartModal');
  setTimeout(function() {
    var input = document.getElementById('trendStartDateInput');
    if (input) input.focus();
  }, 50);
}
function hideTrendStartModal() { hideModal('trendStartModal'); }
async function clearTrendStartDate() {
  document.getElementById('trendStartDateInput').value = '';
  document.getElementById('trendEndDateInput').value = '';
  await saveTrendStartDate();
}
async function saveTrendStartDate() {
  var chartKey = pendingTrendChartKey;
  var start = (document.getElementById('trendStartDateInput').value || '').trim();
  var end = (document.getElementById('trendEndDateInput').value || '').trim();
  if (!chartKey) return;
  if (start && end && start > end) {
    var tmp = start; start = end; end = tmp;
  }
  var ranges = Object.assign({}, config.chart_date_ranges || {});
  if (start || end) ranges[chartKey] = Object.assign({}, start ? {start:start} : {}, end ? {end:end} : {});
  else delete ranges[chartKey];
  try {
    config = await api('/api/gerrit-dashboard/config', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({chart_date_ranges:ranges})});
    hideTrendStartModal();
    if (currentTab === 'department') await loadDepartment(false);
    else await loadPersonal(false);
  } catch (e) { notifyUser('保存统计日期失败', e.message, 'error'); }
}
function renderLists(lists, prefix) {
  const idPrefix = prefix || 'sec';
  return renderChangeList('待评审', lists.pending_review || [], idPrefix + '-pending-review')
    + renderChangeList('最近未合并', lists.open || [], idPrefix + '-open')
    + renderChangeList('最近已合并', lists.merged || [], idPrefix + '-merged')
    + renderChangeList('已废弃', lists.abandoned || [], idPrefix + '-abandoned');
}
function renderChangeList(title, rows, sectionId) {
  return '<section class="list-section" id="' + esc(sectionId || '') + '"><h2>' + esc(title) + '</h2><div class="wrap"><table><thead><tr><th>变更</th><th>项目</th><th>分支</th><th>状态</th><th>更新时间</th></tr></thead><tbody>'
    + (rows || []).map(renderStatsRow).join('')
    + ((rows || []).length ? '' : '<tr><td colspan="5" class="muted">无记录</td></tr>')
    + '</tbody></table></div></section>';
}
function renderDepartmentUsers(users) {
  if (!users.length) return '';
  const rows = users.map(user => {
    const s = user.summary || {};
    const owner = String(user.owner || '');
    const name = String(user.name || owner.split('@')[0] || '-');
    // 整行可点击跳个人看板；操作列阻止冒泡，避免点「移出」也跳转
    const clickAttr = owner ? ' style="cursor:pointer" onclick="viewMemberInPersonal(&quot;' + esc(owner) + '&quot;)"' : '';
    return '<tr' + clickAttr + '>'
      + '<td><strong>' + esc(name) + '</strong></td>'
      + '<td class="muted">' + esc(owner || '-') + '</td>'
      + '<td>' + esc(s.total_count || 0) + '</td>'
      + '<td>' + esc(s.merged_count || 0) + '</td>'
      + '<td>' + esc(s.open_count || 0) + '</td>'
      + '<td>' + esc(s.pending_review_count || 0) + '</td>'
      + '<td>' + esc(user.error || '') + '</td>'
      + '<td onclick="event.stopPropagation()"><button class="secondary dept-action-btn" onclick="removeDepartmentOwner(&quot;' + esc(owner) + '&quot;, this)">移出</button></td>'
      + '</tr>';
  }).join('');
  return '<section class="list-section"><h2>成员汇总</h2><div class="muted" style="margin:-4px 0 10px;font-size:12px">点击任意成员行可在个人看板查看其详细提交。</div><div class="dept-table-wrap"><table class="dept-table"><thead><tr><th>姓名</th><th>邮箱</th><th>历史提交</th><th>已合并</th><th>未合并</th><th>待评审</th><th>错误</th><th>操作</th></tr></thead><tbody>' + rows + '</tbody></table></div></section>';
}
function departmentOptionsHtml(selectedId, includeAll) {
  const seen = {};
  const departments = [];
  (config.department_profiles || []).forEach(function(p) {
    if (!p || !p.id) return;
    seen[p.id] = true;
    departments.push(p);
  });
  (config.redmine_departments || []).forEach(function(p) {
    if (!p || !p.id || seen[p.id]) return;
    seen[p.id] = true;
    departments.push({id:p.id, name:p.name || p.id, source:'redmine'});
  });
  return departments.filter(function(p) {
    return includeAll || p.id !== 'all';
  }).map(function(p) {
    return '<option value="' + esc(p.id) + '" data-name="' + esc(p.name || p.id) + '"' + (p.id === selectedId ? ' selected' : '') + '>' + esc((p.name || p.id) + (p.source === 'redmine' ? ' / Redmine' : '')) + '</option>';
  }).join('');
}
function populateDepartmentSelect(selectId, selectedId, includeAll) {
  const select = document.getElementById(selectId);
  if (!select) return;
  select.innerHTML = departmentOptionsHtml(selectedId || '', includeAll) || '<option value="">暂无部门</option>';
}
function personalControlsHtml() {
  return '<div class="select-with-add"><select id="personalProfile" onchange="onPersonalProfileChange()">' + (config.personal_profiles || []).map(p => '<option value="' + esc(p.id) + '"' + (p.id === currentPersonalProfileId ? ' selected' : '') + '>' + esc((p.name || p.owner || p.id) + (p.department ? ' / ' + p.department : '')) + '</option>').join('') + '</select><button class="select-add-btn" type="button" onclick="showAddPersonalModal()" title="添加成员">＋</button></div>'
    + '<input id="owner" value="' + esc(requestedOwner || (document.getElementById('owner') ? document.getElementById('owner').value : '') || ((config.personal_profiles || [])[0] || {}).owner || config.default_owner || '') + '" placeholder="owner 邮箱">'
    + '<button onclick="loadPersonal(true)">刷新</button>';
}
function departmentControlsHtml(profileId) {
  return '<div class="select-with-add"><select id="departmentProfile" onchange="loadDepartment(true)" style="min-width:160px">' + departmentOptionsHtml(profileId, true) + '</select><button class="select-add-btn" type="button" onclick="showAddDepartmentModal(&quot;departmentProfile&quot;)" title="添加部门">＋</button></div>'
    + '<button class="secondary" onclick="showAddPersonalModal()">成员</button><button class="secondary" onclick="syncRedmineMembers(this)">同步成员</button><button onclick="loadDepartment(true)">刷新</button>';
}
function renderDepartmentDashboard(data) {
  const profile = data.profile || {};
  currentDepartmentProfileId = profile.id || currentDepartmentProfileId || '';
  const s = data.summary || {};
  const header = '<section class="list-section">' + renderSummaryHeader((profile.name || '部门') + ' Gerrit 提交汇总', departmentControlsHtml(currentDepartmentProfileId), '成员: ' + ((data.users || []).length) + ' | 来源: ' + esc(((data.users || [])[0] || {}).source || '-') + ' | 缓存: ' + (data.cache_hit ? '是' : '否'))
    + renderCards([
      {label:'历史提交', value:s.total_count || 0},
      {label:'已合并', value:s.merged_count || 0, className:'ok'},
      {label:'未合并', value:s.open_count || 0, className:'warn'},
      {label:'待评审', value:s.pending_review_count || 0, className:'bad'},
      {label:'已废弃', value:s.abandoned_count || 0}
    ]) + '</section>';
  const trends = '<div class="trend-grid">' + renderTrend('每天提交', (data.trends || {}).daily || [], 'date', 'department_daily') + renderTrend('每周提交', (data.trends || {}).weekly || [], 'week', 'department_weekly') + renderTrend('每月提交', (data.trends || {}).monthly || [], 'month', 'department_monthly') + renderTrend('每年提交', (data.trends || {}).yearly || [], 'year', 'department_yearly') + '</div>';
  return header + trends + renderDepartmentUsers(data.users || []);
}
function showAddPersonalModal() {
  document.getElementById('addPersonalName').value='';
  document.getElementById('addPersonalOwner').value='';
  const selectedDepartment = (currentTab === 'department' && currentDepartmentProfileId && currentDepartmentProfileId !== 'all') ? currentDepartmentProfileId : '';
  populateDepartmentSelect('addPersonalDepartment', selectedDepartment, false);
  showModal('addPersonalModal');
  document.getElementById('addPersonalName').focus();
}
function hideAddPersonalModal() { hideModal('addPersonalModal'); }
async function savePersonalProfile() {
  try {
    const departmentSelect = document.getElementById('addPersonalDepartment');
    const departmentOption = departmentSelect && departmentSelect.selectedOptions ? departmentSelect.selectedOptions[0] : null;
    const result = await api('/api/gerrit-dashboard/personal-profiles', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({name:document.getElementById('addPersonalName').value, owner:document.getElementById('addPersonalOwner').value, department_id:departmentSelect ? departmentSelect.value : '', department_name:departmentOption ? (departmentOption.dataset.name || departmentOption.textContent || '') : ''})});
    config = result.dashboard || config;
    currentPersonalProfileId = (result.profile || {}).id || currentPersonalProfileId;
    if ((result.profile || {}).department_id) currentDepartmentProfileId = result.profile.department_id;
    hideAddPersonalModal();
    await init();
    if (currentTab === 'department') await loadDepartment(true);
  } catch (e) { notifyUser('保存成员失败', e.message, 'error'); }
}
function showAddDepartmentModal(targetSelectId) {
  pendingDepartmentTargetSelect = targetSelectId || 'departmentProfile';
  document.getElementById('addDepartmentName').value='';
  document.getElementById('addDepartmentId').value='';
  document.getElementById('addDepartmentOwners').value='';
  showModal('addDepartmentModal');
  document.getElementById('addDepartmentName').focus();
}
function hideAddDepartmentModal() { hideModal('addDepartmentModal'); }
async function saveDepartmentProfile() {
  try {
    const result = await api('/api/gerrit-dashboard/department-profiles', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({name:document.getElementById('addDepartmentName').value, profile_id:document.getElementById('addDepartmentId').value, owners:document.getElementById('addDepartmentOwners').value})});
    config = result.dashboard || config;
    currentDepartmentProfileId = (result.profile || {}).id || currentDepartmentProfileId;
    hideAddDepartmentModal();
    if (pendingDepartmentTargetSelect === 'addPersonalDepartment') {
      populateDepartmentSelect('addPersonalDepartment', (result.profile || {}).id || '', false);
    } else {
      await init();
      switchTab('department');
    }
  } catch (e) { notifyUser('添加部门看板失败', e.message, 'error'); }
}
function showAddDepartmentOwnerModal() { document.getElementById('addDepartmentOwnerValue').value=''; showModal('addDepartmentOwnerModal'); }
function hideAddDepartmentOwnerModal() { hideModal('addDepartmentOwnerModal'); }
async function saveDepartmentOwner() {
  try {
    const profileSelect = document.getElementById('departmentProfile');
    const profileId = currentDepartmentProfileId || (profileSelect ? profileSelect.value : '') || '';
    const result = await api('/api/gerrit-dashboard/department-profiles/' + encodeURIComponent(profileId) + '/owners', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({owner:document.getElementById('addDepartmentOwnerValue').value})});
    config = result.dashboard || config;
    hideAddDepartmentOwnerModal();
    await loadDepartment(true);
  } catch (e) { notifyUser('添加部门成员失败', e.message, 'error'); }
}
async function removeDepartmentOwner(owner, btn) {
  const profileSelect = document.getElementById('departmentProfile');
  const profileId = currentDepartmentProfileId || (profileSelect ? profileSelect.value : '') || '';
  if (!profileId || !owner) return;
  if (profileId === 'all') {
    notifyUser('无法移出成员', '请先选择具体部门，再移出成员。', 'warning');
    return;
  }
  if (!confirm('确认从当前部门移出 ' + owner + '？')) return;
  if (btn) btn.disabled = true;
  try {
    const result = await api('/api/gerrit-dashboard/department-profiles/' + encodeURIComponent(profileId) + '/owners', {method:'DELETE', headers:{'Content-Type':'application/json'}, body:JSON.stringify({owner:owner})});
    config = result.dashboard || config;
    await loadDepartment(true);
  } catch (e) {
    notifyUser('移出部门成员失败', e.message, 'error');
  } finally {
    if (btn) btn.disabled = false;
  }
}
async function syncRedmineMembers(btn) {
  if (btn) { btn.disabled = true; btn.textContent = '同步中'; }
  try {
    config = await api('/api/gerrit-dashboard/sync-redmine-members', {method:'POST'});
    await loadDepartment(true);
  } catch (e) {
    notifyUser('同步成员失败', e.message, 'error');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '同步成员'; }
  }
}
function changeUrl(item) {
  if (item.url) return item.url;
  const id = item._number || item.number || item.id || '';
  const base = String(config.base_url || '').replace(new RegExp('/+$'), '');
  return base && id ? base + '/c/' + encodeURIComponent(id) : '';
}
function renderStatsRow(item) {
  const url = changeUrl(item);
  const id = item.number || item._number || item.id || '-';
  const subject = item.subject || '-';
  return '<tr><td><div class="change-title">' + (url ? '<a href="' + esc(url) + '" target="_blank">#' + esc(id) + '</a>' : '#' + esc(id)) + '<span title="' + esc(subject) + '">' + esc(subject) + '</span></div></td><td>' + esc(item.project || '-') + '</td><td>' + esc(item.branch || '-') + '</td><td>' + esc(item.status || '-') + '</td><td>' + esc(item.updated || item.lastUpdated || item.updated_at || '-') + '</td></tr>';
}
function renderRow(item) {
  const url = changeUrl(item);
  const id = item._number || item.number || item.id || item.changeId || '-';
  const subject = item.subject || '-';
  return '<tr><td><div class="change-title">' + (url ? '<a href="' + esc(url) + '" target="_blank">#' + esc(id) + '</a>' : '#' + esc(id)) + '<span title="' + esc(subject) + '">' + esc(subject) + '</span></div></td><td>' + esc(item.project || '-') + '</td><td>' + esc(item.branch || '-') + '</td><td>' + esc(item.status || '-') + '</td><td>' + esc((item.owner || {}).name || (item.owner || {}).email || '-') + '</td><td>' + esc(item.updated || item.lastUpdated || item.createdOn || item.created || '-') + '</td></tr>';
}
init();
</script>
</body>
</html>
"""
    )
