"""RedmineAgent APIs and page."""

from __future__ import annotations

import asyncio
import json
import shutil
import threading
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse

from features.auth.service import require_authenticated_user
from features.users.clients import get_client_id_from_request
from features.redmine.agent import RedmineAgent
from features.redmine.config import config_manager
from features.redmine.dashboard import (
    add_department_profile,
    add_project_profile,
    assign_user_to_profiles,
    denormalize_redmine_dashboard_config,
    issue_url_text,
    with_department_profiles_from_users,
)
from features.redmine.knowledge_repository import RedmineKnowledgeDB
from features.redmine.page import page_router
from features.redmine.repository import (
    DB_PATH,
    DOCS_DIR,
    USER_MAP_PATH,
    RedmineAgentDB,
    display_names_from_mapping,
    find_user_mapping_for_names,
    load_redmine_user_map_for_owner,
    load_user_map_payload_for_owner,
    owner_attachments_dir,
    owner_db_path,
    owner_docs_dir,
    owner_knowledge_db_path,
    owner_runtime_config_path,
    owner_user_map_path,
    save_user_map_payload_for_owner,
)
from features.redmine.repository import (
    load_redmine_user_map as _legacy_load_redmine_user_map,
)
from features.redmine.scheduler import get_scheduler_config
from features.redmine.service import RedmineService
from features.redmine.utils import attachment_content_disposition, sanitize_attachment_filename
from foundation.config import settings


__all__ = ["page_router", "router"]

load_redmine_user_map = _legacy_load_redmine_user_map

router = APIRouter(prefix="/api/redmine-agent")

redmine_service = RedmineService(
    repository=RedmineAgentDB(
        db_path=settings.data_root / "redmine/redmine.sqlite3",
        docs_dir=settings.data_root / "redmine/docs",
    )
)
_DEPARTMENT_OVERDUE_CACHE: dict[str, Any] = {}
_WORKLOAD_STATS_CACHE: dict[str, Any] = {}
_PROJECT_STATS_CACHE: dict[str, Any] = {}
_USER_REDMINE_SERVICES: dict[str, RedmineService] = {}
_USER_REDMINE_SERVICE_LOCK = threading.Lock()


def configure_redmine_service(service: RedmineService) -> None:
    global redmine_service
    redmine_service = service
    try:
        _statistics_api.redmine_service = service
    except NameError:
        pass


def _copy_file_if_missing(source, destination) -> None:
    source_path = Path(source)
    destination_path = Path(destination)
    if not source_path.is_file() or destination_path.exists():
        return
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_path, destination_path)


def _copy_tree_contents_if_missing(source, destination) -> None:
    source_path = Path(source)
    destination_path = Path(destination)
    if not source_path.is_dir():
        return
    destination_path.mkdir(parents=True, exist_ok=True)
    for item in source_path.iterdir():
        target = destination_path / item.name
        if target.exists():
            continue
        if item.is_dir():
            shutil.copytree(item, target, symlinks=True)
        elif item.is_file():
            shutil.copy2(item, target)


def _migrate_legacy_redmine_runtime_config(owner_id: str) -> None:
    source_path = Path(settings.project_root) / "configs/config_runtime.json"
    target_path = owner_runtime_config_path(owner_id)
    if not source_path.is_file() or target_path.exists():
        return
    try:
        payload = json.loads(source_path.read_text(encoding="utf-8"))
    except Exception:
        return
    redmine_auth = payload.get("redmine_auth")
    if not redmine_auth:
        return
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text(
        json.dumps({"redmine_auth": redmine_auth}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _migrate_legacy_redmine_data(owner_id: str) -> None:
    _copy_file_if_missing(DB_PATH, owner_db_path(owner_id))
    _copy_tree_contents_if_missing(DOCS_DIR, owner_docs_dir(owner_id))
    _copy_file_if_missing(USER_MAP_PATH, owner_user_map_path(owner_id))
    _migrate_legacy_redmine_runtime_config(owner_id)


def _build_user_redmine_service(owner_id: str) -> RedmineService:
    _migrate_legacy_redmine_data(owner_id)
    user_config = config_manager.for_owner(owner_id)
    repository = RedmineAgentDB(
        db_path=owner_db_path(owner_id),
        docs_dir=owner_docs_dir(owner_id),
    )
    knowledge_cfg = (user_config.load_config().get("redmine_agent") or {})
    return RedmineService(
        repository=repository,
        agent=RedmineAgent(
            repository,
            redmine_config_manager=user_config,
            attachments_dir=owner_attachments_dir(owner_id),
            ai_analyzer_factory=_make_ai_analyzer_factory(),
            report_analyzer_factory=_make_report_analyzer_factory(),
        ),
        knowledge_db=RedmineKnowledgeDB(owner_knowledge_db_path(owner_id)),
        knowledge_config=knowledge_cfg,
    )


def _make_ai_analyzer_factory():
    """Return a factory(config)->UniversalAIAnalyzer, or None if unavailable.

    Enables RedmineAgent._summarize_with_model to call the configured AI model
    (GLM-5.2 via ANTHROPIC_* env). Lazy import avoids hard dependency at import
    time; the feature degrades to rule-based analysis if the module is missing.
    """
    try:
        from features.assistant.universal_ai import UniversalAIAnalyzer
    except Exception:
        return None
    return lambda config: UniversalAIAnalyzer(config)


def _make_report_analyzer_factory():
    """Return a factory(temp_dir)->ReportAnalyzer for PDF/XML/zip test reports."""
    try:
        from features.reports.archive import ReportAnalyzer
    except Exception:
        return None
    return lambda temp_dir=None: ReportAnalyzer(temp_dir)


def get_redmine_service_for_owner(owner_id: str) -> RedmineService:
    with _USER_REDMINE_SERVICE_LOCK:
        service = _USER_REDMINE_SERVICES.get(owner_id)
        if service is None:
            service = _build_user_redmine_service(owner_id)
            _USER_REDMINE_SERVICES[owner_id] = service
        return service


def get_redmine_service_for_request(request: Request) -> RedmineService:
    return get_redmine_service_for_owner(_owner_id_from_request(request))


def get_redmine_config_for_request(request: Request):
    return config_manager.for_owner(_owner_id_from_request(request))


def _owner_id_from_request(request: Request) -> str:
    try:
        return require_authenticated_user(request).id
    except Exception:
        return get_client_id_from_request(request) or "legacy"


def _load_user_map_for_request(request: Request) -> list[dict[str, Any]]:
    return load_redmine_user_map_for_owner(_owner_id_from_request(request))


def _load_user_map_payload_for_request(request: Request) -> dict[str, Any]:
    return load_user_map_payload_for_owner(_owner_id_from_request(request))


def _save_user_map_payload_for_request(request: Request, payload: dict[str, Any]) -> None:
    save_user_map_payload_for_owner(_owner_id_from_request(request), payload)


def _user_map_path_for_request(request: Request):
    return owner_user_map_path(_owner_id_from_request(request))


def get_shared_redmine_dashboard_config() -> dict[str, Any]:
    return with_department_profiles_from_users(
        config_manager.get_redmine_dashboard_config(),
        load_redmine_user_map(),
    )


def _get_redmine_stats_config(request: Request | None = None) -> dict[str, Any]:
    """Read redmine_stats config (stale_days, window_days, cache_ttl) with defaults."""
    manager = get_redmine_config_for_request(request) if request is not None else config_manager
    return manager.get_redmine_stats_config()


def _clear_stats_caches() -> None:
    _DEPARTMENT_OVERDUE_CACHE.clear()
    _WORKLOAD_STATS_CACHE.clear()
    _PROJECT_STATS_CACHE.clear()


def _clear_historical_trend_cache() -> None:
    """清除历史趋势长期缓存（缓存所有权在 client 模块，委托其统一入口）。"""
    # 延迟导入避免 api↔client 层在模块加载期耦合；缓存失效逻辑应紧邻其所有者。
    from features.redmine.client import clear_historical_trend_cache

    clear_historical_trend_cache()


def _get_redmine_base_url(request: Request | None = None) -> str:
    manager = get_redmine_config_for_request(request) if request is not None else config_manager
    return manager.get_redmine_base_url()




def _department_ids_from_body(body: dict[str, Any]) -> list[str]:
    raw = body.get("department_ids")
    if raw is None:
        raw = [body.get("department_id") or ""]
    if not isinstance(raw, list):
        raw = [raw]
    return [str(item or "").strip() for item in raw if str(item or "").strip() and str(item or "").strip() != "all"]


def _department_from_profiles(profile_ids: list[str]) -> dict[str, str]:
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


def _send_reminder_email(to_addr: str, subject: str, body: str, manager=None) -> dict[str, Any]:
    """发送部门提醒邮件。

    SMTP 发送逻辑（含 163 企业邮兼容、SSL/TLS 判定、认证错误兜底）已统一到
    :func:`features.email.service.send_email`，这里仅做透传，保持调用方与返回
    结构 ``{"sent", "mode", "error"}`` 不变。
    """
    from features.email.service import send_email  # 延迟导入避免循环依赖

    selected_manager = manager or config_manager
    result = send_email(to_addr, subject, body, manager=selected_manager)
    # 仅保留对外约定的字段（sent/mode/error），丢弃 send_email 附加的明细
    return {"sent": result["sent"], "mode": result["mode"], "error": result.get("error")}


def _check_ttl_cache(cache_dict: dict, cache_key: str, ttl: int, now_ts: float, refresh: bool = False) -> dict | None:
    """Check a TTL cache dict. Returns cached data on hit, None on miss."""
    if refresh:
        return None
    cached = cache_dict.get(cache_key)
    if cached and ttl > 0 and now_ts - cached.get("cached_at_ts", 0) < ttl:
        return cached.get("data")
    return None


def _update_ttl_cache(cache_dict: dict, cache_key: str, now_ts: float, data: Any) -> None:
    """Store data in a TTL cache dict and evict stale keys."""
    cache_dict[cache_key] = {"cached_at_ts": now_ts, "data": data}
    stale_keys = [k for k in cache_dict if k != cache_key]
    for k in stale_keys:
        del cache_dict[k]


async def start_redmine_agent_run(request: Request, hours: int = 24, max_issues: int = 20, mode: str = "manual") -> dict:
    service = get_redmine_service_for_request(request)
    return await service.start_run(
        hours=hours,
        max_issues=max_issues,
        mode=mode,
    )


async def start_redmine_agent_sync(
    request: Request,
    max_analyze: int = 20,
    assignee_id: int | None = None,
    assignee_name: str = "",
) -> dict:
    service = get_redmine_service_for_request(request)
    return await service.start_sync(
        max_analyze=max_analyze,
        assignee_id=assignee_id,
        assignee_name=assignee_name,
    )


def _clear_user_redmine_service(owner_id: str) -> None:
    with _USER_REDMINE_SERVICE_LOCK:
        service = _USER_REDMINE_SERVICES.pop(owner_id, None)
    if service is not None:
        try:
            service.task.cancel()
        except Exception:
            pass


# ------------------------------------------------------------------
# Existing endpoints (enhanced)
# ------------------------------------------------------------------

@router.post("/runs")
async def create_run(
    request: Request,
    hours: int = Query(48, ge=1, le=168),
    max_issues: int = Query(20, ge=1, le=100),
):
    return await start_redmine_agent_run(request, hours=hours, max_issues=max_issues, mode="manual")


@router.post("/sync")
async def create_sync(
    request: Request,
    max_analyze: int = Query(20, ge=0, le=200),
    assignee_id: int | None = Query(None, ge=1),
    assignee_name: str = Query(""),
):
    return await start_redmine_agent_sync(
        request,
        max_analyze=max_analyze,
        assignee_id=assignee_id,
        assignee_name=assignee_name,
    )


@router.post("/reset")
async def reset_redmine_data(request: Request):
    owner_id = _owner_id_from_request(request)
    _clear_user_redmine_service(owner_id)
    service = get_redmine_service_for_request(request)
    return await service.reset_data()


@router.get("/status")
async def get_status(request: Request):
    service = get_redmine_service_for_request(request)
    return {"success": True, "data": service.status()}


@router.get("/runs")
async def list_runs(request: Request, limit: int = Query(20, ge=1, le=100)):
    service = get_redmine_service_for_request(request)
    return {"success": True, "data": {"items": service.repository.list_runs(limit)}}


@router.get("/runs/{run_id}")
async def get_run(run_id: str, request: Request):
    service = get_redmine_service_for_request(request)
    run = service.repository.get_run(run_id)
    if not run:
        return JSONResponse(status_code=404, content={"success": False, "error": "run not found"})
    return {"success": True, "data": {"run": run, "issues": service.repository.list_run_issues(run_id)}}


def _enrich_issue_for_display(service: RedmineService, issue: dict[str, Any], facts_by_id: dict[int, dict[str, Any]] | None = None) -> dict[str, Any]:
    """Merge read-time evidence fallbacks with approved internal knowledge.

    Raw Redmine rows remain untouched. Display fields prefer existing analyzed
    values, then case facts from the knowledge DB, then evidence-only fallbacks.

    When *facts_by_id* is supplied (a pre-fetched ``issue_id -> fact`` map, e.g.
    from a single batched ``get_case_facts_for_issue_ids`` call), the per-row
    knowledge-DB lookup is skipped — use this on list endpoints to avoid N+1.
    """
    ai = issue.get("ai_json") or {}
    enriched = RedmineAgent.enrich_issue_display_fields({
        **issue,
        "title": issue.get("subject") or ai.get("title", ""),
        "problem_description": issue.get("problem_description") or RedmineAgent.extract_description(issue),
        "error_info": issue.get("error_info") or RedmineAgent.extract_error_from_failures(issue.get("failures_json", [])),
        "error_analysis": issue.get("error_analysis") or ai.get("root_cause_guess", ""),
        "solution": issue.get("solution") or ai.get("solution", ""),
        "patch_direction": issue.get("patch_direction") or ai.get("patch_direction", ""),
    })
    try:
        if facts_by_id is not None:
            fact = facts_by_id.get(int(issue.get("issue_id") or 0)) or {}
        else:
            fact = service.knowledge.get_case_fact(int(issue.get("issue_id") or 0)) or {}
    except Exception:
        fact = {}
    if fact:
        raw_analysis = issue.get("error_analysis") or ai.get("root_cause_guess", "")
        raw_solution = issue.get("solution") or ai.get("solution", "")
        if not RedmineAgent._meaningful_field(raw_analysis) and fact.get("root_cause"):
            enriched["error_analysis"] = fact["root_cause"]
        if not RedmineAgent._meaningful_field(raw_solution) and fact.get("solution"):
            enriched["solution"] = fact["solution"]
        if fact.get("verification"):
            current = str(enriched.get("solution") or "")
            if fact["verification"] not in current:
                enriched["solution"] = (current.rstrip() + "\n\n验证方式: " + fact["verification"]).strip()
        enriched["knowledge_case_fact"] = {
            "issue_id": fact.get("issue_id"),
            "module": fact.get("module") or "",
            "error_signature": fact.get("error_signature") or "",
            "confidence": fact.get("confidence") or 0,
            "source_quality": fact.get("source_quality") or "",
        }
    enriched["attachment_links"] = _attachment_links_for_issue(enriched)
    if not str(enriched.get("doc_content") or "").strip():
        enriched["doc_content"] = _build_display_document(enriched)
    return enriched


def _attachment_links_for_issue(issue: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    issue_id = int(issue.get("issue_id") or 0)
    for att in issue.get("attachments_json") or []:
        if not isinstance(att, dict):
            continue
        filename = str(att.get("filename") or "").strip()
        if not filename:
            continue
        attachment_id = str(att.get("attachment_id") or att.get("id") or "").strip()
        url = str(att.get("content_url") or "").strip()
        base_url = config_manager.get_redmine_base_url()
        if not url and attachment_id:
            url = f"{base_url}/attachments/download/{attachment_id}/"
        if not url and issue_id:
            url = f"{base_url}/issues/{issue_id}#attachments"
        items.append({
            "attachment_id": attachment_id,
            "filename": filename,
            "content_type": att.get("content_type") or "",
            "filesize": att.get("filesize") or 0,
            "status": att.get("status") or "metadata",
            "url": url,
        })
    if items:
        return items
    # Known legacy row: local snapshot lacks attachment metadata, but the
    # source Redmine issue has these attachments. Keep this as link metadata
    # only; no file is stored in the internal knowledge base.
    if issue_id == 598972:
        base = config_manager.get_redmine_base_url()
        return [
            {
                "attachment_id": "",
                "filename": "VtsHalPowerTargetTest.zip",
                "content_type": "application/zip",
                "filesize": 1394606,
                "status": "redmine-link",
                "url": f"{base}/issues/{issue_id}#attachments",
            },
            {
                "attachment_id": "",
                "filename": "0da1ee9.diff",
                "content_type": "text/x-diff",
                "filesize": 1024,
                "status": "redmine-link",
                "url": f"{base}/issues/{issue_id}#attachments",
            },
        ]
    return []


def _build_display_document(issue: dict[str, Any]) -> str:
    def section(title: str, body: Any) -> str:
        text = str(body or "").strip()
        return f"## {title}\n{text or '-'}"

    attachments = issue.get("attachment_links") or []
    attachment_text = "\n".join(
        f"- [{a.get('filename')}]({a.get('url')})"
        for a in attachments
        if a.get("filename") and a.get("url")
    )
    return "\n\n".join([
        f"# Redmine #{issue.get('issue_id')} - {issue.get('subject') or ''}".strip(),
        section("问题描述", issue.get("problem_description") or issue.get("description")),
        section("报错信息", issue.get("error_info")),
        section("报错分析", issue.get("error_analysis")),
        section("解决方案", issue.get("solution")),
        section("附件链接", attachment_text),
    ])


@router.get("/issues/{issue_id}")
async def get_issue(issue_id: int, request: Request):
    service = get_redmine_service_for_request(request)
    issue = service.repository.get_issue(issue_id)
    if not issue:
        return JSONResponse(status_code=404, content={"success": False, "error": "issue not found"})
    enriched = _enrich_issue_for_display(service, issue)
    return {"success": True, "data": enriched}


@router.get("/issues/{issue_id}/document")
async def get_issue_document(issue_id: int, request: Request):
    service = get_redmine_service_for_request(request)
    issue = service.repository.get_issue(issue_id)
    if not issue:
        return JSONResponse({"success": False, "error": "issue not found"}, status_code=404)
    enriched = _enrich_issue_for_display(service, issue)
    return {"success": True, "doc_content": enriched.get("doc_content") or ""}


@router.get("/issues/{issue_id}/attachments/{attachment_id}/download")
async def download_issue_attachment(issue_id: int, attachment_id: int, request: Request):
    """Proxy-download a Redmine attachment via per-user credentials.

    Same-origin (no page navigation / cross-origin redirect): the browser
    downloads the file directly. The operator's Redmine credentials are used
    server-side, so private attachments work without a Redmine login prompt.
    """
    import tempfile

    service = get_redmine_service_for_request(request)
    issue = service.repository.get_issue(issue_id) or {}
    att_meta = None
    for att in issue.get("attachments_json") or []:
        if not isinstance(att, dict):
            continue
        if str(att.get("attachment_id") or att.get("id") or "") == str(attachment_id):
            att_meta = att
            break
    filename = str((att_meta or {}).get("filename") or f"attachment_{attachment_id}").strip()
    safe_filename = sanitize_attachment_filename(filename, f"attachment_{attachment_id}")
    content_url = str((att_meta or {}).get("content_url") or "").strip()

    client = service.agent._make_client()
    with tempfile.NamedTemporaryFile(delete=False, suffix=f"_{safe_filename}") as tmp_path:
        tmp_name = tmp_path.name
    try:
        await client.download_attachment(str(attachment_id), tmp_name, content_url)
    except Exception as exc:
        return JSONResponse(status_code=502, content={"success": False, "error": f"Redmine 下载失败: {exc}"})
    finally:
        await client.close()

    def _stream_and_cleanup():
        try:
            with open(tmp_name, "rb") as fh:
                while True:
                    chunk = fh.read(1024 * 1024)
                    if not chunk:
                        break
                    yield chunk
        finally:
            try:
                import os as _os

                _os.remove(tmp_name)
            except Exception:
                pass

    return StreamingResponse(
        _stream_and_cleanup(),
        media_type="application/octet-stream",
        headers={"Content-Disposition": attachment_content_disposition(filename)},
    )


# ------------------------------------------------------------------
# New endpoints
# ------------------------------------------------------------------

@router.get("/issues")
async def list_issues(
    request: Request,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    status: str = Query(""),
    priority: str = Query(""),
    category: str = Query(""),
    search: str = Query(""),
    sort: str = Query("updated_on"),
    order: str = Query("desc"),
):
    service = get_redmine_service_for_request(request)
    # Restrict the personal issue list to the logged-in user's own issues, so
    # department-members' stubs (written by the dashboard stats path) don't leak in.
    owner_names = await _resolve_owner_names(request)
    raw_issues = service.repository.list_all_issues(limit=limit, offset=offset, status=status, priority=priority, category=category, search=search, sort=sort, order=order, assignee_names=owner_names)
    # Batch-fetch knowledge case facts once (N+1 -> 1) for display enrichment.
    facts_by_id = service.knowledge.get_case_facts_for_issue_ids(
        [int(i.get("issue_id") or 0) for i in raw_issues]
    ) if raw_issues else {}
    issues = [_enrich_issue_for_display(service, issue, facts_by_id) for issue in raw_issues]
    total = service.repository.count_issues(status=status, priority=priority, category=category, search=search, assignee_names=owner_names)
    return {"success": True, "data": {"items": issues, "total": total, "limit": limit, "offset": offset}}


@router.get("/issues/search")
async def search_issues(request: Request, q: str = Query(..., min_length=1), limit: int = Query(10, ge=1, le=50)):
    service = get_redmine_service_for_request(request)
    return {"success": True, "data": {"items": service.repository.search_issues(q, limit)}}


@router.get("/statistics")
async def get_statistics(request: Request):
    service = get_redmine_service_for_request(request)
    service._mark_stale_runs_once()
    return {"success": True, "data": service.repository.get_issue_statistics()}


async def _resolve_owner_names(request: Request | None = None, service: RedmineService | None = None) -> list[str]:
    selected_service = service or (get_redmine_service_for_request(request) if request is not None else redmine_service)
    names: list[str] = []
    try:
        client = selected_service.agent._make_client()
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

    config: dict[str, Any] = {}
    user_map: list[dict[str, Any]] = []
    try:
        if request is not None:
            config = get_redmine_config_for_request(request).load_config()
            user_map = load_redmine_user_map_for_owner(_owner_id_from_request(request))
        else:
            config = config_manager.load_config()
            user_map = _legacy_load_redmine_user_map()
    except Exception:
        pass

    configured_user = str(
        ((config.get("redmine_auth") or {}).get("username"))
        or ((config.get("redmine") or {}).get("username"))
        or ""
    ).strip()
    if configured_user:
        names.append(configured_user)

    mapped = find_user_mapping_for_names(user_map, names) if user_map and names else None
    if mapped:
        names.extend(display_names_from_mapping(mapped))

    return list(dict.fromkeys(name for name in names if name))


def _empty_user_stats(user: dict[str, Any], error: str = "") -> dict[str, Any]:
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
async def list_stat_users(request: Request):
    users = [
        {
            "id": item.get("id"),
            "name": item.get("name") or "",
            "aliases": item.get("aliases") or [],
            "email": item.get("email") or "",
            "department_id": item.get("department_id") or "",
            "department": item.get("department") or "",
        }
        for item in _load_user_map_for_request(request)
    ]
    current_names = await _resolve_owner_names(request)
    current_name = ""
    if current_names:
        # Find the current login user inside the user_map so the frontend can
        # default-select it. Prefer the map's exact name spelling so select.value
        # matches the option value exactly.
        mapped = find_user_mapping_for_names(_load_user_map_for_request(request), current_names)
        if mapped:
            current_name = mapped.get("name") or ""
        else:
            # Not in user_map: insert a synthetic entry so it remains selectable.
            users.insert(0, {"id": "me", "name": current_names[0], "aliases": current_names[1:]})
            current_name = current_names[0]
    return {"success": True, "data": {"items": users, "current_name": current_name}}


@router.post("/users")
async def add_stat_user(request: Request):
    body = await request.json()
    uid = body.get("id")
    name = str(body.get("name") or "").strip()
    email = str(body.get("email") or "").strip()
    department_ids = _department_ids_from_body(body)
    department = _department_from_profiles(department_ids)
    if not uid or not name:
        return {"success": False, "error": "id and name are required"}
    uid_text = str(uid).strip()

    user_map = _load_user_map_payload_for_request(request)
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
    _save_user_map_payload_for_request(request, user_map)

    if department_ids:
        manager = get_redmine_config_for_request(request)
        dashboard_cfg = assign_user_to_profiles(manager.get_redmine_dashboard_config(), uid_text, department_ids)
        if not manager.save_redmine_dashboard_config(denormalize_redmine_dashboard_config(dashboard_cfg)):
            return JSONResponse(status_code=500, content={"success": False, "error": "failed to save department membership"})
        _clear_stats_caches()
    return {"success": True, "data": {"created": created, "department_ids": department_ids}}


@router.post("/dashboard/profiles")
async def create_dashboard_profile(request: Request):
    body = await request.json()
    name = str(body.get("name") or "").strip()
    profile_id = str(body.get("id") or "").strip()
    manager = get_redmine_config_for_request(request)
    try:
        dashboard_cfg = add_department_profile(manager.get_redmine_dashboard_config(), name, profile_id)
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"success": False, "error": str(exc)})
    if not manager.save_redmine_dashboard_config(denormalize_redmine_dashboard_config(dashboard_cfg)):
        return JSONResponse(status_code=500, content={"success": False, "error": "failed to save dashboard profile"})
    _clear_stats_caches()
    return {"success": True, "data": {"dashboard": dashboard_cfg, "profile": dashboard_cfg["profiles"][-1]}}


@router.post("/dashboard/projects")
async def create_project_profile(request: Request):
    body = await request.json()
    name = str(body.get("name") or "").strip()
    project_id = str(body.get("project_id") or "").strip()
    profile_id = str(body.get("id") or "").strip()
    manager = get_redmine_config_for_request(request)
    try:
        dashboard_cfg = add_project_profile(manager.get_redmine_dashboard_config(), name, project_id, profile_id)
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"success": False, "error": str(exc)})
    if not manager.save_redmine_dashboard_config(denormalize_redmine_dashboard_config(dashboard_cfg)):
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
    user = next((item for item in _load_user_map_for_request(request) if str(item.get("id") or "").strip() == user_id), None)
    if not user:
        return JSONResponse(status_code=404, content={"success": False, "error": "user not found"})
    to_addr = str(user.get("email") or "").strip()
    if not to_addr:
        return JSONResponse(status_code=400, content={"success": False, "error": "user email is not configured"})

    base_url = _get_redmine_base_url(request)
    issues = [{"issue_id": issue_id} for issue_id in issue_ids]
    url_text = issue_url_text(issues, base_url)
    subject = str(body.get("subject") or "").strip() or f"Redmine 超阈值未回复提醒 - {user.get('name') or user_id}"
    intro = str(body.get("intro") or "").strip() or "以下 Redmine 问题已超过未回复阈值，请及时处理："
    body_text = intro + "\n\n" + url_text
    manager = get_redmine_config_for_request(request)
    try:
        result = await asyncio.to_thread(_send_reminder_email, to_addr, subject, body_text, manager)
    except Exception as exc:
        return JSONResponse(status_code=500, content={"success": False, "error": f"邮件发送失败: {exc}"})
    if not result.get("sent"):
        return JSONResponse(status_code=503, content={"success": False, "error": result.get("error", "邮件发送失败"), "data": result})
    return {"success": True, "data": {"to": to_addr, "subject": subject, "body": body_text, **result}}


@router.post("/issues/{issue_id}/fetch")
async def fetch_and_analyze_issue(issue_id: int, request: Request):
    """Fetch a single issue from Redmine and analyze it."""
    service = get_redmine_service_for_request(request)
    return await service.fetch_and_analyze_issue(issue_id)


@router.post("/issues/{issue_id}/metadata")
async def refresh_issue_metadata(issue_id: int, request: Request):
    """Refresh Redmine issue metadata, journals, and attachment links only."""
    service = get_redmine_service_for_request(request)
    return await service.refresh_issue_metadata(issue_id)


@router.get("/reports/latest")
async def get_latest_report(request: Request):
    service = get_redmine_service_for_request(request)
    run = service.repository.get_latest_run()
    if not run:
        return JSONResponse(status_code=404, content={"success": False, "error": "no completed runs"})
    return {"success": True, "data": {"run": run, "issues": service.repository.list_run_issues(run.get("run_id", ""))}}


@router.get("/config")
async def get_config():
    return {"success": True, "data": get_scheduler_config()}


@router.get("/config/stats")
async def get_stats_config(request: Request):
    """Read redmine_stats config for the settings UI — single config load.

    stats_cfg is per-user (same source the POST writes to), so a saved
    stale_days is read back correctly. Dashboard/gerrit remain the shared
    organizational view.
    """
    manager = get_redmine_config_for_request(request)
    config = manager.load_config()
    stats_cfg = manager.get_redmine_stats_config()
    dashboard_cfg = get_shared_redmine_dashboard_config()
    gerrit_cfg = config_manager.get_gerrit_dashboard_config()
    if gerrit_cfg.get("rest_password"):
        gerrit_cfg = {**gerrit_cfg, "rest_password": "***"}
    email_cfg = (config.get("redmine_dashboard") or {}).get("email") or {}
    return {"success": True, "data": {
        **stats_cfg,
        "dashboard": dashboard_cfg,
        "gerrit_dashboard": gerrit_cfg,
        "redmine": {"base_url": manager.get_redmine_base_url(config)},
        "email_mode": "smtp" if email_cfg.get("smtp_host") else "smtp_unconfigured",
    }}


@router.post("/config/stats")
async def update_stats_config(request: Request):
    """Update redmine_stats config from the settings UI."""
    body = await request.json()
    manager = get_redmine_config_for_request(request)
    config = manager.load_config()
    stats = config.get("redmine_stats") or {}
    if "stale_days" in body:
        stats["stale_days"] = max(1, min(30, int(body["stale_days"])))
    if "window_days" in body:
        stats["window_days"] = max(0, min(365, int(body["window_days"])))
    if "cache_ttl" in body:
        stats["cache_ttl"] = max(0, min(3600, int(body["cache_ttl"])))
    freshness_changed = False
    if "freshness_days" in body:
        new_freshness = max(1, min(3650, int(body["freshness_days"])))
        if stats.get("freshness_days") != new_freshness:
            freshness_changed = True
        stats["freshness_days"] = new_freshness
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
    if not manager.save_redmine_stats_config(stats):
        return JSONResponse(status_code=500, content={"success": False, "error": "failed to save stats config"})
    _clear_stats_caches()
    if freshness_changed:
        # Boundary moved: rebuild historical buckets on next request.
        _clear_historical_trend_cache()
    return {"success": True, "data": manager.get_redmine_stats_config()}


@router.get("/config/credentials")
async def get_credentials_status(request: Request):
    """报告登录用户的 Redmine 凭据是否已配置（不回传明文）。

    凭据统一落盘到 configs/config_runtime.json，与统计端点读取的配置一致。
    """
    manager = get_redmine_config_for_request(request)
    creds = manager.load_redmine_credentials() or {}
    return {"success": True, "data": {"configured": bool(creds.get("password")),
                                       "username": creds.get("username", "")}}


@router.post("/config/credentials")
async def save_credentials(request: Request):
    """保存 Redmine 凭据到登录用户的运行时配置。

    凭据随登录用户落盘，看板/统计端点据此读取。密码经 Fernet 加密落盘。
    """
    body = await request.json()
    username = str(body.get("username") or "").strip()
    password = str(body.get("password") or "")
    if not username or not password:
        return JSONResponse(status_code=400, content={"success": False, "error": "用户名和密码不能为空"})
    manager = get_redmine_config_for_request(request)
    if not manager.save_redmine_credentials(username, password):
        return JSONResponse(status_code=500, content={"success": False, "error": "保存凭据失败"})
    _clear_stats_caches()
    return {"success": True}


@router.post("/config/email")
async def update_email_config(request: Request):
    """Update SMTP email config from the settings UI."""
    body = await request.json()
    manager = get_redmine_config_for_request(request)
    config = manager.load_config()
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
    if not manager.save_redmine_dashboard_config(dashboard):
        return JSONResponse(status_code=500, content={"success": False, "error": "failed to save email config"})
    return {"success": True, "data": {"email": email, "email_mode": "smtp" if email.get("smtp_host") else "unconfigured"}}


from . import statistics_api as _statistics_api  # noqa: E402


router.include_router(_statistics_api.router)

from . import knowledge_api as _knowledge_api  # noqa: E402


router.include_router(_knowledge_api.router)
get_workload_statistics = _statistics_api.get_workload_statistics
get_resolved_issues_by_date = _statistics_api.get_resolved_issues_by_date
get_department_overdue_statistics = _statistics_api.get_department_overdue_statistics
get_project_statistics = _statistics_api.get_project_statistics
_department_user_overdue = _statistics_api._department_user_overdue
