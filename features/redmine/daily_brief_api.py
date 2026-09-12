"""Daily Brief triage 只读端点 + 运行 API（Phase 3/8/35/36）。

- ``GET  /daily-brief/triage``：当天待处理 issue 快照（CLI/MCP 同源）。
- ``GET  /daily-brief/latest``、``/daily-brief/{date}``：查看晨报。
- ``GET  /daily-brief/config``、``PUT /daily-brief/config``：配置。
- ``POST /daily-brief/run``：手动触发（后台执行，立即返回 run_id）。
- ``POST /daily-brief/{date}/refresh``：delta 刷新（第二阶段启用入口）。
- ``POST /daily-brief/{date}/issues/{issue_id}/reanalyze``：单 issue 重分析。

triage 数据严格来自 ``build_daily_triage_snapshot``（内部唯一调用
``get_workload_statistics``），本文件不做任何业务筛选判断。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from features.auth import require_agent_scope, require_human_principal_when_auth_required
from features.users import owner_id_from_request

from .api import get_redmine_config_for_request
from .daily_brief_models import BRIEF_MODES, RUN_STATUSES
from .daily_brief_service import (
    DEFAULT_BRIEF_CONFIG,
    DailyBriefService,
    normalize_daily_brief_config,
)
from .daily_brief_snapshot import DEFAULT_LIST_LIMIT, DEFAULT_STALE_DAYS
from .statistics_api import _has_redmine_credentials, _missing_credentials_payload


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/redmine-agent")

# 手动 run 的后台任务登记（进程内；重启由 repository 层标记 failed）。
_RUN_TASKS: dict[str, asyncio.Task] = {}


def _require_read(request: Request) -> None:
    """读端点：Redmine 数据按 scope 收口（人工会话天然通过）。"""
    require_agent_scope("redmine.read")(request)


def _require_human(request: Request) -> None:
    """写端点（配置/触发/重分析）只允许人工会话：Agent token 不得改配置，
    也不得通过 API 批量启动 AI 子进程；取证走只读 MCP 工具。"""
    require_human_principal_when_auth_required(request)


def _service_for_request(request: Request | None) -> DailyBriefService:
    return DailyBriefService(
        owner_id=owner_id_from_request(request),
        config_manager=get_redmine_config_for_request(request),
    )


@router.get("/daily-brief/triage")
async def get_daily_triage(
    request: Request,
    stale_days: int = Query(DEFAULT_STALE_DAYS, ge=1, le=30),
    list_limit: int = Query(DEFAULT_LIST_LIMIT, ge=1, le=100),
    refresh: bool = Query(True),
):
    """当天待处理 issue（waiting_my_reply / no_reply_3_days 去重）。

    ``refresh=true``（默认）先从 Redmine 同步该 owner 的最新 issue 快照，
    再基于本地库构建 triage；``refresh=false`` 只读本地镜像（可用于离线
    检视，结果可能落后于 Redmine）。
    """
    _require_read(request)
    if not _has_redmine_credentials(request):
        return _missing_credentials_payload()
    service = _service_for_request(request)
    try:
        snapshot = await service.build_triage(
            stale_days=stale_days, list_limit=list_limit, refresh=refresh,
        )
    except Exception as exc:
        logger.error("daily triage failed: %s", exc)
        return JSONResponse(content={"success": False, "error": str(exc)}, status_code=500)
    return {"success": True, "data": snapshot}


@router.get("/daily-brief/latest")
async def get_latest_brief(request: Request):
    _require_read(request)
    service = _service_for_request(request)
    run = service.latest_run()
    if run is None:
        return {"success": True, "data": {"configured": False, "message": "暂无晨报。"}}
    return {"success": True, "data": service.run_payload(run)}


@router.get("/daily-brief/config")
async def get_daily_brief_config(request: Request):
    _require_read(request)
    manager = get_redmine_config_for_request(request)
    service = _service_for_request(request)
    return {"success": True, "data": service.get_config(manager)}


@router.put("/daily-brief/config")
async def put_daily_brief_config(request: Request, payload: dict):
    _require_human(request)
    manager = get_redmine_config_for_request(request)
    service = _service_for_request(request)
    normalized = normalize_daily_brief_config(payload or {})
    unknown = set(payload or {}) - set(DEFAULT_BRIEF_CONFIG) if isinstance(payload, dict) else set()
    if unknown:
        return JSONResponse(
            content={"success": False, "error": f"unknown config keys: {sorted(unknown)}"},
            status_code=400,
        )
    saved = service.save_config(manager, normalized)
    if not saved:
        return JSONResponse(
            content={"success": False, "error": "failed to persist config"},
            status_code=500,
        )
    return {"success": True, "data": service.get_config(manager)}


@router.post("/daily-brief/run")
async def run_daily_brief(
    request: Request,
    payload: dict | None = None,
    mode: str = Query("manual"),
):
    if mode not in BRIEF_MODES:
        return JSONResponse(
            content={"success": False, "error": f"mode must be one of {BRIEF_MODES}"},
            status_code=400,
        )
    _require_human(request)
    if not _has_redmine_credentials(request):
        return _missing_credentials_payload()
    service = _service_for_request(request)
    started = service.start_run(mode=mode)
    if "run_id" not in started:
        return JSONResponse(
            content={"success": False, "error": started.get("error", "failed to start run")},
            status_code=409,
        )
    run_id = started["run_id"]
    if started.get("reused") or started.get("already_running"):
        # 幂等：当天已有有效 run，直接返回现状，不再启动后台任务。
        return {"success": True, "data": started}

    task = asyncio.create_task(service.execute_run(run_id))
    _RUN_TASKS[run_id] = task
    task.add_done_callback(lambda _t, rid=run_id: _RUN_TASKS.pop(rid, None))
    return {"success": True, "data": {"run_id": run_id, "status": "pending"}}


@router.get("/daily-brief/{brief_date}")
async def get_brief_for_date(request: Request, brief_date: str):
    _require_read(request)
    if not _is_brief_date(brief_date):
        return JSONResponse(
            content={"success": False, "error": "brief_date must be YYYY-MM-DD"},
            status_code=400,
        )
    service = _service_for_request(request)
    run = service.find_run(brief_date=brief_date)
    if run is None:
        return JSONResponse(
            content={"success": False, "error": f"no daily brief for {brief_date}"},
            status_code=404,
        )
    return {"success": True, "data": service.run_payload(run)}


@router.post("/daily-brief/{brief_date}/refresh")
async def refresh_daily_brief(request: Request, brief_date: str):
    """增量刷新：只重分析发生变化的 issue（第二阶段语义，入口先留）。"""
    _require_human(request)
    if not _is_brief_date(brief_date):
        return JSONResponse(
            content={"success": False, "error": "brief_date must be YYYY-MM-DD"},
            status_code=400,
        )
    service = _service_for_request(request)
    result = service.start_refresh(brief_date)
    if "run_id" not in result:
        return JSONResponse(
            content={"success": False, "error": result.get("error", "refresh not started")},
            status_code=409,
        )
    task = asyncio.create_task(service.execute_run(result["run_id"]))
    _RUN_TASKS[result["run_id"]] = task
    task.add_done_callback(lambda _t, rid=result["run_id"]: _RUN_TASKS.pop(rid, None))
    return {"success": True, "data": result}


@router.post("/daily-brief/{brief_date}/issues/{issue_id}/reanalyze")
async def reanalyze_issue(request: Request, brief_date: str, issue_id: int):
    _require_human(request)
    service = _service_for_request(request)
    result = await service.reanalyze_issue(brief_date, issue_id)
    if result.get("status") in RUN_STATUSES or "run_id" in result:
        return {"success": True, "data": result}
    return JSONResponse(
        content={"success": False, "error": result.get("error", "reanalyze failed")},
        status_code=404,
    )


def _is_brief_date(value: str) -> bool:
    try:
        datetime.strptime(value, "%Y-%m-%d")
        return True
    except (TypeError, ValueError):
        return False
