"""外部知识源联邦检索 API（ADR 0014）。

两组路由：

- ``/api/knowledge/external/*``（登录用户会话 + assistant 工具）：
  sources 状态、联邦检索、管理员 reindex；
- ``/api/knowledge/android-internals/*``（Agent Service Token）：
  ``knowledge.read`` scope 门禁，供 ``gms-rt-knowledge-search`` /
  ``gms_rt_knowledge_search`` 消费。

外部知识是 background-only：所有端点只读检索，唯一写路径是管理员显式
reindex。Handler 全部为普通 ``def``——provider 内部是同步阻塞 I/O
（SQLite + git 子进程），声明 async 会卡停事件循环（同 source_api.py）。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field

from features.auth import (
    require_agent_scope,
    require_elevated_admin,
    require_permission,
)
from foundation.errors import handle_api_errors
from foundation.responses import error_response, success_response

from .external import (
    KNOWN_SOURCES,
    ExternalKnowledgeError,
    federated_reindex,
    federated_search,
    federated_status,
)


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/external")
agent_router = APIRouter(prefix="/android-internals")

MAX_LIMIT = 10
DEFAULT_LIMIT = 5
MAX_QUERY_LENGTH = 512


class ExternalSearchRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=MAX_QUERY_LENGTH)
    sources: list[str] = Field(default_factory=list, max_length=8)
    limit: int = Field(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT)


def _validate_sources(sources: list[str]) -> str | None:
    unknown = [s for s in sources if s not in KNOWN_SOURCES]
    return f"未知外部知识源: {', '.join(unknown)}" if unknown else None


@router.get("/sources")
@handle_api_errors
def list_external_sources(request: Request):
    require_permission("knowledge.read")(request)
    return success_response(data={"sources": federated_status()})


@router.post("/search")
@handle_api_errors
def search_external(request: Request, payload: ExternalSearchRequest):
    require_permission("knowledge.read")(request)
    query = payload.query.strip()
    if not query:
        return error_response("检索词不能为空", 422)
    if payload.sources:
        unknown = _validate_sources(payload.sources)
        if unknown:
            return error_response(unknown, 422)
    try:
        data = federated_search(query, sources=payload.sources, limit=payload.limit)
    except Exception as exc:
        logger.warning("external knowledge search degraded: %s", exc)
        data = {"results": [], "sources_status": federated_status()}
    return success_response(data=data)


@router.post("/reindex")
@handle_api_errors
def reindex_external(request: Request):
    require_elevated_admin(request)
    try:
        data = federated_reindex(KNOWN_SOURCES[0])
    except ExternalKnowledgeError as exc:
        # 并发重建 / 未配置 / clone 无效：语义化 4xx，而非 500。
        return error_response(str(exc), 409)
    except Exception as exc:
        logger.warning("external knowledge reindex failed: %s", exc)
        return error_response("外部知识索引重建失败", 502)
    return success_response(data=data, message="External knowledge reindex complete")


@agent_router.get("/search")
@handle_api_errors
def agent_search_android_internals(
    request: Request,
    q: str = Query(..., min_length=1, max_length=MAX_QUERY_LENGTH),
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
):
    require_agent_scope("knowledge.read")(request)
    try:
        data = federated_search(q, sources=[KNOWN_SOURCES[0]], limit=limit)
    except Exception as exc:
        logger.warning("agent external search degraded: %s", exc)
        data = {"results": [], "sources_status": federated_status()}
    return success_response(data=data)


@agent_router.get("/status")
@handle_api_errors
def agent_android_internals_status(request: Request):
    require_agent_scope("knowledge.read")(request)
    rows = [s for s in federated_status() if s.get("source") == KNOWN_SOURCES[0]]
    return success_response(data={"sources": rows})
