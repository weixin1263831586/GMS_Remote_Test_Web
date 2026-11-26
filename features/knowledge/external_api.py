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
import sqlite3
from typing import Literal

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
from .external.web_sources import WebKnowledgeProvider


def _federated_provider(source_id: str):
    """取联邦单例中的 provider（未配置返回 None）；复用进程级懒加载。"""
    from .external import federated_service

    return federated_service().provider(source_id)


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
    android_api_level: int | None = Field(None, ge=1, le=1000)


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
    # Exact citation retrieval and version comparison use the existing search contract.
    if query.startswith('POL-') or query.startswith('compare:'):
        if payload.sources and 'web_documents' not in payload.sources:
            return error_response('请求的来源不包含外部网页', 422)
        provider = WebKnowledgeProvider()
        if query.startswith('POL-'):
            item = provider.read_requirement(query)
            return success_response(data={'results': [item] if item else [], 'sources_status': federated_status()})
        parts = query.removeprefix('compare:').rsplit('|', 1)
        if len(parts) != 2 or not all(part.strip() for part in parts):
            return error_response('对比格式：compare:章节|条款编号', 422)
        rows = provider.compare_requirements(parts[0].strip(), parts[1].strip())
        return success_response(data={'results': rows, 'sources_status': federated_status(),
                                      'comparison_scope': 'current revisions; exact section and requirement ID'})
    if payload.sources:
        unknown = _validate_sources(payload.sources)
        if unknown:
            return error_response(unknown, 422)
    try:
        data = federated_search(
            query,
            sources=payload.sources,
            limit=payload.limit,
            android_api_level=payload.android_api_level,
        )
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
        # 并发重建 / 未配置 / clone 无效 / policy 不可用：语义化 4xx，而非 500。
        return error_response(str(exc), 409)
    except Exception as exc:
        logger.warning("external knowledge reindex failed: %s", exc)
        return error_response("外部知识索引重建失败", 502)
    return success_response(data=data, message="External knowledge reindex complete")


@router.post("/approve-revision")
@handle_api_errors
def approve_external_revision(request: Request):
    """管理员批准当前 clone HEAD 为索引目标（ADR 0014 revision 三态）。

    不自动跟随上游 master：批准是显式动作，随后 reindex 才把批准的
    revision 真正索引。clone 缺新提交时批准后 status 会显示
    "已批准待索引" 状态。
    """
    require_elevated_admin(request)
    provider = _federated_provider(KNOWN_SOURCES[0])
    if provider is None:
        return error_response(f"外部知识源未配置: {KNOWN_SOURCES[0]}", 409)
    approve = getattr(provider, "approve_revision", None)
    if approve is None:
        return error_response(f"外部知识源不支持批准 revision: {KNOWN_SOURCES[0]}", 409)
    try:
        data = approve()
    except ExternalKnowledgeError as exc:
        return error_response(str(exc), 409)
    except Exception as exc:
        logger.warning("external knowledge revision approval failed: %s", exc)
        return error_response("批准 revision 失败", 502)
    return success_response(data=data, message="Revision approved; run reindex to apply")


@agent_router.get("/search")
@handle_api_errors
def agent_search_android_internals(
    request: Request,
    q: str = Query(..., min_length=1, max_length=MAX_QUERY_LENGTH),
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    android_api_level: int | None = Query(None, ge=1, le=1000),
):
    require_agent_scope("knowledge.read")(request)
    try:
        data = federated_search(
            q,
            sources=[KNOWN_SOURCES[0]],
            limit=limit,
            android_api_level=android_api_level,
        )
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


class WebSourceRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=160)
    url: str = Field(..., min_length=1, max_length=2048)
    kind: Literal["cdd", "document"] = "document"
    android_version: int | None = Field(None, ge=1, le=100)
    license: str = Field(..., min_length=1, max_length=240)
    enabled: bool = True


@router.get("/web-sources")
@handle_api_errors
def list_web_sources(request: Request):
    require_elevated_admin(request)
    return success_response(data={"sources": WebKnowledgeProvider().list_sources()})


@router.post("/web-sources")
@handle_api_errors
def create_web_source(request: Request, payload: WebSourceRequest):
    require_elevated_admin(request)
    try:
        source_id = WebKnowledgeProvider().save(**payload.model_dump())
    except (ValueError, sqlite3.IntegrityError):
        return error_response("知识源配置无效、重复或访问方式不受支持", 422)
    return success_response(data={"id": source_id})


@router.put("/web-sources/{source_id}")
@handle_api_errors
def update_web_source(request: Request, source_id: str, payload: WebSourceRequest):
    require_elevated_admin(request)
    try:
        WebKnowledgeProvider().save(source_id=source_id, **payload.model_dump())
    except (ValueError, sqlite3.IntegrityError):
        return error_response("知识源配置无效、重复或不存在", 422)
    return success_response(data={"id": source_id})


@router.delete("/web-sources/{source_id}")
@handle_api_errors
def delete_web_source(request: Request, source_id: str):
    require_elevated_admin(request)
    WebKnowledgeProvider().delete(source_id)
    return success_response(data={"id": source_id})


@router.post("/web-sources/{source_id}/sync")
@handle_api_errors
def sync_web_source(request: Request, source_id: str):
    require_elevated_admin(request)
    try:
        data = WebKnowledgeProvider().sync(source_id)
    except Exception as exc:
        logger.warning("Web knowledge synchronization failed: %s", type(exc).__name__)
        return error_response("知识源同步失败；请检查网址、访问权限和文档格式，已有索引未覆盖", 502)
    return success_response(data=data)


class PartnerGrantRequest(BaseModel):
    enabled: bool
    authorized_for_ai_processing: bool = False


@router.get('/partner-policy-grant')
@handle_api_errors
def get_partner_policy_grant(request: Request):
    user = require_elevated_admin(request)
    return success_response(data={'enabled': WebKnowledgeProvider().has_partner_grant(user.resource_owner_id)})


@router.put('/partner-policy-grant')
@handle_api_errors
def update_partner_policy_grant(request: Request, payload: PartnerGrantRequest):
    user = require_elevated_admin(request)
    if payload.enabled and not payload.authorized_for_ai_processing:
        return error_response('需要确认当前账号有权访问并向配置的分析模型提供 GMS 政策内容', 422)
    try:
        WebKnowledgeProvider().set_partner_grant(user.resource_owner_id, payload.enabled)
    except ValueError:
        return error_response('当前模型目录无法验证，请先配置分析模型', 422)
    return success_response(data={'enabled': payload.enabled})
