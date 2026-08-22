"""Report diagnosis knowledge-base HTTP endpoints."""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Query, Request

from features.auth import require_authenticated_user
from foundation.redaction import redact_sensitive_text
from foundation.responses import error_response, success_response

from .api_helpers import _get_knowledge_base


logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/api/knowledgebase/search")
async def knowledgebase_search(
    request: Request,
    query: str = Query(..., min_length=1, max_length=256),
    limit: int = Query(8, ge=1, le=20),
):
    """Search the local Redmine-derived GMS knowledge base."""

    require_authenticated_user(request)
    try:
        kb = _get_knowledge_base(request)
        if not kb:
            return success_response({
                "query": query.strip(),
                "results": [],
                "count": 0,
            })
        results = await asyncio.to_thread(
            kb.search_similar,
            query.strip(),
            limit,
        )
        return success_response({
            "query": query.strip(),
            "results": results,
            "count": len(results),
        })
    except Exception as exc:
        logger.error("Knowledge base search failed: %s", redact_sensitive_text(exc))
        return error_response("Knowledge base search failed", status_code=500)


@router.get("/api/knowledgebase/stats")
async def knowledgebase_stats(request: Request):
    """Return local Redmine-derived GMS knowledge base stats."""

    require_authenticated_user(request)
    try:
        kb = _get_knowledge_base(request)
        if not kb:
            return success_response({"stats": {"total": 0, "mature_cases": 0}})
        data = await asyncio.to_thread(lambda: kb.list_case_facts(limit=1))
        mature = await asyncio.to_thread(lambda: kb.list_mature_cases(limit=1))
        return success_response({
            "stats": {
                "total": data.get("total", 0),
                "mature_cases": mature.get("total", 0),
            }
        })
    except Exception as exc:
        logger.error("Knowledge base stats failed: %s", redact_sensitive_text(exc))
        return error_response("Knowledge base stats failed", status_code=500)
