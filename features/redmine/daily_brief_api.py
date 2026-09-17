"""Daily Brief triage 只读端点 + 持久任务入队 API。

- ``GET  /daily-brief/triage``：当天待处理 issue 快照（CLI/MCP 同源）。
- ``GET  /daily-brief/latest``、``/daily-brief/{date}``：查看晨报。
- ``GET  /daily-brief/config``、``PUT /daily-brief/config``：配置。
- ``POST /daily-brief/run``：手动触发（持久化入队，立即返回 run_id）。
- ``POST /daily-brief/{date}/refresh``：delta 刷新（第二阶段启用入口）。
- ``POST /daily-brief/{date}/issues/{issue_id}/reanalyze``：单 issue 重分析。

triage 数据严格来自 ``build_daily_triage_snapshot``（内部唯一调用
``get_workload_statistics``），本文件不做任何业务筛选判断。
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Path, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from features.auth import require_agent_scope, require_human_principal_when_auth_required
from features.users import owner_id_from_request
from foundation.error_model import ApiError

from .api import get_redmine_config_for_request
from .daily_brief_config import (
    list_daily_brief_agent_profiles,
    list_daily_brief_model_options,
)
from .daily_brief_dispatch import enqueue_reanalysis, enqueue_refresh, enqueue_run
from .daily_brief_models import BRIEF_MODES
from .daily_brief_service import (
    DEFAULT_BRIEF_CONFIG,
    DailyBriefService,
    normalize_daily_brief_config,
)
from .daily_brief_snapshot import DEFAULT_LIST_LIMIT, DEFAULT_STALE_DAYS
from .statistics_api import _has_redmine_credentials, _missing_credentials_payload


logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/redmine-agent")


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


class SingleIssueAnalysisRequest(BaseModel):
    issue_id: int = Field(strict=True, gt=0, le=9223372036854775807)
    analysis_mode: Literal["incremental", "full"] = "incremental"
    device_serial: str = Field(default="", max_length=64)
    analysis_hint: str = Field(default="", max_length=4000)


@router.post("/daily-brief/analyze-issue")
async def analyze_single_issue(request: Request, payload: SingleIssueAnalysisRequest):
    _require_human(request)
    if not _has_redmine_credentials(request):
        return ApiError.dependency_unavailable("请先配置 Redmine 取证凭据。").to_response()
    device_serial = payload.device_serial.strip()
    if device_serial and not re.fullmatch(r"[A-Za-z0-9:._-]{2,64}", device_serial):
        return ApiError.malformed_request("设备序列号格式无效。").to_response()
    subject = ""
    try:
        # This is a user-requested analysis action, so refresh its metadata
        # before enqueueing.  The resulting title is stored on the standalone
        # row rather than making the UI wait for the AI worker to discover it.
        from .api import get_redmine_service_for_owner

        metadata = await get_redmine_service_for_owner(
            owner_id_from_request(request)
        ).refresh_issue_metadata(payload.issue_id)
        subject = str((metadata.get("data") or {}).get("issue", {}).get("subject") or "").strip()
    except Exception:
        logger.info("single issue %s metadata refresh unavailable", payload.issue_id, exc_info=True)
    result = _service_for_request(request).start_issue_analysis(
        payload.issue_id, analysis_mode=payload.analysis_mode, device_serial=device_serial,
        analysis_hint=payload.analysis_hint.strip(), subject=subject,
    )
    if result.get("error"):
        # 资格/排队失败必须走统一错误信封：把 error 包进 success 信封
        # 会让调用方按正常成功路径渲染（错误码表唯一真源，见
        # foundation.error_model）。
        if str(result.get("code") or "") == "ADMIN_OWNER_FORBIDDEN":
            return ApiError.forbidden(str(result["error"])).to_response()
        return ApiError.malformed_request(str(result["error"])).to_response()
    return {"success": True, "data": result}


@router.get("/daily-brief/issue-analyses")
async def list_issue_analyses(request: Request, limit: int = Query(30, ge=1, le=100)):
    """Persistent standalone-analysis history, grouped by Redmine issue id."""
    _require_read(request)
    return {"success": True, "data": {"items": _service_for_request(request).latest_issue_runs(limit)}}


@router.get("/daily-brief/issue-analyses/{issue_id}")
async def get_issue_analysis(request: Request, issue_id: int = Path(ge=1)):
    """Look up a saved standalone analysis before creating a new one."""
    _require_read(request)
    item = _service_for_request(request).latest_issue_run(issue_id)
    if item is None:
        return ApiError.not_found("该 Redmine 单号暂无已保存的分析。").to_response()
    return {"success": True, "data": item}


@router.get("/daily-brief/runs/{run_id}")
async def get_brief_run(request: Request, run_id: str):
    _require_read(request)
    service = _service_for_request(request)
    run = service.repository.get_run(run_id)
    if run is None or run.owner_id != service.owner_id:
        return ApiError.not_found("分析任务不存在。").to_response()
    return {"success": True, "data": service.run_payload(run)}


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
        return _missing_credentials_payload(request)
    service = _service_for_request(request)
    try:
        snapshot = await service.build_triage(
            stale_days=stale_days, list_limit=list_limit, refresh=refresh,
        )
    except Exception as exc:
        logger.error("daily triage failed: %s", exc)
        return ApiError.internal(f"每日待办构建失败: {exc}").to_response()
    return {"success": True, "data": snapshot}


@router.get("/daily-brief/latest")
async def get_latest_brief(request: Request):
    _require_read(request)
    service = _service_for_request(request)
    run = service.latest_run()
    if run is None:
        return {"success": True, "data": {"configured": False, "message": "暂无晨报。"}}
    return {"success": True, "data": service.run_payload(run)}


@router.get("/daily-brief/active-issue")
async def get_active_issue_analysis(request: Request):
    """Restore a standalone issue analysis after the browser reloads."""
    _require_read(request)
    service = _service_for_request(request)
    run = service.latest_active_issue_run()
    if run is None:
        return {"success": True, "data": {"run": None, "issues": []}}
    return {"success": True, "data": service.run_payload(run)}


@router.get("/daily-brief/config")
async def get_daily_brief_config(request: Request):
    _require_read(request)
    manager = get_redmine_config_for_request(request)
    service = _service_for_request(request)
    return {"success": True, "data": service.get_config(manager)}


@router.get("/daily-brief/model-options")
async def get_daily_brief_model_options(request: Request):
    """Expose enabled system model names for the Daily Brief settings UI."""
    _require_read(request)
    return {"success": True, "data": list_daily_brief_model_options()}


@router.get("/daily-brief/agent-profiles")
async def get_daily_brief_agent_profiles(request: Request):
    """Expose only local kkagent profile names for explicit human selection."""
    _require_read(request)
    return {"success": True, "data": list_daily_brief_agent_profiles()}


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
        return _missing_credentials_payload(request)
    service = _service_for_request(request)
    force = (payload or {}).get("force", False) if isinstance(payload, dict) else False
    if not isinstance(force, bool):
        return JSONResponse(
            content={"success": False, "error": "force must be a boolean"},
            status_code=400,
        )
    started = enqueue_run(service, mode=mode, force=force)
    if "run_id" not in started:
        return JSONResponse(
            content={"success": False, "error": started.get("error", "failed to start run")},
            status_code=409,
        )
    return {"success": True, "data": started}


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
    result = enqueue_refresh(service, brief_date)
    if "run_id" not in result:
        return JSONResponse(
            content={"success": False, "error": result.get("error", "refresh not started")},
            status_code=409,
        )
    return {"success": True, "data": result}


@router.post("/daily-brief/runs/{run_id}/issues/{issue_id}/reanalyze")
async def reanalyze_issue_in_run(request: Request, run_id: str, issue_id: int):
    """按 run_id 精确重新分析单个 issue（优先入口；date 回落见下方兼容路由）。"""
    _require_human(request)
    service = _service_for_request(request)
    result = enqueue_reanalysis(service, "", issue_id, run_id=run_id)
    if "run_id" in result:
        return {"success": True, "data": result}
    return ApiError(code=result.get("code", "NOT_FOUND"), message=result.get("error", "reanalyze failed")).to_response()


@router.post("/daily-brief/{brief_date}/issues/{issue_id}/reanalyze")
async def reanalyze_issue(request: Request, brief_date: str, issue_id: int):
    _require_human(request)
    service = _service_for_request(request)
    result = enqueue_reanalysis(service, brief_date, issue_id)
    if "run_id" in result:
        return {"success": True, "data": result}
    return ApiError(code=result.get("code", "NOT_FOUND"), message=result.get("error", "reanalyze failed")).to_response()


@router.post("/daily-brief/runs/{run_id}/cancel")
async def cancel_daily_brief_run(request: Request, run_id: str):
    """按 run_id 精确请求停止一次晨报 run（协作式取消）。

    同一天可有 nightly/manual/delta 多个 run，按日期取消
    "最新一次"会停错目标；UI 从当前卡片携带 run_id 精确取消。
    """
    _require_human(request)
    service = _service_for_request(request)
    result = service.request_cancel(run_id=run_id)
    if result.get("error"):
        return JSONResponse(
            content={"success": False, "error": result["error"]},
            status_code=404,
        )
    return {"success": True, "data": result}


@router.post("/daily-brief/{brief_date}/cancel")
async def cancel_daily_brief(request: Request, brief_date: str):
    """请求停止该日期最新一次晨报 run（兼容入口；优先用 runs/{run_id}/cancel）。

    独立 Worker 执行靠 DB 标志位在 issue 边界生效；同进程执行额外
    task.cancel()。已终态的 run 幂等返回 already_terminal。
    """
    _require_human(request)
    if not _is_brief_date(brief_date):
        return JSONResponse(
            content={"success": False, "error": "brief_date must be YYYY-MM-DD"},
            status_code=400,
        )
    service = _service_for_request(request)
    result = service.request_cancel(brief_date)
    if result.get("error"):
        return JSONResponse(
            content={"success": False, "error": result["error"]},
            status_code=404,
        )
    return {"success": True, "data": result}


def _is_brief_date(value: str) -> bool:
    try:
        datetime.strptime(value, "%Y-%m-%d")
        return True
    except (TypeError, ValueError):
        return False
