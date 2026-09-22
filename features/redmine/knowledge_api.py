"""HTTP endpoints for the Redmine knowledge base.

All routes are per-user (resolved through ``get_redmine_service_for_request``).
Mounted under the existing ``/api/redmine-agent`` prefix via
``router.include_router`` (see ``api.py``).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from features.auth import require_authenticated_user, require_human_principal_when_auth_required
from features.users import owner_id_from_request
from foundation.error_model import ApiError

from .api import get_redmine_service_for_request
from .daily_brief_repository import canonical_owner_id, owner_daily_brief_repository


router = APIRouter()


def _knowledge(request: Request):
    return get_redmine_service_for_request(request).knowledge


def _approver(request: Request) -> str:
    user = require_authenticated_user(request)
    return getattr(user, "name", "") or getattr(user, "id", "") or "unknown"


def _coerce_optional_int(value: Any) -> int | None:
    """Parse an optional int from request body; None on missing/invalid input."""
    try:
        return int(value) if value else None
    except (TypeError, ValueError):
        return None


# Batch import + case facts

@router.post("/issues/batch-import")
async def batch_import_issues(request: Request):
    body = await _maybe_body(request)
    raw_ids = body.get("issue_ids")
    issue_ids = _coerce_issue_ids(raw_ids)
    reanalyze = bool(body.get("reanalyze", True))
    if not issue_ids:
        # Optional: import the N most-recent scanned issues for the owner.
        limit = int(body.get("recent_limit") or 0)
        if limit > 0:
            return await _knowledge(request).import_recent_assigned(limit=limit, assigned_like=str(body.get("assigned_like") or ""), reanalyze=reanalyze)
        return JSONResponse(status_code=400, content={"success": False, "error": "issue_ids are required"})
    return await _knowledge(request).batch_import_cases(issue_ids, reanalyze=reanalyze)


@router.post("/issues/import-recent")
async def import_recent_issues(request: Request, limit: int = Query(20, ge=1, le=500)):
    body = await _maybe_body(request)
    assigned_like = str((body or {}).get("assigned_like") or "")
    reanalyze = bool((body or {}).get("reanalyze", True))
    return await _knowledge(request).import_recent_assigned(limit=limit, assigned_like=assigned_like, reanalyze=reanalyze)


@router.post("/issues/{issue_id}/analyze-case")
async def analyze_case(issue_id: int, request: Request):
    return await _knowledge(request).import_single_case(issue_id, reanalyze=True)


@router.get("/cases")
async def list_case_facts(
    request: Request,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    module: str = Query(""),
    search: str = Query(""),
):
    return {"success": True, "data": _knowledge(request).list_case_facts(limit=limit, offset=offset, module=module, search=search)}


@router.get("/cases/{issue_id}")
async def get_case_fact(issue_id: int, request: Request):
    fact = _knowledge(request).get_case_fact(issue_id)
    if not fact:
        return JSONResponse(status_code=404, content={"success": False, "error": "case fact not found"})
    return {"success": True, "data": fact}


# Similarity search

@router.post("/search/similar")
async def search_similar(request: Request, limit: int = Query(10, ge=1, le=50)):
    body = await _maybe_body(request)
    query = body.get("query") or body.get("text") or ""
    if isinstance(query, dict):
        probe = query
    else:
        probe = str(query or "")
    exclude = int(body.get("exclude_issue_id") or 0)
    return {"success": True, "data": {"items": _knowledge(request).search_similar(probe, limit=limit, exclude_issue_id=exclude)}}


@router.get("/issues/{issue_id}/similar")
async def similar_for_issue(issue_id: int, request: Request, limit: int = Query(10, ge=1, le=50)):
    return {"success": True, "data": _knowledge(request).similar_for_issue(issue_id, limit=limit)}


@router.get("/issues/{issue_id}/workbench")
async def issue_workbench(issue_id: int, request: Request, similar_limit: int = Query(6, ge=1, le=20)):
    try:
        return {"success": True, "data": _knowledge(request).issue_workbench(issue_id, similar_limit=similar_limit)}
    except Exception as exc:
        # 返回非 2xx 状态，便于前端区分失败和空数据。
        return JSONResponse(
            status_code=502,
            content={
                "success": False,
                "error": f"知识依据加载失败: {exc}",
                "issue_id": int(issue_id),
            },
        )


# Mature cases

@router.post("/mature-cases/build")
async def build_mature_case(request: Request):
    body = await _maybe_body(request)
    issue_ids = _coerce_issue_ids(body.get("issue_ids"))
    title = str(body.get("title") or "")
    if not issue_ids:
        return JSONResponse(status_code=400, content={"success": False, "error": "issue_ids are required"})
    return {"success": True, "data": _knowledge(request).build_mature_case(issue_ids, title=title)}


@router.get("/mature-cases")
async def list_mature_cases(
    request: Request,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    status: str = Query(""),
    search: str = Query(""),
):
    return {"success": True, "data": _knowledge(request).list_mature_cases(limit=limit, offset=offset, status=status, search=search)}


@router.get("/mature-cases/{case_id}")
async def get_mature_case(case_id: int, request: Request):
    case = _knowledge(request).get_mature_case(case_id)
    if not case:
        return JSONResponse(status_code=404, content={"success": False, "error": "mature case not found"})
    return {"success": True, "data": case}


@router.post("/mature-cases/{case_id}/approve")
async def approve_mature_case(case_id: int, request: Request):
    ok = _knowledge(request).approve_mature_case(case_id, _approver(request))
    if not ok:
        return JSONResponse(status_code=404, content={"success": False, "error": "mature case not found"})
    return {"success": True}


# Reply drafting

@router.post("/issues/{issue_id}/draft-reply")
async def draft_reply(issue_id: int, request: Request):
    body = await _maybe_body(request) or {}
    mature_case_id = _coerce_optional_int(body.get("mature_case_id"))
    return {"success": True, "data": _knowledge(request).draft_reply(issue_id, mature_case_id=mature_case_id)}


@router.post("/issues/{issue_id}/agent-reply")
async def agent_reply(issue_id: int, request: Request):
    """Agent-driven reply: online fetch + AI analysis + knowledge-base match.

    Returns reply_draft + patch_direction + root_cause. Slower (10-30s) when a
    fresh AI analysis is needed; fast when the issue is already analyzed.
    """
    body = await _maybe_body(request) or {}
    mature_case_id = _coerce_optional_int(body.get("mature_case_id"))
    force = bool(body.get("force", False))
    data = await _knowledge(request).draft_agent_reply(
        issue_id, force=force, mature_case_id=mature_case_id,
    )
    if data.get("source") == "not_found":
        return JSONResponse(status_code=502, content={"success": False, "error": data.get("error", "not found")})
    return {"success": True, "data": data}


# Reference outputs + evaluation (off the production path)

@router.post("/issues/{issue_id}/reference-output")
async def import_reference_output(issue_id: int, request: Request):
    body = await _maybe_body(request)
    return {"success": True, "data": _knowledge(request).import_reference_output(issue_id, body)}


@router.get("/issues/{issue_id}/reference-outputs")
async def list_reference_outputs(issue_id: int, request: Request):
    return {"success": True, "data": {"items": _knowledge(request).list_reference_outputs(issue_id)}}


@router.post("/issues/{issue_id}/evaluate-case")
async def evaluate_case(issue_id: int, request: Request):
    body = await _maybe_body(request)
    reference = body.get("reference") if body else None
    return {"success": True, "data": _knowledge(request).evaluate_case(issue_id, reference=reference)}


@router.get("/issues/{issue_id}/evaluation")
async def latest_evaluation(issue_id: int, request: Request):
    evaluation = _knowledge(request).latest_evaluation(issue_id)
    return {"success": True, "data": evaluation}


# Daily-brief result -> case fact (local KB sink, no Redmine write)

@router.post("/daily-brief/{brief_date}/issues/{issue_id}/save-case")
async def save_daily_brief_case(brief_date: str, issue_id: int, request: Request, run_id: str = Query("")):
    """把一次晨报**诊断**分析结果沉淀为本地 case_fact（FTS 可检索）。

    批量 triage 的结果只是待办摘要（无根因），且其执行
    状态（completed）不是 Redmine 工单状态——一律拒绝保存，避免把错误
    status 和 AI 推导写进知识库。只有 evidence_gate 判定为 diagnostic 的
    深度分析可以保存。

    只写本地知识库，不做任何 Redmine 写操作。字段以
    RedmineCaseExtractor 从本地扫描库提取的 Redmine 事实为基底，AI 结论
    仅覆盖结论性字段；落库为 non-destructive merge（已有非空事实字段不
    被空值清空）。重复保存按 issue_id upsert，保留 created_at。
    run_id 指定用户当前查看的晨报。
    """
    from .daily_brief_case_fact import build_case_fact_from_brief
    from .kkagent.evidence_gate import result_analysis_mode

    require_human_principal_when_auth_required(request)
    owner_id = canonical_owner_id(owner_id_from_request(request))
    repository = owner_daily_brief_repository(owner_id)
    run = repository.get_run(run_id) if run_id else repository.latest_run(owner_id, brief_date)
    if run is None or run.owner_id != owner_id or run.brief_date != brief_date:
        return ApiError.not_found(f"no daily brief run for {brief_date}").to_response()
    record = repository.get_issue(run.run_id, issue_id)
    if record is None or record.status != "completed" or not record.result:
        return ApiError.not_found(f"issue {issue_id} has no completed analysis in {run.run_id}").to_response()
    if result_analysis_mode(record.result) != "diagnostic":
        return ApiError.conflict(
            "only diagnostic (deep) analysis results can be saved as a case; "
            f"issue {issue_id} in {run.run_id} is a triage summary — run "
            "深度分析此项 first"
        ).to_response()
    # ADR 0009：native（kkagent_markdown）摘要在独立提取契约存在之前
    # 不提供结构化案例保存——它没有 root_cause/solution/confidence 等
    # 结构化字段，落库只会得到空结论的 FTS 壳记录（UI 已隐藏按钮，这里
    # 挡 API 直连绕过）。
    if record.result.get("result_format") == "kkagent_markdown":
        return ApiError.conflict(
            f"native markdown summary (issue {issue_id}) has no structured "
            "case fields yet; structured case saving requires the JSON "
            "diagnostic result format"
        ).to_response()
    service = _knowledge(request)
    issue_row: dict[str, Any] | None = None
    issue_repository = getattr(service, "issue_repository", None)
    if issue_repository is not None:
        try:
            issue_row = issue_repository.get_issue(issue_id)
        except Exception:  # pragma: no cover - scan store unavailable
            issue_row = None
    fact = build_case_fact_from_brief(issue_id, record, run, issue=issue_row)
    service.knowledge_db.upsert_case_fact(fact, merge_missing=True)
    return {"success": True, "data": {"issue_id": issue_id, "saved": True}}


# Internal issue creation (confirmed, configurable)

@router.post("/issues/{issue_id}/create-internal")
async def create_internal_from_issue(issue_id: int, request: Request):
    body = await _maybe_body(request)
    confirmed = bool(body.get("confirmed", False))
    payload = {k: v for k, v in body.items() if k != "confirmed"}
    payload.setdefault("created_by", _approver(request))
    return await _knowledge(request).create_internal_from_issue(issue_id, payload, confirmed=confirmed)


@router.post("/mature-cases/{case_id}/create-internal")
async def create_internal_from_case(case_id: int, request: Request):
    body = await _maybe_body(request)
    confirmed = bool(body.get("confirmed", False))
    payload = {k: v for k, v in body.items() if k != "confirmed"}
    payload.setdefault("created_by", _approver(request))
    return await _knowledge(request).create_internal_from_case(case_id, payload, confirmed=confirmed)


# Helpers

def _coerce_issue_ids(raw: Any) -> list[int]:
    if not raw:
        return []
    if isinstance(raw, (list, tuple)):
        items = list(raw)
    else:
        items = str(raw).replace(",", "\n").splitlines()
    ids: list[int] = []
    for item in items:
        try:
            ids.append(int(str(item).strip().lstrip("#")))
        except (ValueError, AttributeError):
            continue
    seen: set[int] = set()
    result: list[int] = []
    for issue_id in ids:
        if issue_id and issue_id not in seen:
            seen.add(issue_id)
            result.append(issue_id)
    return result


async def _maybe_body(request: Request) -> Any:
    try:
        return await request.json()
    except Exception:
        return {}
