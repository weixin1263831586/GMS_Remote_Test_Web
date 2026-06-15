"""RedmineAgent APIs and page."""

from __future__ import annotations

import asyncio
import smtplib
import uuid
from datetime import datetime
from email.message import EmailMessage
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

from core.redmine_agent import RedmineAgent
from core.redmine_agent_db import (
    RedmineAgentDB, USER_MAP_PATH, find_user_mapping, display_names_from_mapping, load_redmine_user_map,
    load_user_map_payload, save_user_map_payload,
    compute_user_overdue_stats, _name_keys as _nk,
)
from core.redmine_dashboard_config import (
    add_department_profile,
    assign_user_to_profiles,
    denormalize_redmine_dashboard_config,
    filter_users_for_profile,
    issue_id_list,
    issue_url_text,
    add_project_profile,
    merge_resolved_trends,
    select_redmine_dashboard_profile,
    summarize_project_issues,
)
from core.config import config_manager
from modules.redmine_agent_scheduler import get_scheduler_config


router = APIRouter(prefix="/api/redmine-agent")
page_router = APIRouter()

_db = RedmineAgentDB()
_agent = RedmineAgent(_db)
_run_lock = asyncio.Lock()
_active_task: Optional[asyncio.Task] = None
_active_run_id: Optional[str] = None
_stale_runs_marked = False
_DEPARTMENT_OVERDUE_CACHE: Dict[str, Any] = {}
_WORKLOAD_STATS_CACHE: Dict[str, Any] = {}
_PROJECT_STATS_CACHE: Dict[str, Any] = {}


def _get_redmine_stats_config() -> Dict[str, Any]:
    """Read redmine_stats config (stale_days, window_days, cache_ttl) with defaults."""
    return config_manager.get_redmine_stats_config()


def _clear_stats_caches() -> None:
    _DEPARTMENT_OVERDUE_CACHE.clear()
    _WORKLOAD_STATS_CACHE.clear()
    _PROJECT_STATS_CACHE.clear()


def _get_redmine_base_url() -> str:
    return config_manager.get_redmine_base_url()




def _profile_ids_from_body(body: Dict[str, Any]) -> List[str]:
    raw = body.get("profile_ids")
    if raw is None:
        raw = body.get("department_ids")
    if raw is None:
        raw = [body.get("profile_id") or body.get("department_id") or ""]
    if not isinstance(raw, list):
        raw = [raw]
    return [str(item or "").strip() for item in raw if str(item or "").strip() and str(item or "").strip() != "all"]


def _department_from_profiles(profile_ids: List[str]) -> Dict[str, str]:
    if not profile_ids:
        return {}
    dashboard = config_manager.get_redmine_dashboard_config()
    for profile in dashboard.get("profiles") or []:
        if profile.get("id") == profile_ids[0]:
            return {
                "department_id": str(profile.get("id") or ""),
                "department": str(profile.get("name") or ""),
            }
    return {"department_id": profile_ids[0], "department": ""}


def _send_reminder_email(to_addr: str, subject: str, body: str) -> Dict[str, Any]:
    dashboard_cfg = config_manager.load_config().get("redmine_dashboard") or {}
    email_cfg = dashboard_cfg.get("email") or {}
    smtp_host = str(email_cfg.get("smtp_host") or "").strip()
    # from_addr 默认值统一来自 config.json 的 redmine_dashboard.email.default_from_addr
    default_from = str(email_cfg.get("default_from_addr") or "").strip()
    from_addr = str(email_cfg.get("from_addr") or email_cfg.get("username") or default_from).strip()
    if not smtp_host:
        return {"sent": False, "mode": "unconfigured", "error": "SMTP 未配置，请在设置中填写 smtp_host"}

    smtp_port = int(email_cfg.get("smtp_port") or 465)
    username = str(email_cfg.get("username") or "").strip()
    password = str(email_cfg.get("password") or "").strip()
    is_qiye_163 = smtp_host.lower().endswith("qiye.163.com")
    # 163 企业邮要求发件人与登录账号一致；缺省（default_from）或与账号不符时，强制对齐
    if is_qiye_163 and username and (not from_addr or from_addr == default_from or from_addr != username):
        from_addr = username
    use_ssl = bool(email_cfg.get("use_ssl", smtp_port == 465))
    use_tls = bool(email_cfg.get("use_tls", not use_ssl and smtp_port != 465))
    timeout = int(email_cfg.get("timeout") or 10)

    # 注意：SMTP 授权码 与 Redmine 网页登录/API 密码是两回事，不能互相兜底。
    # 163 企业邮用错误凭据会被服务器直接断开连接（而非返回认证失败码），
    # 因此这里必须用专门的 SMTP 授权码；为空时直接返回明确错误，引导用户填写。
    if is_qiye_163 and (not username or not password):
        return {"sent": False, "mode": "unconfigured", "error": "163 企业邮箱 SMTP 需要用户名和授权码（注意：是邮箱 SMTP 授权码，不是 Redmine 登录密码），请在 Redmine 看板「设置 → SMTP」中填写"}

    message = EmailMessage()
    message["From"] = from_addr
    message["To"] = to_addr
    message["Subject"] = subject
    message.set_content(body)
    try:
        if use_ssl:
            with smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=timeout) as smtp:
                if username and password:
                    smtp.login(username, password)
                smtp.send_message(message)
        else:
            with smtplib.SMTP(smtp_host, smtp_port, timeout=timeout) as smtp:
                if use_tls:
                    smtp.starttls()
                if username and password:
                    smtp.login(username, password)
                smtp.send_message(message)
    except smtplib.SMTPAuthenticationError as exc:
        return {
            "sent": False,
            "mode": "smtp",
            "error": f"SMTP认证失败，请在设置中填写企业邮箱SMTP授权码/密码，发件人需与账号一致: {exc}",
        }
    except smtplib.SMTPServerDisconnected as exc:
        return {
            "sent": False,
            "mode": "smtp",
            "error": f"SMTP连接被服务器关闭，请检查企业邮箱SMTP授权码/密码、账号是否开启SMTP服务，发件人需与账号一致: {exc}",
        }
    return {"sent": True, "mode": "smtp"}


def _check_ttl_cache(cache_dict: Dict, cache_key: str, ttl: int, now_ts: float, refresh: bool = False) -> Optional[Dict]:
    """Check a TTL cache dict. Returns cached data on hit, None on miss."""
    if refresh:
        return None
    cached = cache_dict.get(cache_key)
    if cached and ttl > 0 and now_ts - cached.get("cached_at_ts", 0) < ttl:
        return cached.get("data")
    return None


def _update_ttl_cache(cache_dict: Dict, cache_key: str, now_ts: float, data: Any) -> None:
    """Store data in a TTL cache dict and evict stale keys."""
    cache_dict[cache_key] = {"cached_at_ts": now_ts, "data": data}
    stale_keys = [k for k in cache_dict if k != cache_key]
    for k in stale_keys:
        del cache_dict[k]


def _ensure_stale_runs_marked() -> None:
    """Mark stale runs on first API call instead of at import time."""
    global _stale_runs_marked
    if not _stale_runs_marked:
        _stale_runs_marked = True
        _db.mark_stale_running_runs()


async def _start_task(coro_factory, run_id: str, message: str) -> dict:
    """Shared helper to launch a background task with lock protection."""
    global _active_task, _active_run_id
    async with _run_lock:
        if _active_task and not _active_task.done():
            return {"success": False, "error": "RedmineAgent already running", "run_id": _active_run_id}
        _active_task = asyncio.create_task(coro_factory())
        _active_run_id = run_id
        return {"success": True, "message": message, "run_id": run_id}


async def start_redmine_agent_run(hours: int = 24, max_issues: int = 20, mode: str = "manual") -> dict:
    run_id = datetime.now().strftime("%Y%m%d%H%M%S") + "-" + uuid.uuid4().hex[:8]
    return await _start_task(
        lambda: _agent.run(hours=hours, max_issues=max_issues, run_id=run_id, mode=mode),
        run_id,
        "RedmineAgent started",
    )


async def start_redmine_agent_sync(max_analyze: int = 20) -> dict:
    run_id = "sync-" + datetime.now().strftime("%Y%m%d%H%M%S")
    return await _start_task(
        lambda: _agent.sync_all_assigned_issues(analyze_new=True, max_analyze=max_analyze),
        run_id,
        "RedmineAgent sync started",
    )


# ------------------------------------------------------------------
# Existing endpoints (enhanced)
# ------------------------------------------------------------------

@router.post("/runs")
async def create_run(
    hours: int = Query(48, ge=1, le=168),
    max_issues: int = Query(20, ge=1, le=100),
):
    _ensure_stale_runs_marked()
    return await start_redmine_agent_run(hours=hours, max_issues=max_issues, mode="manual")


@router.get("/status")
async def get_status():
    _ensure_stale_runs_marked()
    running = bool(_active_task and not _active_task.done())
    result = None
    if _active_task and _active_task.done():
        try:
            result = _active_task.result()
        except Exception as exc:
            result = {"status": "failed", "error": str(exc)}
    return {"success": True, "data": {"running": running, "active_run_id": _active_run_id, "last_result": result}}


@router.get("/runs")
async def list_runs(limit: int = Query(20, ge=1, le=100)):
    return {"success": True, "data": {"items": _db.list_runs(limit)}}


@router.get("/runs/{run_id}")
async def get_run(run_id: str):
    run = _db.get_run(run_id)
    if not run:
        return JSONResponse(status_code=404, content={"success": False, "error": "run not found"})
    return {"success": True, "data": {"run": run, "issues": _db.list_run_issues(run_id)}}


@router.get("/issues/{issue_id}")
async def get_issue(issue_id: int):
    issue = _db.get_issue(issue_id)
    if not issue:
        return JSONResponse(status_code=404, content={"success": False, "error": "issue not found"})
    # Enrich with structured fields
    ai = issue.get("ai_json") or {}
    enriched = {
        **issue,
        "title": issue.get("subject") or ai.get("title", ""),
        "problem_description": issue.get("problem_description") or RedmineAgent.extract_description(issue),
        "error_info": issue.get("error_info") or RedmineAgent.extract_error_from_failures(issue.get("failures_json", [])),
        "error_analysis": issue.get("error_analysis") or ai.get("root_cause_guess", ""),
        "solution": issue.get("solution") or ai.get("solution", ""),
        "patch_direction": issue.get("patch_direction") or ai.get("patch_direction", ""),
    }
    return {"success": True, "data": enriched}


@router.get("/issues/{issue_id}/document")
async def get_issue_document(issue_id: int):
    issue = _db.get_issue(issue_id)
    if not issue:
        return PlainTextResponse("issue not found", status_code=404)
    return PlainTextResponse(issue.get("doc_content") or "", media_type="text/markdown")


# ------------------------------------------------------------------
# New endpoints
# ------------------------------------------------------------------

@router.get("/issues")
async def list_issues(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    status: str = Query(""),
    priority: str = Query(""),
    category: str = Query(""),
    search: str = Query(""),
    sort: str = Query("updated_on"),
    order: str = Query("desc"),
):
    issues = _db.list_all_issues(limit=limit, offset=offset, status=status, priority=priority, category=category, search=search, sort=sort, order=order)
    total = _db.count_issues(status=status, priority=priority, category=category, search=search)
    return {"success": True, "data": {"items": issues, "total": total, "limit": limit, "offset": offset}}


@router.get("/issues/search")
async def search_issues(q: str = Query(..., min_length=1), limit: int = Query(10, ge=1, le=50)):
    return {"success": True, "data": {"items": _db.search_issues(q, limit)}}


@router.get("/statistics")
async def get_statistics():
    _ensure_stale_runs_marked()
    return {"success": True, "data": _db.get_issue_statistics()}


async def _resolve_owner_names() -> List[str]:
    names: List[str] = []
    try:
        client = _agent._make_client()
        user = await client.get_current_user()
        first = str(getattr(user, "firstname", "") or "").strip()
        last = str(getattr(user, "lastname", "") or "").strip()
        login = str(getattr(user, "login", "") or "").strip()
        mail = str(getattr(user, "mail", "") or getattr(user, "email", "") or "").strip()
        display_name = f"{last} {first}".strip() or f"{first} {last}".strip()
        names.extend([
            display_name,
            mail or login,
        ])
    except Exception:
        pass

    return list(dict.fromkeys(name for name in names if name))


def _empty_user_stats(user: Dict[str, Any], error: str = "") -> Dict[str, Any]:
    return {
        "id": user.get("id"),
        "name": user.get("name") or "",
        "aliases": user.get("aliases") or [],
        "total_owned": 0,
        "open_count": 0,
        "closed_count": 0,
        "scanned_open_count": 0,
        "waiting_my_reply": 0,
        "no_reply_3_days": 0,
        "max_unreplied_days": 0,
        "overdue_issues": [],
        "detail_source": "local_db",
        **({"error": error} if error else {}),
    }


@router.get("/users")
async def list_stat_users():
    users = [
        {
            "id": item.get("id"),
            "name": item.get("name") or "",
            "aliases": item.get("aliases") or [],
            "email": item.get("email") or item.get("eamil") or "",
            "department_id": item.get("department_id") or "",
            "department": item.get("department") or "",
        }
        for item in load_redmine_user_map()
    ]
    current_names = await _resolve_owner_names()
    if current_names:
        current_keys = set()
        for n in current_names:
            current_keys.update(_nk(n))
        # Only insert if not already in user_map
        already_mapped = any(
            _nk(item.get("name") or "") & current_keys
            for item in users
        )
        if not already_mapped:
            users.insert(0, {"id": "me", "name": current_names[0], "aliases": current_names[1:]})
    return {"success": True, "data": {"items": users}}


@router.post("/users")
async def add_stat_user(request: Request):
    body = await request.json()
    uid = body.get("id")
    name = str(body.get("name") or "").strip()
    email = str(body.get("email") or body.get("eamil") or "").strip()
    profile_ids = _profile_ids_from_body(body)
    department = _department_from_profiles(profile_ids)
    if not uid or not name:
        return {"success": False, "error": "id and name are required"}
    uid_text = str(uid).strip()

    user_map = load_user_map_payload()
    departments = user_map.setdefault("departments", [])
    dept_id = str(department.get("department_id") or "").strip()
    dept_name = str(department.get("department") or "").strip()
    created = True
    target_department = None
    for dept in departments:
        if not isinstance(dept, dict):
            continue
        if dept_id and str(dept.get("department_id") or "").strip() == dept_id:
            target_department = dept
            break
        if not dept_id and dept_name and str(dept.get("department") or "").strip() == dept_name:
            target_department = dept
            break
    if target_department is None:
        target_department = {"department_id": dept_id, "department": dept_name, "members": []}
        departments.append(target_department)
    updated_member = {"id": uid, "name": name}
    if email:
        updated_member["email"] = email
    for dept in departments:
        if not isinstance(dept, dict):
            continue
        members = dept.setdefault("members", [])
        kept = []
        for item in members:
            if isinstance(item, dict) and str(item.get("id") or "").strip() == uid_text:
                created = False
                if dept is target_department:
                    kept.append(updated_member)
                continue
            kept.append(item)
        dept["members"] = kept
    if created:
        target_department.setdefault("members", []).append(updated_member)
    user_map.pop("users", None)
    save_user_map_payload(user_map)

    if profile_ids:
        dashboard_cfg = assign_user_to_profiles(config_manager.get_redmine_dashboard_config(), uid_text, profile_ids)
        if not config_manager.save_redmine_dashboard_config(denormalize_redmine_dashboard_config(dashboard_cfg)):
            return JSONResponse(status_code=500, content={"success": False, "error": "failed to save department membership"})
        _clear_stats_caches()
    return {"success": True, "data": {"created": created, "profile_ids": profile_ids}}


@router.post("/dashboard/profiles")
async def create_dashboard_profile(request: Request):
    body = await request.json()
    name = str(body.get("name") or "").strip()
    profile_id = str(body.get("id") or body.get("profile_id") or "").strip()
    try:
        dashboard_cfg = add_department_profile(config_manager.get_redmine_dashboard_config(), name, profile_id)
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"success": False, "error": str(exc)})
    if not config_manager.save_redmine_dashboard_config(denormalize_redmine_dashboard_config(dashboard_cfg)):
        return JSONResponse(status_code=500, content={"success": False, "error": "failed to save dashboard profile"})
    _clear_stats_caches()
    return {"success": True, "data": {"dashboard": dashboard_cfg, "profile": dashboard_cfg["profiles"][-1]}}


@router.post("/dashboard/projects")
async def create_project_profile(request: Request):
    body = await request.json()
    name = str(body.get("name") or "").strip()
    project_id = str(body.get("project_id") or body.get("project_url") or "").strip()
    profile_id = str(body.get("id") or body.get("profile_id") or "").strip()
    try:
        dashboard_cfg = add_project_profile(config_manager.get_redmine_dashboard_config(), name, project_id, profile_id)
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"success": False, "error": str(exc)})
    if not config_manager.save_redmine_dashboard_config(denormalize_redmine_dashboard_config(dashboard_cfg)):
        return JSONResponse(status_code=500, content={"success": False, "error": "failed to save project profile"})
    _clear_stats_caches()
    return {"success": True, "data": {"dashboard": dashboard_cfg, "profile": dashboard_cfg["project_profiles"][-1]}}


@router.post("/reminders/email")
async def send_department_reminder_email(request: Request):
    body = await request.json()
    user_id = str(body.get("user_id") or "").strip()
    issue_ids = [str(item or "").strip() for item in body.get("issue_ids") or [] if str(item or "").strip()]
    if not user_id:
        return JSONResponse(status_code=400, content={"success": False, "error": "user_id is required"})
    if not issue_ids:
        return JSONResponse(status_code=400, content={"success": False, "error": "issue_ids are required"})
    user = next((item for item in load_redmine_user_map() if str(item.get("id") or "").strip() == user_id), None)
    if not user:
        return JSONResponse(status_code=404, content={"success": False, "error": "user not found"})
    to_addr = str(user.get("email") or user.get("eamil") or "").strip()
    if not to_addr:
        return JSONResponse(status_code=400, content={"success": False, "error": "user email is not configured"})

    base_url = _get_redmine_base_url()
    issues = [{"issue_id": issue_id} for issue_id in issue_ids]
    url_text = issue_url_text(issues, base_url)
    subject = str(body.get("subject") or "").strip() or f"Redmine 超阈值未回复提醒 - {user.get('name') or user_id}"
    intro = str(body.get("intro") or "").strip() or "以下 Redmine 问题已超过未回复阈值，请及时处理："
    body_text = intro + "\n\n" + url_text
    try:
        result = await asyncio.to_thread(_send_reminder_email, to_addr, subject, body_text)
    except Exception as exc:
        return JSONResponse(status_code=500, content={"success": False, "error": f"邮件发送失败: {exc}"})
    if not result.get("sent"):
        return JSONResponse(status_code=503, content={"success": False, "error": result.get("error", "邮件发送失败"), "data": result})
    return {"success": True, "data": {"to": to_addr, "subject": subject, "body": body_text, **result}}


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
    live_counts: Dict[str, int] = {}
    if name:
        mapped = find_user_mapping(name)
        if mapped:
            owner_names = display_names_from_mapping(mapped)
            display_names = owner_names
            client = _agent._make_client()
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
            for run in _db.list_runs(10):
                assigned_to = str(run.get("assigned_to") or "").strip()
                if assigned_to:
                    extra_names.append(assigned_to)
        except Exception:
            pass
    all_names = owner_names + [n for n in extra_names if n]
    data = _db.get_workload_statistics(
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


async def _department_user_overdue(client, user: Dict[str, Any], stale_days: int, issue_limit: int, window_days: int = 0) -> Dict[str, Any]:
    try:
        return await compute_user_overdue_stats(client, _db, user, stale_days, issue_limit, window_days)
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
    profile_users: List[Dict[str, Any]] = []
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
            client = _agent._make_client()
            semaphore = asyncio.Semaphore(4)

            async def _user_issues(user: Dict[str, Any]) -> List[Dict[str, Any]]:
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
            issues: List[Dict[str, Any]] = []
            for batch in batches:
                for item in batch:
                    iid = int(item.get("issue_id") or 0)
                    if iid and iid not in seen:
                        seen.add(iid)
                        issues.append(item)
            issues.sort(key=lambda i: (i.get("resolved_on") or "", i.get("issue_id") or 0), reverse=True)
            issues = issues[:limit]
        else:
            issues = _db.get_resolved_issues_by_date(
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
    stale_days: Optional[int] = Query(None, ge=1, le=30),
    list_limit: Optional[int] = Query(None, ge=1, le=500),
    issue_limit: Optional[int] = Query(None, ge=1, le=2000),
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
    client = _agent._make_client()
    window_days = int(profile.get("window_days") or stats_cfg["window_days"])
    semaphore = asyncio.Semaphore(4)

    async def _safe_user(user: Dict[str, Any]) -> Dict[str, Any]:
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

    client = _agent._make_client()
    try:
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
    finally:
        await client.close()


@router.post("/sync")
async def trigger_sync(max_analyze: int = Query(20, ge=1, le=100)):
    return await start_redmine_agent_sync(max_analyze=max_analyze)


@router.post("/issues/{issue_id}/fetch")
async def fetch_and_analyze_issue(issue_id: int):
    """Fetch a single issue from Redmine and analyze it."""
    existing = _db.get_issue(issue_id)
    if existing and existing.get("analysis_status") == "done":
        return {"success": True, "data": {"action": "exists", "issue": existing}}
    run_id = f"fetch-{datetime.now().strftime('%Y%m%d%H%M%S')}"
    return await _start_task(
        lambda: _agent.analyze_issue(_agent._make_client(), issue_id, run_id),
        run_id,
        f"Fetching #{issue_id} from Redmine",
    )


@router.get("/reports/latest")
async def get_latest_report():
    run = _db.get_latest_run()
    if not run:
        return JSONResponse(status_code=404, content={"success": False, "error": "no completed runs"})
    return {"success": True, "data": {"run": run, "issues": _db.list_run_issues(run.get("run_id", ""))}}


@router.get("/config")
async def get_config():
    return {"success": True, "data": get_scheduler_config()}


@router.get("/config/stats")
async def get_stats_config():
    """Read redmine_stats config for the settings UI — single config load."""
    config = config_manager.load_config()
    stats_cfg = config_manager.get_redmine_stats_config()
    dashboard_cfg = config_manager.get_redmine_dashboard_config()
    gerrit_cfg = config_manager.get_gerrit_dashboard_config()
    if gerrit_cfg.get("rest_password"):
        gerrit_cfg = {**gerrit_cfg, "rest_password": "***"}
    redmine_cfg = config.get("redmine") or {}
    email_cfg = (config.get("redmine_dashboard") or {}).get("email") or {}
    base_url = config_manager.get_redmine_base_url(config)
    return {"success": True, "data": {
        **stats_cfg,
        "dashboard": dashboard_cfg,
        "gerrit_dashboard": gerrit_cfg,
        "redmine_base_url": base_url,
        "email_mode": "smtp" if email_cfg.get("smtp_host") else "smtp_unconfigured",
    }}


@router.post("/config/stats")
async def update_stats_config(request: Request):
    """Update redmine_stats config from the settings UI."""
    body = await request.json()
    config = config_manager.load_config()
    stats = config.get("redmine_stats") or {}
    if "stale_days" in body:
        stats["stale_days"] = max(1, min(30, int(body["stale_days"])))
    if "window_days" in body:
        stats["window_days"] = max(0, min(365, int(body["window_days"])))
    if "cache_ttl" in body:
        stats["cache_ttl"] = max(0, min(3600, int(body["cache_ttl"])))
    if "chart_start_dates" in body and isinstance(body.get("chart_start_dates"), dict):
        current_dates = dict(stats.get("chart_start_dates") or {})
        for key, value in body.get("chart_start_dates", {}).items():
            clean_key = str(key or "").strip()
            clean_value = str(value or "").strip()
            if not clean_key:
                continue
            if clean_value:
                current_dates[clean_key] = clean_value
            else:
                current_dates.pop(clean_key, None)
        stats["chart_start_dates"] = current_dates
    if "chart_date_ranges" in body and isinstance(body.get("chart_date_ranges"), dict):
        current_ranges = dict(stats.get("chart_date_ranges") or {})
        for key, value in body.get("chart_date_ranges", {}).items():
            clean_key = str(key or "").strip()
            if not clean_key:
                continue
            if isinstance(value, dict):
                start = str(value.get("start") or "").strip()
                end = str(value.get("end") or "").strip()
                if start or end:
                    current_ranges[clean_key] = {
                        **({"start": start} if start else {}),
                        **({"end": end} if end else {}),
                    }
                    continue
            current_ranges.pop(clean_key, None)
        stats["chart_date_ranges"] = current_ranges
    if not config_manager.save_redmine_stats_config(stats):
        return JSONResponse(status_code=500, content={"success": False, "error": "failed to save stats config"})
    _clear_stats_caches()
    return {"success": True, "data": config_manager.get_redmine_stats_config()}


@router.post("/config/email")
async def update_email_config(request: Request):
    """Update SMTP email config from the settings UI."""
    body = await request.json()
    config = config_manager.load_config()
    dashboard = config.get("redmine_dashboard") or {}
    email = dashboard.get("email") or {}
    if "smtp_host" in body:
        email["smtp_host"] = str(body["smtp_host"] or "").strip()
    if "smtp_port" in body:
        email["smtp_port"] = int(body["smtp_port"] or 465)
    if "from_addr" in body:
        email["from_addr"] = str(body["from_addr"] or "").strip()
    if "username" in body:
        email["username"] = str(body["username"] or "").strip()
    if "password" in body:
        new_pass = str(body["password"] or "").strip()
        if new_pass:
            email["password"] = new_pass
    if email.get("smtp_port") == 465:
        email["use_ssl"] = True
        email.pop("use_tls", None)
    else:
        email["use_tls"] = True
        email.pop("use_ssl", None)
    dashboard["email"] = email
    if not config_manager.save_redmine_dashboard_config(dashboard):
        return JSONResponse(status_code=500, content={"success": False, "error": "failed to save email config"})
    return {"success": True, "data": {"email": email, "email_mode": "smtp" if email.get("smtp_host") else "unconfigured"}}


# ------------------------------------------------------------------
# Web UI
# ------------------------------------------------------------------

@page_router.get("/redmine-agent", response_class=HTMLResponse)
async def redmine_agent_page():
    return HTMLResponse(
        """
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>RedmineAgent</title>
  <style>
    :root { color-scheme: dark; --bg:#0b0d12; --panel:#131720; --panel2:#191f2b; --border:#2b3342; --text:#e8edf7; --muted:#96a1b5; --primary:#3b82f6; --ok:#22c55e; --warn:#f59e0b; --bad:#ef4444; --high:#ef4444; --medium:#f59e0b; --low:#6b7280; }
    * { box-sizing: border-box; }
    html { scrollbar-gutter: stable; overflow-y: scroll; scroll-padding-top:86px; }
    body { margin:0; font-family:system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; background:var(--bg); color:var(--text); }
    header { position:sticky; top:0; z-index:1000; display:grid; grid-template-columns:max-content minmax(0, 1fr) 480px 340px; align-items:center; column-gap:16px; padding:8px 16px; border-bottom:1px solid var(--border); background:var(--panel); box-shadow:0 8px 18px rgba(0,0,0,.22); }
    .header-title { font-size:18px; font-weight:700; white-space:nowrap; margin:0; }
    .header-right { display:flex; align-items:center; gap:12px; flex-shrink:0; flex-wrap:wrap; }
    .muted { color:var(--muted); font-size:13px; line-height:1.6; }
    button { height:30px; border:0; border-radius:6px; padding:0 10px; color:white; background:var(--primary); font-weight:650; cursor:pointer; font-size:13px; }
    button.secondary { background:#30394a; }
    button.warn { background:#b45309; }
    button:disabled { opacity:.55; cursor:not-allowed; }
    .btn-group { display:flex; gap:6px; flex-wrap:wrap; }
    /* Header toolbar: freeze layout so text changes (扫描/同步/刷新 中…) don't reflow */
    .header-spacer { min-width:0; }
    header .filter-bar { width:480px; min-width:480px; justify-content:flex-start; flex-wrap:nowrap; }
    header .btn-group { flex-wrap:nowrap; }
    header .btn-group { width:340px; min-width:340px; justify-content:flex-end; }
    header .btn-group > button { width:78px; min-width:78px; white-space:nowrap; box-sizing:border-box; padding:0 6px; }
    header .btn-group > button[title="设置"] { width:38px; min-width:38px; }
    header .filter-bar input { width:260px; min-width:260px; }
    header .filter-bar select { width:100px; min-width:100px; }

    /* Modal */
    .modal { display:none; position:fixed; inset:0; background:rgba(0,0,0,0.7); justify-content:center; align-items:center; z-index:9999; padding:20px; }
    .modal.show { display:flex; }
    .modal-content { background:var(--panel); border:1px solid var(--border); border-radius:8px; box-shadow:0 8px 32px rgba(0,0,0,0.4); max-width:400px; width:100%; max-height:85vh; display:flex; flex-direction:column; overflow:hidden; }
    .modal-header { background:linear-gradient(135deg,#667eea,#764ba2); border-bottom:1px solid var(--border); padding:12px 16px; display:flex; justify-content:space-between; align-items:center; }
    .modal-title { color:#fff; font-size:15px; font-weight:600; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
    .modal-close { color:#fff; font-size:22px; cursor:pointer; line-height:1; background:none; border:none; padding:0; height:auto; }
    .modal-close:hover { color:#ccc; }
    .modal-body { padding:16px; display:flex; flex-direction:column; gap:10px; overflow:auto; min-height:0; }
    .modal-body label { font-size:13px; font-weight:600; color:var(--muted); margin-bottom:2px; display:block; }
    .modal-body input { width:100%; padding:8px 10px; background:var(--panel2); color:var(--text); border:1px solid var(--border); border-radius:5px; font-size:13px; }
    .modal-buttons { display:flex; gap:8px; justify-content:flex-end; margin-top:6px; }

    /* Tabs — inline in header, left side */
    .tabs { display:flex; gap:0; }
    .tab { padding:8px 16px; cursor:pointer; font-size:13px; font-weight:600; color:var(--muted); border-bottom:2px solid transparent; transition:all .15s; }
    .tab:hover { color:var(--text); }
    .tab.active { color:var(--primary); border-bottom-color:var(--primary); }

    /* Panels */
    .tab-content { display:none; padding:16px; }
    .tab-content.active { display:block; }

    /* Filter bar */
    .filter-bar { display:flex; gap:8px; align-items:center; flex-wrap:wrap; }
    .filter-bar input, .filter-bar select { height:28px; background:var(--panel2); color:var(--text); border:1px solid var(--border); border-radius:5px; padding:0 8px; font-size:13px; }
    .filter-bar input { width:240px; }
    .filter-bar select { width:100px; }
    .select-with-add { position:relative; display:inline-flex; align-items:center; }
    .select-with-add select { padding-right:26px; }
    .select-add-btn { position:absolute; right:2px; top:50%; transform:translateY(-50%); background:none; border:0; color:var(--muted); font-size:14px; padding:0 5px; line-height:1; cursor:pointer; height:24px; min-width:20px; }
    .select-add-btn:hover { color:var(--text); background:transparent; }

    /* Issue cards */
    .issue-card { border:1px solid var(--border); border-radius:8px; padding:14px; margin-bottom:14px; background:#101620; }
    .issue-card h3 { margin:0 0 10px; font-size:16px; display:flex; align-items:center; gap:8px; }
    .issue-card h3 a { color:#7bb0ff; text-decoration:none; }
    .issue-card h3 a:hover { text-decoration:underline; }

    .chips { display:flex; gap:6px; flex-wrap:wrap; margin:6px 0 10px; }
    .chip { font-size:11px; color:#dbeafe; background:#1d3558; padding:3px 7px; border-radius:999px; }
    .chip.high { background:#5c1d1d; color:#fca5a5; }
    .chip.medium { background:#5c3a0a; color:#fde68a; }
    .chip.ok { background:#14412a; color:#86efac; }

    .field { margin-bottom:12px; }
    .field-label { font-size:12px; font-weight:700; color:var(--muted); margin-bottom:4px; text-transform:uppercase; letter-spacing:.5px; }
    .field-content { font-size:14px; line-height:1.6; }
    .field-content.error-section { background:#1a0a0a; border:1px solid #3b1111; border-radius:5px; padding:8px 10px; font-family:'Cascadia Code','Fira Code',monospace; font-size:12px; white-space:pre-wrap; word-break:break-word; color:#fca5a5; }
    .field-content.code-section { background:#0a0f1a; border:1px solid #112040; border-radius:5px; padding:8px 10px; font-family:'Cascadia Code','Fira Code',monospace; font-size:12px; white-space:pre-wrap; word-break:break-word; color:#93c5fd; }
    .field-content.solution-section { background:#0a1a0e; border:1px solid #113320; border-radius:5px; padding:8px 10px; font-size:13px; }

    /* Code block containers with syntax highlighting */
    .code-block { position:relative; background:#0a0f1a; border:1px solid #1a2540; border-radius:6px; margin:6px 0; overflow:hidden; }
    .code-block pre { margin:0; padding:10px 12px; border:none; background:transparent; font-family:'Cascadia Code','Fira Code','JetBrains Mono',monospace; font-size:12px; line-height:1.5; white-space:pre-wrap; word-break:break-word; color:#c9d1d9; }
    .code-block-lang { display:inline-block; background:#1a2540; color:#7bb0ff; font-size:10px; font-weight:700; padding:2px 8px; border-radius:0 0 4px 0; text-transform:uppercase; letter-spacing:.5px; }
    .diff-block { border-color:#1a2540; }
    .diff-header { color:#79c0ff; font-weight:700; }
    .diff-hunk { color:#39d353; background:rgba(57,211,83,.08); display:inline-block; width:100%; }
    .diff-add { color:#3fb950; background:rgba(63,185,80,.08); display:inline-block; width:100%; }
    .diff-remove { color:#f85149; background:rgba(248,81,73,.08); display:inline-block; width:100%; }
    .shell-block { border-color:#1a2540; }
    .shell-cmd { color:#f0c674; font-weight:600; }
    .copy-btn { position:absolute; top:4px; right:4px; background:#1a2540; color:#7bb0ff; border:1px solid #2a3550; border-radius:4px; padding:2px 8px; font-size:11px; cursor:pointer; opacity:0; transition:opacity .2s; }
    .code-block:hover .copy-btn { opacity:1; }
    .copy-btn:hover { background:#2a3550; }

    /* Info table for platform & version — single row */
    .info-table { width:100%; border-collapse:collapse; margin:4px 0; font-size:13px; }
    .info-table th { text-align:left; color:var(--muted); font-weight:600; font-size:11px; padding:5px 8px; background:var(--panel2); border:1px solid var(--border); white-space:nowrap; }
    .info-table td { padding:5px 10px; border:1px solid var(--border); white-space:nowrap; }
    .info-table td strong { color:#93c5fd; }

    .test-info { padding:6px 0; font-size:13px; color:#93c5fd; border-bottom:1px solid var(--border); margin-bottom:8px; }
    .ref-item { display:flex; align-items:flex-start; gap:8px; padding:4px 0; font-size:13px; }
    .ref-item a { color:#7bb0ff; text-decoration:none; font-weight:600; white-space:nowrap; flex-shrink:0; }
    .ref-item a:hover { text-decoration:underline; }
    .ref-title { color:var(--text); font-size:13px; line-height:1.4; }
    .ref-badge { font-size:11px; padding:2px 6px; border-radius:999px; font-weight:600; flex-shrink:0; }
    .ref-badge.high { background:#5c1d1d; color:#fca5a5; }
    .ref-badge.medium { background:#5c3a0a; color:#fde68a; }
    .ref-badge.low { background:#2a2a2a; color:#9ca3af; }

    /* Run list */
    .run-item { padding:10px 12px; border-bottom:1px solid var(--border); cursor:pointer; }
    .run-item:hover { background:#1b2230; }
    .run-item.active { background:#1f2d45; }
    .run-item-title { font-size:13px; font-weight:700; margin-bottom:4px; }
    .run-item-meta { font-size:12px; color:var(--muted); }

    /* Pagination */
    .pagination { display:flex; justify-content:center; gap:8px; margin:16px 0; }
    .pagination button { min-width:32px; }

    /* Stats */
    .stats-grid { display:grid; grid-template-columns:repeat(auto-fit, minmax(180px, 1fr)); gap:12px; margin-bottom:20px; }
    .stat-card { background:var(--panel); border:1px solid var(--border); border-radius:8px; padding:14px; text-align:center; }
    .stat-card .value { font-size:28px; font-weight:700; color:var(--primary); }
    .stat-card .label { font-size:12px; color:var(--muted); margin-top:4px; }
    .stat-card.warn .value { color:var(--warn); }
    .stat-card.bad .value { color:var(--bad); }
    .stat-card.ok .value { color:var(--ok); }
    .clickable-stat { cursor:pointer; transition:transform .15s,box-shadow .15s; }
    .clickable-stat:hover { transform:translateY(-2px); box-shadow:0 4px 12px rgba(0,0,0,.3); }
    .stats-section { margin-bottom:18px; }
    .stats-section[id], .dept-user-block[id], .project-user-block[id] { scroll-margin-top:86px; }
    .stats-section h2 { font-size:15px; margin:0 0 10px; }
    .dashboard-summary-header { display:flex; justify-content:space-between; align-items:center; margin-bottom:10px; gap:12px; flex-wrap:wrap; min-height:30px; }
    .dashboard-summary-title { margin:0; font-size:15px; line-height:30px; }
    .dashboard-summary-controls { display:flex; align-items:center; gap:8px; margin-left:auto; flex-wrap:wrap; min-height:30px; }
    .dashboard-summary-meta { font-size:12px; min-height:30px; display:flex; align-items:center; }
    .trend-grid { display:grid; grid-template-columns:repeat(auto-fit, minmax(380px, 1fr)); gap:12px; margin-bottom:18px; }
    .trend-panel { background:var(--panel); border:1px solid var(--border); border-radius:8px; padding:12px; min-height:180px; display:flex; flex-direction:column; }
    .trend-panel h3 { margin:0; font-size:14px; flex-shrink:0; line-height:30px; }
    .trend-title-row { display:flex; align-items:center; justify-content:space-between; gap:8px; margin-bottom:10px; min-height:30px; }
    .trend-start-btn { width:30px; min-width:30px; padding:0; background:#30394a; color:var(--muted); }
    .trend-start-btn:hover { color:var(--text); }
    .trend-body { overflow-y:auto; max-height:330px; flex:1; }
    .trend-body::-webkit-scrollbar { width:6px; }
    .trend-body::-webkit-scrollbar-track { background:var(--panel2); border-radius:3px; }
    .trend-body::-webkit-scrollbar-thumb { background:var(--border); border-radius:3px; }
    .trend-body::-webkit-scrollbar-thumb:hover { background:var(--muted); }
    .bar-row { display:flex; align-items:center; gap:6px; margin:4px 0; font-size:12px; padding-right:8px; }
    .bar-label { flex:0 0 auto; min-width:0; max-width:86px; color:var(--muted); white-space:nowrap; overflow:hidden; text-overflow:ellipsis; padding-right:8px; }
    .bar-track { flex:1; height:8px; background:#202838; border-radius:999px; overflow:hidden; min-width:40px; }
    .bar-fill { height:100%; min-width:3px; background:var(--primary); border-radius:999px; }
    .bar-count { flex-shrink:0; width:28px; text-align:right; color:var(--muted); }
    .issue-mini-list { display:grid; gap:8px; }
    .issue-mini { display:grid; grid-template-columns:92px minmax(0, 1fr) 128px; gap:10px; align-items:start; padding:10px 12px; background:#101620; border:1px solid var(--border); border-radius:8px; font-size:13px; }
    .issue-mini a { color:#7bb0ff; text-decoration:none; font-weight:700; }
    .issue-mini a:hover { text-decoration:underline; }
    .issue-mini-title { min-width:0; }
    .issue-mini-title strong { display:block; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; color:var(--text); font-weight:600; }
    .issue-mini-title span { display:block; color:var(--muted); font-size:12px; line-height:1.5; margin-top:2px; }
    .issue-mini-meta { color:var(--muted); font-size:12px; text-align:right; line-height:1.5; }
    .dept-toolbar { display:flex; justify-content:space-between; align-items:center; gap:12px; margin-bottom:14px; flex-wrap:wrap; }
    .dept-table-wrap { overflow:auto; border:1px solid var(--border); border-radius:8px; margin-bottom:18px; }
    .dept-table { width:100%; border-collapse:collapse; min-width:760px; font-size:13px; }
    .dept-table th { text-align:left; color:var(--muted); font-weight:600; font-size:12px; padding:8px 10px; background:var(--panel2); border-bottom:1px solid var(--border); white-space:nowrap; }
    .dept-table td { padding:9px 10px; border-bottom:1px solid var(--border); white-space:nowrap; }
    .dept-table tr:last-child td { border-bottom:0; }
    .dept-table tbody tr:hover { background:#111927; }
    .dept-table a { color:#7bb0ff; text-decoration:none; font-weight:700; }
    .dept-table a:hover { text-decoration:underline; }
    .dept-action-btn { padding:0 10px; text-align:center; white-space:nowrap; }
    /* Keep the person column compact so numeric columns don't get squeezed. */
    .dept-table .col-person { width:1%; white-space:nowrap; }
    .dept-table td.col-person { padding-right:18px; }
    #trendDetailBody { max-height:calc(85vh - 62px); }
    .toggle-btn.active { background:var(--primary); color:#fff; }
    .project-filter-th { width:132px; min-width:132px; text-align:right !important; }
    .project-filter-cell { width:132px; min-width:132px; }
    .dept-user-block { margin-bottom:18px; }
    .dept-user-title { display:flex; justify-content:space-between; align-items:center; gap:10px; margin:0 0 8px; }
    .dept-user-title h2 { margin:0; font-size:15px; }

    /* Layout */
    .two-col { display:grid; grid-template-columns:340px minmax(0,1fr); gap:12px; min-height:calc(100vh - 160px); }
    .panel { background:var(--panel); border:1px solid var(--border); border-radius:8px; overflow:hidden; }
    .panel h2 { margin:0; padding:10px 12px; font-size:14px; background:var(--panel2); border-bottom:1px solid var(--border); }
    .panel .scroll { overflow:auto; max-height:calc(100vh - 220px); }

    details { margin-top:6px; }
    details summary { cursor:pointer; font-size:13px; color:var(--muted); font-weight:600; }
    pre { white-space:pre-wrap; word-break:break-word; background:#090c11; border:1px solid var(--border); border-radius:6px; padding:10px; color:#d7e1f5; font-size:12px; }

    @media (max-width:900px) { header { grid-template-columns:1fr; row-gap:8px; } .tabs { margin-left:0; width:100%; } header .filter-bar { width:100%; min-width:0; } header .filter-bar input { width:140px; min-width:140px; } header .btn-group { width:100%; min-width:0; justify-content:flex-start; } .two-col { grid-template-columns:1fr; } .issue-mini { grid-template-columns:1fr; } .issue-mini-meta { text-align:left; } }
  </style>
</head>
<body>
<header>
  <div class="tabs">
    <div class="tab" data-tab="department" onclick="switchTab('department')">🏢 部门看板</div>
    <div class="tab active" data-tab="stats" onclick="switchTab('stats')">📈 个人看板</div>
    <div class="tab" data-tab="project" onclick="switchTab('project')">🧩 项目看板</div>
    <div class="tab" data-tab="issues" onclick="switchTab('issues')">📋 工单列表</div>
    <div class="tab" data-tab="runs" onclick="switchTab('runs')">📊 扫描记录</div>
  </div>
  <div class="header-spacer"></div>
  <div class="filter-bar" style="margin:0">
    <input type="text" id="searchInput" placeholder="搜索工单 / 输入 #工单号查询Redmine..." onkeydown="if(event.key==='Enter')smartSearch()" style="width:260px">
    <select id="statusFilter" onchange="loadIssues()">
      <option value="">全部状态</option>
      <option value="新建">新建</option>
      <option value="进行中">进行中</option>
      <option value="已解决">已解决</option>
      <option value="已关闭">已关闭</option>
      <option value="反馈">反馈</option>
    </select>
    <select id="priorityFilter" onchange="loadIssues()">
      <option value="">全部优先级</option>
      <option value="紧急">紧急</option>
      <option value="高">高</option>
      <option value="正常">正常</option>
      <option value="低">低</option>
    </select>
  </div>
  <div class="btn-group">
    <button id="scanBtn" onclick="startScan()">🔍 扫描</button>
    <button class="secondary" onclick="triggerSync()">🔄 同步</button>
    <button class="secondary" onclick="refreshCurrentTab()">刷新</button>
    <button class="secondary" onclick="showSettingsModal()" title="设置">⚙️</button>
  </div>
</header>

<div id="tab-issues" class="tab-content">
  <div id="issuesList"></div>
  <div id="issuesPagination" class="pagination"></div>
</div>

<div id="tab-runs" class="tab-content">
  <div class="two-col">
    <section class="panel">
      <h2>扫描记录</h2>
      <div id="runsList" class="scroll"></div>
    </section>
    <section class="panel">
      <h2 id="runDetailTitle">日报详情</h2>
      <div id="runDetail" class="scroll" style="padding:14px;"><div class="muted">选择一次扫描记录查看结果。</div></div>
    </section>
  </div>
</div>

<div id="tab-stats" class="tab-content active">
  <div id="statsContent"><div class="muted">加载中...</div></div>
</div>

<div id="tab-department" class="tab-content">
  <div id="departmentContent"><div class="muted">加载中...</div></div>
</div>

<div id="tab-project" class="tab-content">
  <div id="projectContent"><div class="muted">加载中...</div></div>
</div>

<!-- Add User Modal -->
<div id="addUserModal" class="modal">
  <div class="modal-content">
    <div class="modal-header">
      <span class="modal-title">添加用户</span>
      <span class="modal-close" onclick="hideAddUserModal()">&times;</span>
    </div>
    <div class="modal-body">
      <div>
        <label>Redmine 用户 ID</label>
        <input type="number" id="addUserId" placeholder="例如：8912">
      </div>
      <div>
        <label>用户姓名</label>
        <input type="text" id="addUserName" placeholder="例如：张三">
      </div>
      <div>
        <label>邮箱</label>
        <input type="email" id="addUserEmail" placeholder="例如：zhangsan@example.com">
      </div>
      <div>
        <label>所属部门</label>
        <div class="select-with-add" style="width:100%">
          <select id="addUserDepartment" style="width:100%"></select>
          <button class="select-add-btn" type="button" onclick="showAddDepartmentModal('addUserDepartment')" title="添加部门">＋</button>
        </div>
      </div>
      <div class="modal-buttons">
        <button class="secondary" onclick="hideAddUserModal()">取消</button>
        <button onclick="submitAddUser()">确定</button>
      </div>
    </div>
  </div>
</div>

<!-- Add Department Modal -->
<div id="addDepartmentModal" class="modal">
  <div class="modal-content">
    <div class="modal-header">
      <span class="modal-title">添加部门</span>
      <span class="modal-close" onclick="hideAddDepartmentModal()">&times;</span>
    </div>
    <div class="modal-body">
      <div>
        <label>部门名称</label>
        <input type="text" id="addDepartmentName" placeholder="例如：系统三部">
      </div>
      <div>
        <label>部门 ID</label>
        <input type="text" id="addDepartmentId" placeholder="可选，例如：system-3">
        <div style="font-size:11px;color:var(--muted);margin-top:2px">留空时自动生成 dept-N</div>
      </div>
      <div class="modal-buttons">
        <button class="secondary" onclick="hideAddDepartmentModal()">取消</button>
        <button onclick="submitAddDepartment()">确定</button>
      </div>
    </div>
  </div>
</div>

<!-- Add Project Modal -->
<div id="addProjectModal" class="modal">
  <div class="modal-content">
    <div class="modal-header">
      <span class="modal-title">添加项目</span>
      <span class="modal-close" onclick="hideAddProjectModal()">&times;</span>
    </div>
    <div class="modal-body">
      <div>
        <label>项目名称</label>
        <input type="text" id="addProjectName" placeholder="例如：RK3572 Android 16 SDK">
      </div>
      <div>
        <label>项目标识或 URL</label>
        <input type="text" id="addProjectId" placeholder="例如：https://redmine.rock-chips.com/projects/rk3572-android-16-sdk">
      </div>
      <div class="modal-buttons">
        <button class="secondary" onclick="hideAddProjectModal()">取消</button>
        <button onclick="submitAddProject()">确定</button>
      </div>
    </div>
  </div>
</div>

<!-- Settings Modal -->
<div id="settingsModal" class="modal">
  <div class="modal-content">
    <div class="modal-header" style="background:linear-gradient(135deg,#3b82f6,#6366f1)">
      <span class="modal-title">⚙️ 统计设置</span>
      <span class="modal-close" onclick="hideSettingsModal()">&times;</span>
    </div>
    <div class="modal-body">
      <div>
        <label>未回复天数阈值 (stale_days)</label>
        <input type="number" id="settingStaleDays" min="1" max="30" placeholder="默认 20">
        <div style="font-size:11px;color:var(--muted);margin-top:2px">超过/达到此天数未回复的工单标记为过期</div>
      </div>
      <div>
        <label>统计时间窗口 (window_days)</label>
        <input type="number" id="settingWindowDays" min="0" max="365" placeholder="0 = 不限制">
        <div style="font-size:11px;color:var(--muted);margin-top:2px">只统计最近 N 天内有活动的工单，0 表示不限制</div>
      </div>
      <div>
        <label>缓存有效期 (秒)</label>
        <input type="number" id="settingCacheTtl" min="0" max="3600" placeholder="600">
        <div style="font-size:11px;color:var(--muted);margin-top:2px">统计数据缓存时间，期间内重复访问直接返回缓存</div>
      </div>
      <hr style="border:0;border-top:1px solid var(--border);margin:12px 0">
      <div>
        <label>SMTP 服务器</label>
        <input type="text" id="settingSmtpHost" placeholder="例如：smtphz.qiye.163.com">
      </div>
      <div style="display:flex;gap:8px">
        <div style="flex:1">
          <label>端口</label>
          <input type="number" id="settingSmtpPort" placeholder="465">
        </div>
        <div style="flex:1">
          <label>发件人地址</label>
          <input type="text" id="settingFromAddr" placeholder="留空则使用默认发件人">
        </div>
      </div>
      <div style="display:flex;gap:8px">
        <div style="flex:1">
          <label>SMTP 用户名</label>
          <input type="text" id="settingSmtpUser" placeholder="chaoqun.huang@rock-chips.com">
        </div>
        <div style="flex:1">
          <label>SMTP授权码/密码</label>
          <input type="password" id="settingSmtpPass" placeholder="企业邮箱SMTP授权码或密码">
        </div>
      </div>
      <div class="modal-buttons">
        <button class="secondary" onclick="hideSettingsModal()">取消</button>
        <button onclick="saveSettings()">保存并刷新</button>
      </div>
    </div>
  </div>
</div>

<!-- Trend Start Date Modal -->
<div id="trendStartModal" class="modal">
  <div class="modal-content">
    <div class="modal-header" style="background:linear-gradient(135deg,#2563eb,#4f46e5)">
      <span class="modal-title" id="trendStartModalTitle">设置统计日期范围</span>
      <span class="modal-close" onclick="hideTrendStartModal()">&times;</span>
    </div>
    <div class="modal-body">
      <div>
        <label>起始日期</label>
        <input type="date" id="trendStartDateInput">
      </div>
      <div>
        <label>结束日期</label>
        <input type="date" id="trendEndDateInput">
        <div style="font-size:11px;color:var(--muted);margin-top:2px">清空后表示不限；仅填一侧日期也可以</div>
      </div>
      <div class="modal-buttons">
        <button class="secondary" onclick="clearTrendStartDate()">不限</button>
        <button class="secondary" onclick="hideTrendStartModal()">取消</button>
        <button onclick="saveTrendStartDate()">保存并刷新</button>
      </div>
    </div>
  </div>
</div>
<div id="trendDetailModal" class="modal">
  <div class="modal-content" style="max-width:900px">
    <div class="modal-header" style="background:linear-gradient(135deg,#2563eb,#4f46e5)">
      <span class="modal-title" id="trendDetailTitle">解决Redmine问题明细</span>
      <span class="modal-close" onclick="hideModal('trendDetailModal')">&times;</span>
    </div>
    <div class="modal-body" id="trendDetailBody"><div class="muted">加载中…</div></div>
  </div>
</div>

<script>
function scrollToSection(id) {
  var el = document.getElementById(id);
  if (!el) return;
  var header = document.querySelector('header');
  var offset = (header ? header.getBoundingClientRect().height : 0) + 14;
  var top = el.getBoundingClientRect().top + window.pageYOffset - offset;
  window.scrollTo({top: Math.max(0, top), behavior: 'smooth'});
}
let currentTab = 'stats';
let currentPage = 1;
const pageSize = 15;
let currentRunId = '';
let statsUserInitialized = false;
let statsConfig = {stale_days: 20, window_days: 60, cache_ttl: 600, redmine_base_url: 'https://redmine.rock-chips.com', dashboard: {profiles: [], defaults: {list_limit: 50, issue_limit: 500}}};
let departmentProfileId = '';
let projectProfileId = '';
// 趋势明细点击上下文：当前看板作用的指派人姓名列表（个人=[name]，部门=全员）
let redmineTrendNames = [];
function updateRedmineTrendNames(selectedName, meta) {
  if (selectedName) {
    redmineTrendNames = [selectedName];
    return;
  }
  redmineTrendNames = ((meta || {}).owner_names || []).map(function(name) {
    return String(name || '').trim();
  }).filter(Boolean);
}
let pendingDepartmentTargetSelect = '';
let pendingTrendChartKey = '';
let projectOpenOnly = false;

// ---- Load stats config from backend (cached 60s) ----
let _statsConfigCacheTs = 0;
async function loadStatsConfig() {
  if (statsConfig.stale_days && Date.now() - _statsConfigCacheTs < 60000) return;
  try {
    statsConfig = await api('/api/redmine-agent/config/stats');
    _statsConfigCacheTs = Date.now();
  } catch (_) {}
}

// ---- API helper ----
async function api(url, options) {
  const r = await fetch(url, options || {});
  const text = await r.text();
  let data = {};
  try {
    data = text ? JSON.parse(text) : {};
  } catch (e) {
    throw new Error((r.status ? 'HTTP ' + r.status + ': ' : '') + (text || e.message).slice(0, 180));
  }
  if (!r.ok) throw new Error(data.error || data.detail || ('HTTP ' + r.status));
  if (!data.success) throw new Error(data.error || '请求失败');
  return data.data || data;
}
function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
function trunc(s, n) {
  s = String(s || '');
  return s.length > n ? s.slice(0, n) + '...' : s;
}
function redmineBaseUrl() {
  return String(statsConfig.redmine_base_url || 'https://redmine.rock-chips.com').replace(new RegExp('/+$'), '');
}
function redmineIssueUrl(issueId) {
  return redmineBaseUrl() + '/issues/' + encodeURIComponent(String(issueId || '').trim());
}
function redmineIssueUrls(items) {
  return (items || []).map(function(item) { return item.issue_id || ''; }).filter(Boolean).map(redmineIssueUrl);
}
function departmentProfiles() {
  return ((statsConfig.dashboard || {}).profiles || []);
}
function projectProfiles() {
  return ((statsConfig.dashboard || {}).project_profiles || []);
}
function departmentOptionsHtml(selectedId, includeAll) {
  var profiles = departmentProfiles().filter(function(item) { return includeAll || item.id !== 'all'; });
  return profiles.map(function(item) {
    var selected = item.id === selectedId ? ' selected' : '';
    return '<option value="' + esc(item.id || '') + '"' + selected + '>' + esc(item.name || item.id || '-') + '</option>';
  }).join('');
}
function projectOptionsHtml(selectedId) {
  return projectProfiles().map(function(item) {
    var selected = item.id === selectedId ? ' selected' : '';
    return '<option value="' + esc(item.id || '') + '"' + selected + '>' + esc(item.name || item.project_id || '-') + '</option>';
  }).join('');
}

// ---- Formatted content rendering ----
var _BT = String.fromCharCode(96);
var _F3 = _BT+_BT+_BT;
var _NL = String.fromCharCode(10);
var _HTML_RE = /<pre><code(?:\s+class="(\w*)")?\s*>([\s\S]*?)<\/code><\/pre>/g;

function _nl2br(s) { return s.replace(new RegExp(_NL, 'g'), '<br>'); }

function renderFormattedContent(text, defaultClass) {
  if (!text) return '';
  var cls = defaultClass || 'field-content';
  var result = '';
  var parts = []; // {type:'text'|'code', content, lang}
  var lastIdx = 0;

  // 1. Extract HTML <pre><code class="lang">...</code></pre> blocks
  _HTML_RE.lastIndex = 0;
  var m;
  while ((m = _HTML_RE.exec(text)) !== null) {
    if (m.index > lastIdx) parts.push({type:'text', content:text.slice(lastIdx, m.index), lang:''});
    parts.push({type:'code', content:m[2]||'', lang:(m[1]||'').toLowerCase()});
    lastIdx = _HTML_RE.lastIndex;
  }
  if (lastIdx < text.length) parts.push({type:'text', content:text.slice(lastIdx), lang:''});

  // If no HTML blocks found, try markdown ```lang``` blocks
  if (parts.length <= 1 && parts[0] && parts[0].type === 'text') {
    parts = [];
    lastIdx = 0;
    var mdRe = new RegExp(_F3 + '(\\\\w*)' + _NL + '([\\\\s\\\\S]*?)' + _F3, 'g');
    var mm;
    while ((mm = mdRe.exec(text)) !== null) {
      if (mm.index > lastIdx) parts.push({type:'text', content:text.slice(lastIdx, mm.index), lang:''});
      parts.push({type:'code', content:mm[2]||'', lang:(mm[1]||'').toLowerCase()});
      lastIdx = mdRe.lastIndex;
    }
    if (lastIdx < text.length) parts.push({type:'text', content:text.slice(lastIdx), lang:''});
  }

  // If still no blocks, return escaped text
  if (!parts.length) return _nl2br(esc(text));

  // 2. Render each part
  for (var i = 0; i < parts.length; i++) {
    var p = parts[i];
    if (p.type === 'text') {
      if (p.content.trim()) result += '<div class="' + cls + '">' + _nl2br(esc(p.content)) + '</div>';
    } else {
      if (p.lang === 'diff') result += renderDiffBlock(p.content);
      else if (p.lang === 'shell' || p.lang === 'bash' || p.lang === 'sh') result += renderShellBlock(p.content);
      else result += renderGenericCodeBlock(p.content, p.lang);
    }
  }
  return result || _nl2br(esc(text));
}
function renderDiffBlock(code) {
  var lines = code.split(_NL).map(function(line) {
    var e = esc(line);
    if (line.startsWith('---') || line.startsWith('+++')) return '<span class="diff-header">' + e + '</span>';
    if (line.startsWith('@@')) return '<span class="diff-hunk">' + e + '</span>';
    if (line.startsWith('+')) return '<span class="diff-add">' + e + '</span>';
    if (line.startsWith('-')) return '<span class="diff-remove">' + e + '</span>';
    return e;
  }).join(_NL);
  return '<div class="code-block diff-block"><div class="code-block-lang">diff</div><pre><code>' + lines + '</code></pre><button class="copy-btn" onclick="copyCode(this)">复制</button></div>';
}
function renderShellBlock(code) {
  var lines = code.split(_NL).map(function(line) {
    var e = esc(line);
    if (/^\\$\\s/.test(line)) return '<span class="shell-cmd">' + e + '</span>';
    return e;
  }).join(_NL);
  return '<div class="code-block shell-block"><div class="code-block-lang">shell</div><pre><code>' + lines + '</code></pre><button class="copy-btn" onclick="copyCode(this)">复制</button></div>';
}
function renderGenericCodeBlock(code, lang) {
  var langLabel = lang || 'code';
  return '<div class="code-block"><div class="code-block-lang">' + esc(langLabel) + '</div><pre><code>' + esc(code) + '</code></pre><button class="copy-btn" onclick="copyCode(this)">复制</button></div>';
}
function copyCode(btn) {
  var code = btn.previousElementSibling.querySelector('code');
  navigator.clipboard.writeText(code.textContent).then(function() {
    btn.textContent = '已复制';
    setTimeout(function() { btn.textContent = '复制'; }, 1500);
  });
}
function copyText(text, btn) {
  navigator.clipboard.writeText(String(text || '')).then(function() {
    if (!btn) return;
    var old = btn.textContent;
    btn.textContent = '✓';
    setTimeout(function() { btn.textContent = old; }, 1500);
  });
}

// ---- Tab switching ----
function switchTab(tab) {
  currentTab = tab;
  try { window.sessionStorage.setItem('redmineLastTab', tab); } catch(_) {}
  document.querySelectorAll('.tab').forEach(t => t.classList.toggle('active', t.dataset.tab === tab));
  document.querySelectorAll('.tab-content').forEach(t => t.classList.toggle('active', t.id === 'tab-' + tab));
  if (tab === 'issues') loadIssues();
  else if (tab === 'runs') loadRuns();
  else if (tab === 'department') loadDepartmentOverdue(false);
  else if (tab === 'project') loadProjectDashboard(false);
  else if (tab === 'stats') loadStatistics();
}

async function refreshCurrentTab() {
  var refreshBtns = document.querySelectorAll('.btn-group .secondary');
  var targetBtn = null;
  refreshBtns.forEach(function(b) { if (b.textContent.includes('刷新')) targetBtn = b; });
  if (targetBtn) { targetBtn.disabled = true; targetBtn.textContent = '⏳ 刷新中...'; }
  try {
    await (currentTab === 'issues' ? loadIssues() : currentTab === 'runs' ? loadRuns() : currentTab === 'department' ? loadDepartmentOverdue(true) : currentTab === 'project' ? loadProjectDashboard(true) : loadStatistics());
  } finally {
    if (targetBtn) { targetBtn.disabled = false; targetBtn.textContent = '刷新'; }
  }
}

async function initStatsUserSelect() {
  if (statsUserInitialized) return;
  statsUserInitialized = true;
  var select = document.getElementById('statsUserSelect');
  if (!select) return;
  try {
    var data = await api('/api/redmine-agent/users');
    var items = (data.items || []).slice().sort(function(a, b) { return (a.name || '').localeCompare(b.name || ''); });
    select.innerHTML = '<option value="">当前登录用户</option>' + items.map(function(item) {
      var name = item.name || '';
      return '<option value="' + esc(name) + '">' + esc(name) + '</option>';
    }).join('');
    var q = new URLSearchParams(window.location.search);
    var name = q.get('name') || '';
    if (name) select.value = name;
  } catch (_) {}
}

async function onStatsUserChange() {
  var select = document.getElementById('statsUserSelect');
  var name = select ? select.value : '';
  var url = new URL(window.location.href);
  if (name) url.searchParams.set('name', name);
  else url.searchParams.delete('name');
  url.searchParams.set('tab', 'stats');
  window.history.replaceState({}, '', url.toString());
  if (select) select.disabled = true;
  document.getElementById('statsContent').innerHTML = '<div class="muted" style="padding:20px;text-align:center">⏳ 正在加载 ' + esc(name || '当前用户') + ' 的统计数据...</div>';
  try {
    await loadStatistics();
  } finally {
    if (select) select.disabled = false;
  }
}

// ---- Shared modal helpers ----
document.addEventListener('keydown', function(e) {
  if (e.key === 'Escape') document.querySelectorAll('.modal.show').forEach(function(m) { m.classList.remove('show'); });
});
function showModal(id) { document.getElementById(id).classList.add('show'); }
function hideModal(id) { document.getElementById(id).classList.remove('show'); }

// 趋势柱状图点击：粒度+标签 → 日期范围 [start, end)（ISO，闭开区间）
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
    var y = parseInt(label, 10); if (!y) return null;
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
async function showRedmineTrendDetail(granularity, label, namesCsv, profileId) {
  var range = trendLabelToDateRange(granularity, label);
  var title = document.getElementById('trendDetailTitle');
  var body = document.getElementById('trendDetailBody');
  if (!range || !title || !body) { alert('无法解析时段：' + label); return; }
  title.textContent = '解决Redmine问题明细：' + label + '（' + displayTrendRange(range) + '）';
  body.innerHTML = '<div class="muted">查询中…</div>';
  showModal('trendDetailModal');
  try {
    var names = String(namesCsv || '').trim();
    profileId = String(profileId || '').trim();
    if (!names && redmineTrendNames && redmineTrendNames.length) names = redmineTrendNames.join(',');
    var url = '/api/redmine-agent/statistics/resolved-by-date?start=' + encodeURIComponent(range[0])
      + '&end=' + encodeURIComponent(range[1])
      + (names ? '&names=' + encodeURIComponent(names) : '')
      + (profileId ? '&profile_id=' + encodeURIComponent(profileId) : '');
    var data = await api(url);
    var items = (data && data.items) || [];
    if (!items.length) { body.innerHTML = '<div class="muted">该时段无已解决的问题单。</div>'; return; }
    body.innerHTML = '<div class="muted" style="margin-bottom:8px">共 ' + items.length + ' 条</div><div class="wrap"><table class="dept-table"><thead><tr><th>#</th><th>主题</th><th>状态</th><th>指派人</th><th>解决日期</th></tr></thead><tbody>'
      + items.slice(0, 200).map(function(i) {
        var issueId = i.issue_id || '';
        var issueCell = issueId ? '<a href="' + redmineIssueUrl(issueId) + '" target="_blank">#' + esc(issueId) + '</a>' : '-';
        return '<tr><td>' + issueCell + '</td><td>' + esc((i.subject || '-').slice(0, 60)) + '</td><td>' + esc(i.status_name || '-') + '</td><td>' + esc(i.assigned_to_name || '-') + '</td><td>' + esc(i.closed_on || '-') + '</td></tr>';
      }).join('') + '</tbody></table></div>';
  } catch (e) {
    body.innerHTML = '<span class="error">' + esc(e.message) + '</span>';
  }
}

// ---- Add User Modal ----
async function populateDepartmentSelect(selectId, selectedId, includeAll) {
  await loadStatsConfig();
  var select = document.getElementById(selectId);
  if (!select) return;
  var html = departmentOptionsHtml(selectedId || '', includeAll);
  select.innerHTML = html || '<option value="">暂无部门</option>';
}
async function showAddUserModal() {
  document.getElementById('addUserId').value = '';
  document.getElementById('addUserName').value = '';
  document.getElementById('addUserEmail').value = '';
  var selected = (currentTab === 'department' && departmentProfileId && departmentProfileId !== 'all') ? departmentProfileId : '';
  await populateDepartmentSelect('addUserDepartment', selected, false);
  showModal('addUserModal');
  document.getElementById('addUserId').focus();
}
function hideAddUserModal() { hideModal('addUserModal'); }
async function submitAddUser() {
  var id = document.getElementById('addUserId').value.trim();
  var name = document.getElementById('addUserName').value.trim();
  var email = document.getElementById('addUserEmail').value.trim();
  var profileId = (document.getElementById('addUserDepartment') || {}).value || '';
  if (!id || !name) { alert('请输入用户 ID 和姓名'); return; }
  try {
    await api('/api/redmine-agent/users', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({id: Number(id), name: name, email: email, profile_id: profileId})
    });
    hideModal('addUserModal');
    statsUserInitialized = false;
    _statsConfigCacheTs = 0;
    await initStatsUserSelect();
    document.getElementById('statsUserSelect').value = name;
    if (currentTab === 'department') loadDepartmentOverdue(true);
    else onStatsUserChange();
  } catch (e) { alert('添加失败: ' + e.message); }
}

// ---- Add Department Modal ----
function showAddDepartmentModal(targetSelectId) {
  pendingDepartmentTargetSelect = targetSelectId || 'departmentProfileSelect';
  document.getElementById('addDepartmentName').value = '';
  document.getElementById('addDepartmentId').value = '';
  showModal('addDepartmentModal');
  document.getElementById('addDepartmentName').focus();
}
function hideAddDepartmentModal() { hideModal('addDepartmentModal'); }
async function submitAddDepartment() {
  var name = document.getElementById('addDepartmentName').value.trim();
  var id = document.getElementById('addDepartmentId').value.trim();
  if (!name) { alert('请输入部门名称'); return; }
  try {
    var result = await api('/api/redmine-agent/dashboard/profiles', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({name: name, id: id})
    });
    hideAddDepartmentModal();
    _statsConfigCacheTs = 0;
    await loadStatsConfig();
    var profile = result.profile || {};
    if (pendingDepartmentTargetSelect === 'addUserDepartment') {
      await populateDepartmentSelect('addUserDepartment', profile.id || '', false);
    } else {
      departmentProfileId = profile.id || departmentProfileId;
      loadDepartmentOverdue(true);
    }
  } catch (e) { alert('添加部门失败: ' + e.message); }
}

// ---- Add Project Modal ----
function showAddProjectModal() {
  document.getElementById('addProjectName').value = '';
  document.getElementById('addProjectId').value = '';
  showModal('addProjectModal');
  document.getElementById('addProjectName').focus();
}
function hideAddProjectModal() { hideModal('addProjectModal'); }
async function submitAddProject() {
  var name = document.getElementById('addProjectName').value.trim();
  var projectId = document.getElementById('addProjectId').value.trim();
  if (!projectId) { alert('请输入项目标识或 URL'); return; }
  try {
    var result = await api('/api/redmine-agent/dashboard/projects', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({name: name, project_id: projectId})
    });
    hideAddProjectModal();
    _statsConfigCacheTs = 0;
    await loadStatsConfig();
    projectProfileId = (result.profile || {}).id || projectProfileId;
    loadProjectDashboard(true);
  } catch (e) { alert('添加项目失败: ' + e.message); }
}

// ---- Settings Modal ----
function showSettingsModal() {
  showModal('settingsModal');
  (async function() {
    try {
      await loadStatsConfig();
      document.getElementById('settingStaleDays').value = statsConfig.stale_days || 20;
      document.getElementById('settingWindowDays').value = statsConfig.window_days || 0;
      document.getElementById('settingCacheTtl').value = statsConfig.cache_ttl || 600;
      // SMTP fields from statsConfig (returned by get_stats_config)
      var cfg = await api('/api/redmine-agent/config/stats');
      var email = (cfg.dashboard || {}).email || {};
      document.getElementById('settingSmtpHost').value = email.smtp_host || '';
      document.getElementById('settingSmtpPort').value = email.smtp_port || 465;
      document.getElementById('settingFromAddr').value = email.from_addr || email.default_from_addr || '';
      document.getElementById('settingSmtpUser').value = email.username || '';
      document.getElementById('settingSmtpPass').value = '';
    } catch (_) {}
  })();
}
function hideSettingsModal() { hideModal('settingsModal'); }
async function saveSettings() {
  var stale = parseInt(document.getElementById('settingStaleDays').value) || 20;
  var window_ = parseInt(document.getElementById('settingWindowDays').value) || 60;
  var cacheTtl = parseInt(document.getElementById('settingCacheTtl').value) || 600;
  try {
    // Save stats config
    var result = await api('/api/redmine-agent/config/stats', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({stale_days: stale, window_days: window_, cache_ttl: cacheTtl})
    });
    if (result) { statsConfig = Object.assign({}, statsConfig, result); _statsConfigCacheTs = Date.now(); }
    // Save SMTP config
    var smtpHost = document.getElementById('settingSmtpHost').value.trim();
    var smtpPort = parseInt(document.getElementById('settingSmtpPort').value) || 465;
    var fromAddr = document.getElementById('settingFromAddr').value.trim();
    var smtpUser = document.getElementById('settingSmtpUser').value.trim();
    var smtpPass = document.getElementById('settingSmtpPass').value;
    await api('/api/redmine-agent/config/email', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({smtp_host: smtpHost, smtp_port: smtpPort, from_addr: fromAddr, username: smtpUser, password: smtpPass})
    });
    _statsConfigCacheTs = 0; // force reload
    hideSettingsModal();
    refreshCurrentTab();
  } catch (e) { alert('保存失败: ' + e.message); }
}

// ---- Smart search: detect issue ID and fetch from Redmine ----
async function smartSearch() {
  var q = document.getElementById('searchInput').value.trim();
  if (!q) { loadIssues(); return; }
  // Detect issue ID pattern: #634227, 634227, or pure number
  var idMatch = q.match(/^#?(\d{4,})$/);
  if (idMatch) {
    var issueId = parseInt(idMatch[1]);
    // Check local DB first
    try {
      var local = await api('/api/redmine-agent/issues/' + issueId);
      if (local && local.issue_id) {
        loadIssues();
        return;
      }
    } catch (_) {
      // Not found locally — fetch from Redmine
    }
    // Fetch from Redmine
    await fetchIssueFromRedmine(issueId);
  } else {
    loadIssues();
  }
}

async function fetchIssueFromRedmine(issueId) {
  var btn = document.getElementById('scanBtn');
  var origText = btn.textContent;
  btn.disabled = true;
  btn.textContent = '⏳ 拉取 #' + issueId + '...';
  try {
    var result = await api('/api/redmine-agent/issues/' + issueId + '/fetch', {method: 'POST'});
    if (result.action === 'exists') {
      document.getElementById('searchInput').value = '';
      loadIssues();
      return;
    }
    // Wait for analysis to complete
    btn.textContent = '⏳ 分析 #' + issueId + '...';
    await waitForRun(result.run_id);
    document.getElementById('searchInput').value = '';
    loadIssues();
  } catch (e) {
    alert('拉取工单失败: ' + e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = origText;
  }
}

// ---- Issues list ----
async function loadIssues(page) {
  if (page) currentPage = page;
  const search = document.getElementById('searchInput').value.trim();
  const status = document.getElementById('statusFilter').value;
  const priority = document.getElementById('priorityFilter').value;
  const offset = (currentPage - 1) * pageSize;
  let url = `/api/redmine-agent/issues?limit=${pageSize}&offset=${offset}`;
  if (search) url += `&search=${encodeURIComponent(search)}`;
  if (status) url += `&status=${encodeURIComponent(status)}`;
  if (priority) url += `&priority=${encodeURIComponent(priority)}`;
  try {
    const data = await api(url);
    renderIssuesList(data.items || []);
    renderPagination(data.total || 0, data.limit, data.offset);
  } catch (e) {
    document.getElementById('issuesList').innerHTML = `<div class="muted">加载失败: ${esc(e.message)}</div>`;
  }
}

function renderIssuesList(issues) {
  const box = document.getElementById('issuesList');
  if (!issues.length) {
    box.innerHTML = '<div class="muted" style="padding:20px">暂无工单数据。点击"全量同步"按钮拉取所有指派给你的 Redmine 工单。</div>';
    return;
  }
  box.innerHTML = issues.map(renderIssueCard).join('');
}

function renderIssueCard(item) {
  const refs = item.references_json || [];
  const failures = item.failures_json || [];
  const ai = item.ai_json || {};

  // Extract seven fields
  const title = esc(item.subject || ai.title || '-');
  const problemDesc = esc(item.problem_description || item.description || '-');
  const errorInfoRaw = item.error_info || _extractErrorHtml(failures) || '-';
  const errorAnalysis = esc(item.error_analysis || ai.root_cause_guess || '-');
  const solutionRaw = item.solution || ai.solution || '-';
  const patchRaw = item.patch_direction || ai.patch_direction || '-';

  const statusClass = ['已关闭','Closed','已解决','Resolved'].includes(item.status_name) ? 'ok' :
                      ['紧急','Urgent'].includes(item.priority_name) ? 'high' :
                      ['高','High'].includes(item.priority_name) ? 'medium' : '';

  // Build combined error info: test module/case + error stack trace in one code block
  var errorInfoCombined = errorInfoRaw;
  if (failures && failures.length) {
    var f0 = failures[0];
    var header = '';
    if (f0.module) header += '测试模块: ' + f0.module + _NL;
    if (f0.name) header += '测试用例: ' + f0.name + _NL;
    if (header) header += _NL;
    // Prepend test info before the error code block
    // If errorInfoRaw starts with ```, insert after the opening fence
    if (errorInfoRaw.startsWith(_F3) || errorInfoRaw.startsWith(_BT+_BT+_BT)) {
      // Find the first newline after ```
      var nlIdx = errorInfoRaw.indexOf(_NL);
      if (nlIdx > 0) {
        errorInfoCombined = errorInfoRaw.substring(0, nlIdx + 1) + header + errorInfoRaw.substring(nlIdx + 1);
      } else {
        errorInfoCombined = header + errorInfoRaw;
      }
    } else {
      errorInfoCombined = header + errorInfoRaw;
    }
  }

  // Build references HTML — full display, no truncation
  let refsHtml = '';
  if (refs.length) {
    refsHtml = refs.map(r => {
      const level = r.similarity_level || 'low';
      const score = (r.score || 0).toFixed(0);
      const levelText = level === 'high' ? '高' : level === 'medium' ? '中' : '低';
      return `<div class="ref-item">
        <a href="${redmineIssueUrl(r.issue_id)}" target="_blank">#${r.issue_id}</a>
        <span class="ref-badge ${level}">${levelText} ${score}</span>
        <span class="ref-title">${esc(r.subject || '')}</span>
      </div>`;
    }).join('');
  } else {
    refsHtml = '<div class="muted">暂无参考单</div>';
  }

  // Detect issue type: GMS certification or SDK platform
  var issueType = 'SDK';
  var comp = (item.component || '').toUpperCase();
  var cat = (item.category || '').toUpperCase();
  var fv = (item.fixed_version || '').toUpperCase();
  if (comp.includes('GMS') || cat.includes('GMS') || fv.includes('GMS')) issueType = 'GMS';

  // Detect status display
  var statusName = item.status_name || '-';
  var statusIcon = '';
  if (['已关闭','Closed'].includes(statusName)) statusIcon = '✅ ';
  else if (['已解决','Resolved'].includes(statusName)) statusIcon = '✓ ';
  else if (['新建','New'].includes(statusName)) statusIcon = '🆕 ';
  var isClosed = ['已关闭','Closed','已解决','Resolved'].includes(statusName);

  return `<div class="issue-card">
    <h3>
      <a href="${redmineIssueUrl(item.issue_id)}" target="_blank">#${item.issue_id}</a>
      <span>${title}</span>
      <span style="margin-left:auto;font-size:12px;color:var(--muted)">${esc(item.priority_name || '-')}</span>
    </h3>

    <div class="field-label">📋 基本信息</div>
    <table class="info-table">
      <tr>
        <th>SoC</th><td><strong>${esc(item.soc_platform || '-')}</strong></td>
        <th>Android</th><td><strong>${esc(item.android_version || '-')}</strong></td>
        <th>类型</th><td>${esc(issueType)}</td>
        <th>分类</th><td>${esc(item.category || '-')}</td>
        <th>状态</th><td>${statusIcon}${esc(statusName)}</td>
        <th>指派</th><td>${esc(item.assigned_to_name || '-')}</td>
        <th>创建</th><td>${esc((item.created_on || '-').slice(0, 10))}</td>
      </tr>
    </table>

    <div class="field">
      <div class="field-label">📝 问题描述</div>
      <div class="field-content">${trunc(problemDesc, 500)}</div>
    </div>

    <div class="field">
      <div class="field-label">🔴 报错信息</div>
      ${renderFormattedContent(trunc(errorInfoCombined, 2000), 'field-content error-section')}
    </div>

    <div class="field">
      <div class="field-label">🔍 报错分析</div>
      <div class="field-content">${trunc(errorAnalysis, 800)}</div>
    </div>

    <div class="field">
      <div class="field-label">✅ 解决方案</div>
      <div class="field-content solution-section">${renderFormattedContent(trunc(solutionRaw, 1500), 'field-content solution-section')}</div>
    </div>

    ${patchRaw && patchRaw !== '-' && patchRaw !== '需要进一步分析具体日志和源码' ? `<div class="field">
      <div class="field-label">🔧 解决补丁</div>
      ${renderFormattedContent(patchRaw, 'field-content')}
    </div>` : ''}

    ${refs.length ? `<div class="field">
      <div class="field-label">📎 参考Redmine</div>
      ${refsHtml}
    </div>` : ''}

    <details><summary>📄 完整文档</summary><div class="formatted-doc">${renderFormattedContent(item.doc_content || '', 'field-content')}</div></details>
  </div>`;
}

function _extractErrorHtml(failures) {
  if (!failures || !failures.length) return '';
  return failures.slice(0, 3).map(f => `[${f.module || '-'}] ${f.name || '-'}: ${trunc(f.reason || '', 200)}`).join(_NL);
}

function renderPagination(total, limit, offset) {
  const box = document.getElementById('issuesPagination');
  const pages = Math.ceil(total / limit);
  const current = Math.floor(offset / limit) + 1;
  if (pages <= 1) { box.innerHTML = `<div class="muted">共 ${total} 条</div>`; return; }
  let html = '';
  if (current > 1) html += `<button onclick="loadIssues(${current-1})">上一页</button>`;
  html += `<span class="muted" style="line-height:32px">第 ${current}/${pages} 页 (共${total}条)</span>`;
  if (current < pages) html += `<button onclick="loadIssues(${current+1})">下一页</button>`;
  box.innerHTML = html;
}

// ---- Runs ----
async function loadRuns() {
  try {
    const data = await api('/api/redmine-agent/runs?limit=30');
    const items = data.items || [];
    const box = document.getElementById('runsList');
    box.innerHTML = items.map(run => `
      <div class="run-item ${run.run_id === currentRunId ? 'active' : ''}" onclick="loadRun('${esc(run.run_id)}')">
        <div class="run-item-title">${esc(run.started_at || run.run_id)}</div>
        <div class="run-item-meta">${esc(run.status)} | mode=${esc(run.mode)} | issues ${run.issue_count || 0} | done ${run.processed_count || 0}</div>
      </div>`).join('') || '<div class="muted" style="padding:12px">暂无扫描记录</div>';
    if (!currentRunId && items.length) loadRun(items[0].run_id);
  } catch (e) {
    document.getElementById('runsList').innerHTML = `<div class="muted">加载失败: ${esc(e.message)}</div>`;
  }
}

async function loadRun(runId) {
  currentRunId = runId;
  try {
    const data = await api('/api/redmine-agent/runs/' + encodeURIComponent(runId));
    document.getElementById('runDetailTitle').textContent = '日报详情 ' + runId;
    const issues = data.issues || [];
    document.getElementById('runDetail').innerHTML = `
      <div class="muted">状态: ${esc(data.run.status)} | 报告: ${esc(data.run.report_path || '-')}</div>
      <div style="height:10px"></div>
      ${issues.map(renderIssueCard).join('') || '<div class="muted">没有扫描到问题。</div>'}`;
    loadRuns();
  } catch (e) {
    document.getElementById('runDetail').innerHTML = `<div class="muted">加载失败: ${esc(e.message)}</div>`;
  }
}

// ---- Statistics ----
function trendStartDate(chartKey) {
  return ((trendDateRange(chartKey) || {}).start || '').trim();
}
function trendEndDate(chartKey) {
  return ((trendDateRange(chartKey) || {}).end || '').trim();
}
function trendDateRange(chartKey) {
  var ranges = statsConfig.chart_date_ranges || {};
  if (ranges[chartKey]) return ranges[chartKey] || {};
  var legacy = ((statsConfig.chart_start_dates || {})[chartKey] || '').trim();
  return legacy ? {start: legacy} : {};
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
async function setTrendStartDate(chartKey, title) {
  pendingTrendChartKey = chartKey || '';
  document.getElementById('trendStartModalTitle').textContent = title + ' 日期范围';
  document.getElementById('trendStartDateInput').value = trendStartDate(chartKey);
  document.getElementById('trendEndDateInput').value = trendEndDate(chartKey);
  showModal('trendStartModal');
  setTimeout(function() {
    var input = document.getElementById('trendStartDateInput');
    if (!input) return;
    input.focus();
    if (typeof input.showPicker === 'function') {
      try { input.showPicker(); } catch (_) {}
    }
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
  var ranges = Object.assign({}, statsConfig.chart_date_ranges || {});
  if (start || end) ranges[chartKey] = Object.assign({}, start ? {start: start} : {}, end ? {end: end} : {});
  else delete ranges[chartKey];
  try {
    var result = await api('/api/redmine-agent/config/stats', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({chart_date_ranges: ranges})
    });
    statsConfig = Object.assign({}, statsConfig, result);
    _statsConfigCacheTs = Date.now();
    hideTrendStartModal();
    refreshCurrentTab();
  } catch (e) {
    alert('保存起始时间失败: ' + e.message);
  }
}
function renderTrend(title, items, keyName, chartKey, detailNames, detailProfileId) {
  chartKey = chartKey || title;
  const filtered = filterTrendItems(items || [], keyName, chartKey);
  const reversed = filtered.slice().reverse();
  const max = Math.max(1, ...reversed.map(item => Number(item.count || 0)));
  const rows = reversed.map(item => {
    const label = item[keyName] || '-';
    const count = Number(item.count || 0);
    const pct = Math.max(5, Math.round((count / max) * 100));
    const namesArg = Array.isArray(detailNames) ? detailNames.join(',') : String(detailNames || '');
    const profileArg = String(detailProfileId || '');
    const clickAttr = count > 0 ? ` style="cursor:pointer" onclick="showRedmineTrendDetail('${esc(keyName)}','${esc(String(label))}','${esc(namesArg)}','${esc(profileArg)}')" title="点击查看该时段解决的问题单"` : '';
    return `<div class="bar-row"${clickAttr}>
      <div class="bar-label">${esc(label)}</div>
      <div class="bar-track"><div class="bar-fill" style="width:${pct}%"></div></div>
      <div class="bar-count">${count}</div>
    </div>`;
  }).join('');
  var start = trendStartDate(chartKey);
  var end = trendEndDate(chartKey);
  var tip = (start || end) ? ('范围: ' + (start || '不限') + ' 至 ' + (end || '不限')) : '设置统计日期范围';
  return `<section class="trend-panel">
    <div class="trend-title-row">
      <h3>${esc(title)}</h3>
      <button class="trend-start-btn" onclick="setTrendStartDate('${esc(chartKey)}','${esc(title)}')" title="${esc(tip)}">⚙</button>
    </div>
    <div class="trend-body">${rows || '<div class="muted">暂无已解决数据</div>'}</div>
  </section>`;
}

function renderMiniIssueList(title, items, emptyText, sectionId) {
  const rows = (items || []).map(item => {
    const issueId = item.issue_id || '';
    const reply = item.last_external_reply_by ? `最后回复: ${item.last_external_reply_by}` :
      (item.last_owner_reply_by ? `最后回复: ${item.last_owner_reply_by}` : `附件: ${item.attachment_count || 0}`);
    const note = item.last_external_reply || item.last_owner_reply || '';
    const time = item.last_external_reply_at || item.last_owner_reply_at || item.updated_on || item.created_on || '-';
    return `<div class="issue-mini">
      <div><a href="${redmineIssueUrl(issueId)}" target="_blank">#${issueId}</a><div class="muted">${esc(item.status_name || '-')}</div></div>
      <div class="issue-mini-title">
        <strong title="${esc(item.subject || '')}">${esc(item.subject || '-')}</strong>
        <span>${esc(reply)}${note ? ' | ' + esc(trunc(note, 120)) : ''}</span>
      </div>
      <div class="issue-mini-meta">${esc(item.priority_name || '-')}<br>${esc(String(time).slice(0, 16))}</div>
    </div>`;
  }).join('');
  return `<section class="stats-section" id="${sectionId || ''}"><h2>${esc(title)}</h2><div class="issue-mini-list">${rows || `<div class="muted">${esc(emptyText || '暂无数据')}</div>`}</div></section>`;
}

function renderGroupCards(title, data) {
  const cards = Object.entries(data || {}).map(([k,v]) => `<div class="stat-card"><div class="value">${v}</div><div class="label">${esc(k)}</div></div>`).join('');
  return `<section class="stats-section"><h2>${esc(title)}</h2><div class="stats-grid">${cards || '<div class="muted">无数据</div>'}</div></section>`;
}
function renderSummaryHeader(title, controlsHtml, metaHtml) {
  return `<div class="dashboard-summary-header">
    <h2 class="dashboard-summary-title">${esc(title)}</h2>
    <div class="dashboard-summary-controls">${controlsHtml || ''}</div>
    <div class="muted dashboard-summary-meta">${metaHtml || ''}</div>
  </div>`;
}
function renderStatsCards(cards) {
  return '<div class="stats-grid">' + (cards || []).map(function(card) {
    var cls = card.className ? ' ' + card.className : '';
    var onclick = card.onclick ? ' onclick="' + card.onclick + '"' : '';
    return '<div class="stat-card' + cls + '"' + onclick + '><div class="value">' + esc(card.value == null ? 0 : card.value) + '</div><div class="label">' + esc(card.label || '') + '</div></div>';
  }).join('') + '</div>';
}

function redmineIssueIds(items) {
  return (items || []).map(function(item) { return item.issue_id || ''; }).filter(Boolean);
}

function copyDepartmentIssues(userId, btn) {
  var user = (window._departmentUsers || []).find(function(item) { return String(item.id || '') === String(userId || ''); });
  var urls = redmineIssueUrls((user || {}).overdue_issues || []);
  copyText(urls.join(_NL), btn);
}
function copyProjectIssues(userId, btn) {
  var user = (window._projectUsers || []).find(function(item) { return String(item.id || '') === String(userId || ''); });
  var urls = redmineIssueUrls((user || {}).issues || []);
  copyText(urls.join(_NL), btn);
}

async function sendDepartmentReminder(userId, btn) {
  var user = (window._departmentUsers || []).find(function(item) { return String(item.id || '') === String(userId || ''); });
  var ids = redmineIssueIds((user || {}).overdue_issues || []);
  if (!ids.length) {
    alert('该人员没有超过阈值未回复的 Redmine 问题。');
    return;
  }
  if (btn) { btn.disabled = true; btn.textContent = '⏳'; }
  try {
    var data = await api('/api/redmine-agent/reminders/email', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({user_id: userId, issue_ids: ids})
    });
    alert('✅ 已发送到 ' + (data.to || '绑定邮箱'));
  } catch (e) {
    alert('❌ ' + (e.message || '发送失败'));
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '邮箱'; }
  }
}
async function sendProjectReminder(userId, btn) {
  var user = (window._projectUsers || []).find(function(item) { return String(item.id || '') === String(userId || ''); });
  var ids = redmineIssueIds((user || {}).issues || []);
  if (!ids.length) {
    alert('该人员没有项目未关闭 Redmine 问题。');
    return;
  }
  if (btn) { btn.disabled = true; btn.textContent = '⏳'; }
  try {
    var data = await api('/api/redmine-agent/reminders/email', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        user_id: userId,
        issue_ids: ids,
        subject: 'Redmine 项目未关闭问题提醒 - ' + (user.name || userId),
        intro: '以下 Redmine 问题在项目看板中仍未关闭，请及时处理：'
      })
    });
    alert('✅ 已发送到 ' + (data.to || '绑定邮箱'));
  } catch (e) {
    alert('❌ ' + (e.message || '发送失败'));
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '邮箱'; }
  }
}

function onDepartmentProfileChange() {
  var select = document.getElementById('departmentProfileSelect');
  departmentProfileId = select ? select.value : '';
  loadDepartmentOverdue(true);
}

function renderDepartmentIssue(item) {
  const issueId = item.issue_id || '';
  const lastAt = item.last_external_reply_at || item.updated_on || '-';
  const days = Number(item.unreplied_days || 0);
  const replyText = item.last_external_reply ? ' | ' + esc(trunc(item.last_external_reply, 140)) : '';
  return `<div class="issue-mini">
    <div><a href="${redmineIssueUrl(issueId)}" target="_blank">#${issueId}</a><div class="muted">${esc(item.status_name || '-')}</div></div>
    <div class="issue-mini-title">
      <strong title="${esc(item.subject || '')}">${esc(item.subject || '-')}</strong>
      <span>最后回复: ${esc(item.last_external_reply_by || '-')} | 未回复 ${days} 天${replyText}</span>
    </div>
    <div class="issue-mini-meta">${esc(item.priority_name || '-')}<br>${esc(String(lastAt).slice(0, 16))}</div>
  </div>`;
}

function renderProjectIssue(item) {
  const issueId = item.issue_id || '';
  const updated = item.updated_on || item.created_on || '-';
  return `<div class="issue-mini">
    <div><a href="${redmineIssueUrl(issueId)}" target="_blank">#${issueId}</a><div class="muted">${esc(item.status_name || '-')}</div></div>
    <div class="issue-mini-title">
      <strong title="${esc(item.subject || '')}">${esc(item.subject || '-')}</strong>
      <span>指派给: ${esc(item.assigned_to_name || '-')}</span>
    </div>
    <div class="issue-mini-meta">${esc(item.priority_name || '-')}<br>${esc(String(updated).slice(0, 16))}</div>
  </div>`;
}

function renderDepartmentOverdue(data) {
  const summary = data.summary || {};
  window._departmentUsers = data.users || [];
  redmineTrendNames = (data.users || []).map(function(u) { return u.name; }).filter(Boolean);
  const users = (data.users || []).slice().sort(function(a, b) {
    return String(a.name || '').localeCompare(String(b.name || ''), 'zh-Hans-CN-u-co-pinyin');
  });
  const generatedAt = String(data.generated_at || '-').replace('T', ' ').replace(/:\d{2}$/, '');
  const profile = data.profile || {};
  const sd = data.stale_days || 20;
  departmentProfileId = profile.id || departmentProfileId || '';
  if (data.available_profiles) {
    statsConfig.dashboard = Object.assign({}, statsConfig.dashboard || {}, {profiles: data.available_profiles});
  }
  const profileSelect = `<div class="select-with-add">
    <select id="departmentProfileSelect" onchange="onDepartmentProfileChange()" style="min-width:160px">
      ${departmentOptionsHtml(departmentProfileId, true)}
    </select>
    <button class="select-add-btn" type="button" onclick="showAddDepartmentModal('departmentProfileSelect')" title="添加部门">＋</button>
  </div>`;
  const cards = renderStatsCards([
    {value: summary.open_count || 0, label: '当前未关闭', className: 'warn'},
    {value: summary.waiting_my_reply || 0, label: '待回复', className: 'bad'},
    {value: summary.no_reply_3_days || 0, label: 'RK ' + sd + '天未回复', className: 'bad'},
    {value: summary.customer_no_reply_3_days || 0, label: '客户 ' + sd + '天未回复', className: 'warn'},
    {value: summary.total_owned || 0, label: '历史总数'},
    {value: summary.user_count || 0, label: '配置用户'},
  ]);
  const trends = data.trends || {};
  const trendNames = users.reduce(function(acc, user) {
    (user.owner_names || [user.name]).forEach(function(name) {
      if (name) acc.push(name);
    });
    return acc;
  }, []);
  const trendPanels = `<div class="trend-grid">
    ${renderTrend('每天解决Redmine问题', trends.resolved_daily || [], 'date', 'department_daily', trendNames, departmentProfileId)}
    ${renderTrend('每周解决Redmine问题', trends.resolved_weekly || [], 'week', 'department_weekly', trendNames, departmentProfileId)}
    ${renderTrend('每月解决Redmine问题', trends.resolved_monthly || [], 'month', 'department_monthly', trendNames, departmentProfileId)}
    ${renderTrend('每年解决Redmine问题', trends.resolved_yearly || [], 'year', 'department_yearly', trendNames, departmentProfileId)}
  </div>`;
  const rows = users.map(function(user) {
    const names = (user.owner_names || []).join(' / ');
    const nameLine = esc(user.name || '-');
    const subLine = names ? '<div class="muted">' + esc(names) + '</div>' : '';
    const ids = redmineIssueIds(user.overdue_issues || []);
    const copyDisabled = ids.length ? '' : ' disabled';
    return `<tr style="cursor:pointer" onclick="scrollToSection('dept-user-${esc(user.id || '')}')">
      <td class="col-person"><strong>${nameLine}</strong>${subLine}</td>
      <td>${user.total_owned || 0}</td>
      <td>${user.open_count || 0}</td>
      <td>${user.scanned_open_count || 0}</td>
      <td>${user.waiting_my_reply || 0}</td>
      <td><strong style="color:var(--bad)">${user.no_reply_3_days || 0}</strong></td>
      <td>${user.customer_no_reply_3_days || 0}</td>
      <td>${user.max_unreplied_days || 0}</td>
      <td onclick="event.stopPropagation()">
        <button class="secondary dept-action-btn"${copyDisabled} onclick="copyDepartmentIssues('${esc(user.id || '')}', this)">复制3天未回复工单</button>
        <button class="secondary dept-action-btn"${copyDisabled} onclick="sendDepartmentReminder('${esc(user.id || '')}', this)">邮箱</button>
      </td>
    </tr>`;
  }).join('');
  const table = `<div class="dept-table-wrap">
    <table class="dept-table">
      <thead><tr><th class="col-person">人员</th><th>历史数量</th><th>未关闭</th><th>本地未关闭</th><th>待回复</th><th>RK ${sd}天未回复</th><th>客户 ${sd}天未回复</th><th>最长未回复天数</th><th>操作</th></tr></thead>
      <tbody>${rows || '<tr><td colspan="9" class="muted">暂无配置用户</td></tr>'}</tbody>
    </table>
  </div>`;
  const detailUsers = users.filter(function(user) { return (user.overdue_issues || []).length > 0; });
  const details = detailUsers.map(function(user) {
    const issues = (user.overdue_issues || []).map(renderDepartmentIssue).join('');
    const names = (user.owner_names || []).join(' / ');
    return `<section class="dept-user-block" id="dept-user-${esc(user.id || '')}">
      <div class="dept-user-title">
        <h2>${esc(user.name || '-')} ${sd}天未回复问题 (${(user.overdue_issues || []).length})</h2>
        <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
          <div class="muted">${esc(names || '-')} | 最长 ${user.max_unreplied_days || 0} 天 | 窗口 ${data.window_days || 0} 天</div>
        </div>
      </div>
      <div class="issue-mini-list">${issues}</div>
    </section>`;
  }).join('');
  document.getElementById('departmentContent').innerHTML = `
    <section class="stats-section">
      ${renderSummaryHeader((profile.name || '部门') + ' Redmine 未回复汇总', '<div class="filter-bar">' + profileSelect + '</div>', '更新时间: ' + esc(generatedAt) + ' | 阈值: ' + esc(data.stale_days || 3) + ' 天 | 缓存: ' + (data.cache_hit ? '是' : '否'))}
      ${cards}
    </section>
    ${trendPanels}
    ${table}
    ${details || '<div class="muted" style="padding:12px">当前配置用户暂无超过 ' + sd + ' 天未回复的问题。</div>'}
  `;
}

async function loadDepartmentOverdue(force) {
  const box = document.getElementById('departmentContent');
  if (!box) return;
  await loadStatsConfig();
  var sd = statsConfig.stale_days || 20;
  box.innerHTML = '<div class="muted" style="padding:20px;text-align:center">⏳ 正在统计部门看板超阈值未回复问题...</div>';
  try {
    var defaults = (statsConfig.dashboard || {}).defaults || {};
    var url = '/api/redmine-agent/statistics/department-overdue?stale_days=' + sd
      + '&list_limit=' + (defaults.list_limit || 50)
      + '&issue_limit=' + (defaults.issue_limit || 500)
      + '&profile_id=' + encodeURIComponent(departmentProfileId || '');
    if (force) url += '&refresh=true';
    const data = await api(url);
    renderDepartmentOverdue(data);
  } catch (e) {
    box.innerHTML = `<div class="muted">加载失败: ${esc(e.message)}</div>`;
  }
}

async function loadStatistics() {
  var savedName = '';
  try {
    var oldSel = document.getElementById('statsUserSelect');
    if (oldSel) savedName = oldSel.value;
  } catch(_) {}
  try {
    await loadStatsConfig();
    var selectedName = savedName || '';
    var q = new URLSearchParams(window.location.search);
    if (!selectedName) selectedName = q.get('name') || '';
    var sd = statsConfig.stale_days || 3;
    var workloadUrl = '/api/redmine-agent/statistics/workload?stale_days=' + sd + '&list_limit=30';
    if (selectedName) workloadUrl += '&name=' + encodeURIComponent(selectedName);
    const [basic, workload] = await Promise.all([
      api('/api/redmine-agent/statistics'),
      api(workloadUrl)
    ]);
    const lists = workload.lists || {};
    const meta = workload.meta || {};
    updateRedmineTrendNames(selectedName, meta);

    const userSelectHtml = '<div class="select-with-add">'
      + '<select id="statsUserSelect" onchange="onStatsUserChange()" style="width:160px">'
      + '<option value="">当前登录用户</option>'
      + '</select>'
      + '<button class="select-add-btn" onclick="showAddUserModal()" title="添加用户">＋</button>'
      + '</div>';

    document.getElementById('statsContent').innerHTML = `
      <section class="stats-section">
        ${renderSummaryHeader('Redmine概览', '<div class="filter-bar">' + userSelectHtml + '</div>', '统计身份: ' + ((meta.owner_names || []).map(esc).join(' / ') || '未识别') + ' | 更新时间: ' + esc((meta.generated_at || '-').replace('T', ' ').replace(/:\\d{2}$/, '')))}
        ${renderStatsCards([
          {value: workload.open_count || 0, label: '当前未关闭', className: 'warn'},
          {value: workload.waiting_my_reply || 0, label: '待回复 ⬇', className: 'bad clickable-stat', onclick: "scrollToSection('sec-waiting-reply')"},
          {value: workload.no_reply_3_days || 0, label: 'RK ' + sd + '天未回复客户 ⬇', className: 'bad clickable-stat', onclick: "scrollToSection('sec-no-reply-3d')"},
          {value: workload.customer_no_reply_3_days || 0, label: '客户 ' + sd + '天未回复RK ⬇', className: 'warn clickable-stat', onclick: "scrollToSection('sec-customer-no-reply')"},
          {value: workload.missing_test_report || 0, label: '缺失测试报告 ⬇', className: 'warn clickable-stat', onclick: "scrollToSection('sec-missing-report')"},
          {value: workload.closed_count || 0, label: '已解决 / 已关闭', className: 'ok'},
          {value: workload.total_owned || 0, label: '名下历史数量'},
        ])}
      </section>

      <div class="trend-grid">
        ${renderTrend('每天解决Redmine问题', workload.resolved_daily || [], 'date', 'personal_daily', redmineTrendNames)}
        ${renderTrend('每周解决Redmine问题', workload.resolved_weekly || [], 'week', 'personal_weekly', redmineTrendNames)}
        ${renderTrend('每月解决Redmine问题', workload.resolved_monthly || [], 'month', 'personal_monthly', redmineTrendNames)}
        ${renderTrend('每年解决Redmine问题', workload.resolved_yearly || [], 'year', 'personal_yearly', redmineTrendNames)}
      </div>

      ${renderMiniIssueList('待回复的问题 (' + (lists.waiting_my_reply || []).length + ')', lists.waiting_my_reply || [], '暂无待回复问题', 'sec-waiting-reply')}
      ${renderMiniIssueList('RK ' + sd + '天未回复客户的问题 (' + (lists.no_reply_3_days || []).length + ')', lists.no_reply_3_days || [], '暂无RK超过阈值未回复客户问题', 'sec-no-reply-3d')}
      ${renderMiniIssueList('客户 ' + sd + '天未回复RK的问题 (' + (lists.customer_no_reply_3_days || []).length + ')', lists.customer_no_reply_3_days || [], '暂无客户超过阈值未回复RK问题', 'sec-customer-no-reply')}
      ${renderMiniIssueList('缺失测试报告的问题 (' + (lists.missing_test_report || []).length + ')', lists.missing_test_report || [], '暂无缺失测试报告问题', 'sec-missing-report')}
    `;
    statsUserInitialized = false;
    await initStatsUserSelect();
    if (selectedName) {
      var sel = document.getElementById('statsUserSelect');
      if (sel) sel.value = selectedName;
    }
  } catch (e) {
    document.getElementById('statsContent').innerHTML = `<div class="muted">加载失败: ${esc(e.message)}</div>`;
  }
}

function onProjectProfileChange() {
  var select = document.getElementById('projectProfileSelect');
  projectProfileId = select ? select.value : '';
  loadProjectDashboard(true);
}
function toggleProjectOpenOnly() {
  projectOpenOnly = !projectOpenOnly;
  renderProjectDashboard(window._projectData || {});
}

function renderProjectDashboard(data) {
  window._projectData = data || {};
  const summary = data.summary || {};
  const profile = data.profile || {};
  projectProfileId = profile.id || projectProfileId || '';
  if (data.available_profiles) {
    statsConfig.dashboard = Object.assign({}, statsConfig.dashboard || {}, {project_profiles: data.available_profiles});
  }
  window._projectUsers = data.assignees || [];
  const generatedAt = String(data.generated_at || '-').replace('T', ' ').replace(/:\d{2}$/, '');
  const profileSelect = `<div class="select-with-add">
    <select id="projectProfileSelect" onchange="onProjectProfileChange()" style="min-width:220px">${projectOptionsHtml(projectProfileId)}</select>
    <button class="select-add-btn" type="button" onclick="showAddProjectModal()" title="添加项目">＋</button>
  </div>`;
  const openOnlyBtn = `<button class="secondary toggle-btn ${projectOpenOnly ? 'active' : ''}" onclick="toggleProjectOpenOnly()">${projectOpenOnly ? '显示全员' : '仅未关闭人员'}</button>`;
  const assignees = (data.assignees || []).slice().filter(function(user) {
    return !projectOpenOnly || Number(user.open_count || 0) > 0;
  }).sort(function(a, b) {
    return String(a.name || '').localeCompare(String(b.name || ''), 'zh-Hans-CN-u-co-pinyin');
  });
  const cards = renderStatsCards([
    {value: summary.issue_count || 0, label: '项目总数'},
    {value: summary.assignee_count || 0, label: '涉及人员'},
    {value: summary.open_count || 0, label: '当前未关闭', className: 'warn'},
    {value: summary.closed_count || 0, label: '已解决 / 已关闭', className: 'ok'},
  ]);
  const rows = assignees.map(function(user) {
    const ids = redmineIssueIds(user.issues || []);
    const actionDisabled = ids.length ? '' : ' disabled';
    return `<tr style="cursor:pointer" onclick="scrollToSection('project-user-${esc(user.id || '')}')">
      <td class="col-person"><strong>${esc(user.name || '-')}</strong></td>
      <td>${user.total_owned || 0}</td>
      <td>${user.open_count || 0}</td>
      <td>${user.closed_count || 0}</td>
      <td onclick="event.stopPropagation()">
        <button class="secondary dept-action-btn"${actionDisabled} onclick="copyProjectIssues('${esc(user.id || '')}', this)">复制</button>
        <button class="secondary dept-action-btn"${actionDisabled} onclick="sendProjectReminder('${esc(user.id || '')}', this)">邮箱</button>
      </td>
      <td class="project-filter-cell"></td>
    </tr>`;
  }).join('');
  const table = `<div class="dept-table-wrap">
    <table class="dept-table">
      <thead><tr><th class="col-person">人员</th><th>项目内数量</th><th>未关闭</th><th>已关闭</th><th>操作</th><th class="project-filter-th">${openOnlyBtn || ''}</th></tr></thead>
      <tbody>${rows || '<tr><td colspan="6" class="muted">暂无项目人员数据</td></tr>'}</tbody>
    </table>
  </div>`;
  const details = assignees.filter(function(user) { return (user.issues || []).length > 0; }).map(function(user) {
    const issues = (user.issues || []).map(renderProjectIssue).join('');
    return `<section class="dept-user-block" id="project-user-${esc(user.id || '')}">
      <div class="dept-user-title"><h2>${esc(user.name || '-')} 未关闭问题 (${(user.issues || []).length})</h2></div>
      <div class="issue-mini-list">${issues}</div>
    </section>`;
  }).join('');
  document.getElementById('projectContent').innerHTML = `
    <section class="stats-section">
      ${renderSummaryHeader((profile.name || profile.project_id || '项目') + ' Redmine 当前情况', '<div class="filter-bar">' + profileSelect + '</div>', '项目: ' + esc(profile.project_id || '-') + ' | 更新时间: ' + esc(generatedAt) + ' | 缓存: ' + (data.cache_hit ? '是' : '否') + ' | ' + (projectOpenOnly ? '仅显示未关闭人员' : '显示全员'))}
      ${cards}
    </section>
    ${table}
    ${details || '<div class="muted" style="padding:12px">当前项目暂无未关闭问题。</div>'}
  `;
}

async function loadProjectDashboard(force) {
  const box = document.getElementById('projectContent');
  if (!box) return;
  await loadStatsConfig();
  if (!projectProfiles().length) {
    box.innerHTML = '<div class="muted" style="padding:20px">暂无项目看板配置。<button style="margin-left:10px" onclick="showAddProjectModal()">＋ 添加项目</button></div>';
    return;
  }
  box.innerHTML = '<div class="muted" style="padding:20px;text-align:center">⏳ 正在统计项目 Redmine 当前情况...</div>';
  try {
    var selected = projectProfileId || (projectProfiles()[0] || {}).id || '';
    var url = '/api/redmine-agent/statistics/project?profile_id=' + encodeURIComponent(selected);
    if (force) url += '&refresh=true';
    const data = await api(url);
    renderProjectDashboard(data);
  } catch (e) {
    box.innerHTML = `<div class="muted">加载失败: ${esc(e.message)}</div>`;
  }
}

// ---- Actions ----
function _sendParentNotification(title, message, level) {
  try {
    window.parent.postMessage({type:'redmine-agent-notification', title, message, level}, '*');
  } catch(_) {}
}

async function startScan() {
  const btn = document.getElementById('scanBtn');
  btn.disabled = true; btn.textContent = '⏳ 扫描中...';
  try {
    const started = await api('/api/redmine-agent/runs?hours=48&max_issues=50', {method:'POST'});
    const rid = started.run_id || '';
    btn.textContent = '⏳ 等待结果...';
    await waitForRun(rid, '扫描');
  } catch (e) { alert('扫描失败: ' + e.message); }
  finally { btn.disabled = false; btn.textContent = '🔍 扫描'; }
}

async function triggerSync() {
  if (!confirm('确认全量同步所有指派给你的 Redmine 工单？这可能需要几分钟。')) return;
  const btn = document.getElementById('scanBtn');
  try {
    const started = await api('/api/redmine-agent/sync?max_analyze=30', {method:'POST'});
    if (btn) { btn.disabled = true; btn.textContent = '⏳ 同步中...'; }
    await waitForRun(started.run_id, '同步');
  } catch (e) { alert('同步失败: ' + e.message); }
  finally { if (btn) { btn.disabled = false; btn.textContent = '🔍 扫描'; } }
}

async function waitForRun(runId, label) {
  for (let i = 0; i < 240; i++) {
    await new Promise(r => setTimeout(r, 1500));
    try {
      const status = await api('/api/redmine-agent/status');
      if (!status.running) {
        refreshCurrentTab();
        _sendParentNotification('RedmineAgent ' + label + '完成', '任务 ' + runId + ' 已完成', 'success');
        return;
      }
    } catch (_) {}
  }
  refreshCurrentTab();
  _sendParentNotification('RedmineAgent ' + label + '超时', '任务 ' + runId + ' 等待超时，请检查状态', 'warning');
}

// ---- Init ----
var initialTab = new URLSearchParams(window.location.search).get('tab') || (window.sessionStorage.getItem('redmineLastTab') || 'stats');
if (!document.getElementById('tab-' + initialTab)) initialTab = 'stats';
switchTab(initialTab);

// Check if a task is already running on page load — reset button state
(async function() {
  try {
    const status = await api('/api/redmine-agent/status');
    if (!status.running) {
      var btn = document.getElementById('scanBtn');
      if (btn) { btn.disabled = false; btn.textContent = '🔍 扫描'; }
    }
  } catch(_) {}
})();

// Auto-refresh status
setInterval(async () => {
  try {
    const status = await api('/api/redmine-agent/status');
    var btn = document.getElementById('scanBtn');
    if (status.running) {
      document.title = '⏳ RedmineAgent (运行中...)';
    } else {
      document.title = '🔧 RedmineAgent';
      if (btn && btn.disabled) { btn.disabled = false; btn.textContent = '🔍 扫描'; }
    }
  } catch (_) {}
}, 10000);
</script>
</body>
</html>
"""
    )
