"""统一知识库网关：聚合 notes 文档 + Redmine 成熟案例，带来源标记。

不修改 reports / redmine 后端，仅只读调用其查询方法。路由 prefix=/api/knowledge。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Query, Request

from features.users import get_client_id_from_request
from foundation.errors import handle_api_errors
from foundation.responses import success_response

from . import relations as _relations
from .service import NotesService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/knowledge")

_service = NotesService()


def _user(request: Request) -> str:
    return get_client_id_from_request(request)


def _collect_notes(request: Request, q: str, limit: int, notebook: str) -> list[dict[str, Any]]:
    """从 notes 检索，统一加 source 标记 + id 前缀。"""
    user_id = _user(request)
    try:
        if q.strip():
            notes = _service.storage.search(user_id, q.strip(), limit=limit)
        else:
            notes = _service.storage.list_notes(user_id, notebook=notebook, limit=limit)
    except Exception as e:
        logger.debug("[knowledge/unified] notes lookup failed: %s", e)
        return []
    items: list[dict[str, Any]] = []
    for n in notes:
        items.append(
            {
                "id": f"note:{n.get('note_id')}",
                "source": "note",
                "note_id": n.get("note_id"),
                "title": n.get("title"),
                "summary": n.get("summary"),
                "tags": n.get("tags"),
                "notebook": n.get("notebook"),
                "related_module": n.get("related_module"),
                "updated_at": n.get("updated_at") or n.get("created_at"),
                "preview": (n.get("content") or "")[:200],
            }
        )
    return items


def _collect_redmine_cases(request: Request, q: str, limit: int) -> list[dict[str, Any]]:
    """从 Redmine 成熟案例检索，统一加 source 标记 + id 前缀。"""
    try:
        from features.redmine.api import get_redmine_service_for_request

        service = get_redmine_service_for_request(request)
        payload = service.knowledge.list_mature_cases(limit=limit, search=q.strip())
        items = payload.get("items", []) if isinstance(payload, dict) else (payload or [])
    except Exception as e:
        logger.debug("[knowledge/unified] redmine mature-cases lookup failed: %s", e)
        return []
    out: list[dict[str, Any]] = []
    for c in items:
        out.append(
            {
                "id": f"case:{c.get('case_id')}",
                "source": "redmine_case",
                "case_id": c.get("case_id"),
                "title": c.get("title"),
                "module": c.get("module"),
                "chip_platform": c.get("chip_platform"),
                "android_version": c.get("android_version"),
                "canonical_error_signature": c.get("canonical_error_signature"),
                "status": c.get("status"),
                "updated_at": c.get("updated_at") or c.get("created_at"),
            }
        )
    return out


@router.get("/unified")
@handle_api_errors
async def unified_search(
    request: Request,
    q: str = Query(""),
    source: str = Query("all"),  # all | notes | redmine_case
    notebook: str = Query(""),
    limit: int = Query(50, ge=1, le=200),
):
    """统一知识库搜索：合并 notes 文档与 Redmine 成熟案例，按来源标记区分。"""
    half = max(1, limit) if source != "all" else max(1, limit // 2)
    notes_items: list[dict[str, Any]] = []
    redmine_items: list[dict[str, Any]] = []

    if source in ("all", "notes"):
        notes_items = _collect_notes(request, q, limit if source == "notes" else half, notebook)
    if source in ("all", "redmine_case"):
        redmine_items = _collect_redmine_cases(request, q, limit if source == "redmine_case" else half)

    merged = notes_items + redmine_items
    return success_response(
        data={
            "items": merged,
            "count": len(merged),
            "notes_count": len(notes_items),
            "redmine_count": len(redmine_items),
        }
    )


@router.get("/presets")
@handle_api_errors
async def preset_notebooks(request: Request):
    """返回 Wiki 预置固定分类列表（前端侧栏固定渲染用）。"""
    from .storage import PRESET_NOTEBOOKS

    return success_response(data={"notebooks": PRESET_NOTEBOOKS})


@router.get("/related-by-module")
@handle_api_errors
async def related_by_module(
    request: Request,
    module: str = Query(""),
    test_case: str = Query(""),
):
    """按 module 直接聚合相关报告 + Redmine 成熟案例（无需 note_id）。"""
    related_module = f"{module}::{test_case}" if module else ""
    result = _relations.build_related(request, related_module, note_id="")
    return success_response(data=result)
