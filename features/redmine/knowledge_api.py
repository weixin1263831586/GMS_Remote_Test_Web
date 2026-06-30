"""HTTP endpoints for the Redmine knowledge base.

All routes are per-user (resolved through ``get_redmine_service_for_request``).
Mounted under the existing ``/api/redmine-agent`` prefix via
``router.include_router`` (see ``api.py``).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from features.auth.service import require_authenticated_user

from .api import get_redmine_service_for_request

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


# ------------------------------------------------------------------
# Batch import + case facts
# ------------------------------------------------------------------

@router.post("/issues/batch-import")
async def batch_import_issues(request: Request):
    body = await request.json()
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
    body = await request.json() if await _maybe_body(request) else {}
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


# ------------------------------------------------------------------
# Similarity search
# ------------------------------------------------------------------

@router.post("/search/similar")
async def search_similar(request: Request, limit: int = Query(10, ge=1, le=50)):
    body = await request.json()
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
        # Surface the failure as a real error (non-2xx + success:false) so the
        # frontend's catch branch shows "知识面板加载失败". The previous body
        # returned 200/success:true with a degraded payload, which made a crash
        # indistinguishable from genuinely-empty data.
        return JSONResponse(
            status_code=502,
            content={
                "success": False,
                "error": f"知识依据加载失败: {exc}",
                "issue_id": int(issue_id),
            },
        )


# ------------------------------------------------------------------
# Mature cases
# ------------------------------------------------------------------

@router.post("/mature-cases/build")
async def build_mature_case(request: Request):
    body = await request.json()
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


# ------------------------------------------------------------------
# Reply drafting
# ------------------------------------------------------------------

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


# ------------------------------------------------------------------
# Reference outputs + evaluation (off the production path)
# ------------------------------------------------------------------

@router.post("/issues/{issue_id}/reference-output")
async def import_reference_output(issue_id: int, request: Request):
    body = await request.json()
    return {"success": True, "data": _knowledge(request).import_reference_output(issue_id, body)}


@router.get("/issues/{issue_id}/reference-outputs")
async def list_reference_outputs(issue_id: int, request: Request):
    return {"success": True, "data": {"items": _knowledge(request).list_reference_outputs(issue_id)}}


@router.post("/issues/{issue_id}/evaluate-case")
async def evaluate_case(issue_id: int, request: Request):
    body = await request.json() if await _maybe_body(request) else {}
    reference = body.get("reference") if body else None
    return {"success": True, "data": _knowledge(request).evaluate_case(issue_id, reference=reference)}


@router.get("/issues/{issue_id}/evaluation")
async def latest_evaluation(issue_id: int, request: Request):
    evaluation = _knowledge(request).latest_evaluation(issue_id)
    return {"success": True, "data": evaluation}


# ------------------------------------------------------------------
# Internal issue creation (confirmed, configurable)
# ------------------------------------------------------------------

@router.post("/issues/{issue_id}/create-internal")
async def create_internal_from_issue(issue_id: int, request: Request):
    body = await request.json()
    confirmed = bool(body.get("confirmed", False))
    payload = {k: v for k, v in body.items() if k != "confirmed"}
    payload.setdefault("created_by", _approver(request))
    return await _knowledge(request).create_internal_from_issue(issue_id, payload, confirmed=confirmed)


@router.post("/mature-cases/{case_id}/create-internal")
async def create_internal_from_case(case_id: int, request: Request):
    body = await request.json()
    confirmed = bool(body.get("confirmed", False))
    payload = {k: v for k, v in body.items() if k != "confirmed"}
    payload.setdefault("created_by", _approver(request))
    return await _knowledge(request).create_internal_from_case(case_id, payload, confirmed=confirmed)


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

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
