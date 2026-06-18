"""RedmineAgent APIs and page."""

from __future__ import annotations

import asyncio
import smtplib
from datetime import datetime
from email.message import EmailMessage
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

from features.redmine.agent import RedmineAgent
from features.redmine.repository import (
    RedmineAgentDB, USER_MAP_PATH, find_user_mapping, display_names_from_mapping, load_redmine_user_map,
    load_user_map_payload, save_user_map_payload,
    compute_user_overdue_stats, _name_keys as _nk,
)
from features.redmine.dashboard import (
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
from features.redmine.config import config_manager
from features.redmine.scheduler import get_scheduler_config
from features.redmine.service import RedmineService
from features.redmine.page import page_router
from foundation.config import settings


router = APIRouter(prefix="/api/redmine-agent")

redmine_service = RedmineService(
    repository=RedmineAgentDB(
        db_path=settings.data_root / "redmine/redmine.sqlite3",
        docs_dir=settings.data_root / "redmine/docs",
    )
)
_DEPARTMENT_OVERDUE_CACHE: Dict[str, Any] = {}
_WORKLOAD_STATS_CACHE: Dict[str, Any] = {}
_PROJECT_STATS_CACHE: Dict[str, Any] = {}


def configure_redmine_service(service: RedmineService) -> None:
    global redmine_service
    redmine_service = service
    try:
        _statistics_api.redmine_service = service
    except NameError:
        pass


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


async def start_redmine_agent_run(hours: int = 24, max_issues: int = 20, mode: str = "manual") -> dict:
    return await redmine_service.start_run(
        hours=hours,
        max_issues=max_issues,
        mode=mode,
    )


async def start_redmine_agent_sync(max_analyze: int = 20) -> dict:
    return await redmine_service.start_sync(max_analyze=max_analyze)


# ------------------------------------------------------------------
# Existing endpoints (enhanced)
# ------------------------------------------------------------------

@router.post("/runs")
async def create_run(
    hours: int = Query(48, ge=1, le=168),
    max_issues: int = Query(20, ge=1, le=100),
):
    return await start_redmine_agent_run(hours=hours, max_issues=max_issues, mode="manual")


@router.get("/status")
async def get_status():
    return {"success": True, "data": redmine_service.status()}


@router.get("/runs")
async def list_runs(limit: int = Query(20, ge=1, le=100)):
    return {"success": True, "data": {"items": redmine_service.repository.list_runs(limit)}}


@router.get("/runs/{run_id}")
async def get_run(run_id: str):
    run = redmine_service.repository.get_run(run_id)
    if not run:
        return JSONResponse(status_code=404, content={"success": False, "error": "run not found"})
    return {"success": True, "data": {"run": run, "issues": redmine_service.repository.list_run_issues(run_id)}}


@router.get("/issues/{issue_id}")
async def get_issue(issue_id: int):
    issue = redmine_service.repository.get_issue(issue_id)
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
    issue = redmine_service.repository.get_issue(issue_id)
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
    issues = redmine_service.repository.list_all_issues(limit=limit, offset=offset, status=status, priority=priority, category=category, search=search, sort=sort, order=order)
    total = redmine_service.repository.count_issues(status=status, priority=priority, category=category, search=search)
    return {"success": True, "data": {"items": issues, "total": total, "limit": limit, "offset": offset}}


@router.get("/issues/search")
async def search_issues(q: str = Query(..., min_length=1), limit: int = Query(10, ge=1, le=50)):
    return {"success": True, "data": {"items": redmine_service.repository.search_issues(q, limit)}}


@router.get("/statistics")
async def get_statistics():
    redmine_service._mark_stale_runs_once()
    return {"success": True, "data": redmine_service.repository.get_issue_statistics()}


async def _resolve_owner_names() -> List[str]:
    names: List[str] = []
    try:
        client = redmine_service.agent._make_client()
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


@router.post("/sync")
async def trigger_sync(max_analyze: int = Query(20, ge=1, le=100)):
    return await start_redmine_agent_sync(max_analyze=max_analyze)


@router.post("/issues/{issue_id}/fetch")
async def fetch_and_analyze_issue(issue_id: int):
    """Fetch a single issue from Redmine and analyze it."""
    return await redmine_service.fetch_and_analyze_issue(issue_id)


@router.get("/reports/latest")
async def get_latest_report():
    run = redmine_service.repository.get_latest_run()
    if not run:
        return JSONResponse(status_code=404, content={"success": False, "error": "no completed runs"})
    return {"success": True, "data": {"run": run, "issues": redmine_service.repository.list_run_issues(run.get("run_id", ""))}}


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


from . import statistics_api as _statistics_api

router.include_router(_statistics_api.router)
get_workload_statistics = _statistics_api.get_workload_statistics
get_resolved_issues_by_date = _statistics_api.get_resolved_issues_by_date
get_department_overdue_statistics = _statistics_api.get_department_overdue_statistics
get_project_statistics = _statistics_api.get_project_statistics
_department_user_overdue = _statistics_api._department_user_overdue
