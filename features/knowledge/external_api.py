"""外部知识源联邦检索 API（ADR 0014）。

``/api/knowledge/external/search`` 供 Web、CLI 和 MCP 检索公共背景知识。
``/api/knowledge/android-internals/*`` 保留 Wiki 专用接口。
索引配置、修订审批和手动同步需要管理员提权。Provider 使用同步 I/O。
"""

from __future__ import annotations

import logging
import re
import sqlite3

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field

from features.auth import (
    require_agent_scope,
    require_elevated_admin,
    require_permission,
)
from features.retrieval import embedding_configuration_issue
from foundation.errors import handle_api_errors
from foundation.responses import error_response, success_response

from .external import (
    KNOWN_SOURCES,
    ExternalKnowledgeError,
    federated_reindex,
    federated_search,
    federated_status,
)
from .external.web_sources import WebKnowledgeProvider, serialize_clause


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
    if payload.sources:
        unknown = _validate_sources(payload.sources)
        if unknown:
            return error_response(unknown, 422)
    if query.startswith("analyze:"):
        question = query.removeprefix("analyze:").strip()
        if not question:
            return error_response("分析问题不能为空", 422)
        from .service import analyze_external_knowledge

        selected = payload.sources or ["web_documents"]
        result = analyze_external_knowledge(question, sources=selected, limit=payload.limit,
                                            android_api_level=payload.android_api_level)
        result["sources_status"] = [row for row in federated_status() if row.get("source") in selected]
        return success_response(data=result)
    if query.startswith(('context:', 'section:')):
        if payload.sources and 'web_documents' not in payload.sources:
            return error_response('请求的来源不包含外部网页', 422)
        match = re.fullmatch(r'(context|section):(POL-[0-9a-f]{24})(?:\|(\d{1,5}))?', query)
        if not match or int(match.group(3) or 0) > 30000:
            return error_response('读取格式：context:POL-引用|偏移 或 section:POL-引用|偏移', 422)
        mode, token, offset = match.groups()
        result = WebKnowledgeProvider().read_cdd_scope(
            token, mode=mode, offset=int(offset or 0), limit=payload.limit,
            android_api_level=payload.android_api_level,
        )
        result['sources_status'] = [row for row in federated_status() if row.get('source') == 'web_documents']
        if result['next_offset'] is not None:
            result['next_query'] = f"{mode}:{token}|{result['next_offset']}"
        return success_response(data=result)
    # Exact citation retrieval and version comparison use the existing search contract.
    if query.startswith('POL-') or query.startswith('compare:'):
        if payload.sources and 'web_documents' not in payload.sources:
            return error_response('请求的来源不包含外部网页', 422)
        provider = WebKnowledgeProvider()
        if query.startswith('POL-'):
            item = provider.read_requirement(query)
            return success_response(data={'results': [serialize_clause(item)] if item else [],
                                          'sources_status': federated_status()})
        parts = query.removeprefix('compare:').rsplit('|', 1)
        if len(parts) != 2 or not all(part.strip() for part in parts):
            return error_response('对比格式：compare:章节|条款编号', 422)
        rows = provider.compare_requirements(
            parts[0].strip(), parts[1].strip(),
            limit=payload.limit, android_api_level=payload.android_api_level,
        )
        return success_response(data={'results': [serialize_clause(row) for row in rows],
                                      'sources_status': federated_status(),
                                      'comparison_scope': 'current revisions; exact section and requirement ID'})
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
    url: str = Field(..., min_length=1, max_length=2048)
    name: str = Field('', max_length=160)
    enabled: bool | None = None
    authorized_for_ai_processing: bool = False


def _initial_sync(provider, source_id):
    try:
        return provider.sync(source_id)
    except Exception as exc:
        logger.warning('Web knowledge initial synchronization failed: %s', type(exc).__name__)
        return {'source_id': source_id, 'status': 'error'}

@router.get("/web-sources")
@handle_api_errors
def list_web_sources(request: Request):
    require_elevated_admin(request)
    return success_response(data={"sources": WebKnowledgeProvider().list_sources()})


@router.post("/web-sources")
@handle_api_errors
def create_web_source(request: Request, payload: WebSourceRequest):
    require_elevated_admin(request)
    provider = WebKnowledgeProvider()
    try:
        source_id = provider.save_url(**payload.model_dump())
    except (ValueError, sqlite3.IntegrityError):
        return error_response('知识源网址无效、重复或未经内容使用授权', 422)
    sync = _initial_sync(provider, source_id) if payload.enabled is not False else {'status': 'disabled'}
    return success_response(data={'id': source_id, 'sync': sync})


@router.put("/web-sources/{source_id}")
@handle_api_errors
def update_web_source(request: Request, source_id: str, payload: WebSourceRequest):
    require_elevated_admin(request)
    provider = WebKnowledgeProvider()
    try:
        provider.save_url(source_id=source_id, **payload.model_dump())
    except (ValueError, sqlite3.IntegrityError):
        return error_response('知识源配置无效或不存在；更换网址请添加新知识源', 422)
    source = next((row for row in provider.list_sources() if row['id'] == source_id), None)
    sync = _initial_sync(provider, source_id) if source and source['enabled'] and not source['revision'] else None
    return success_response(data={'id': source_id, 'sync': sync})


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


@router.get("/web-sources/{source_id}/semantic-index")
@handle_api_errors
def semantic_index_status(request: Request, source_id: str):
    require_elevated_admin(request)
    provider = WebKnowledgeProvider()
    result = {**provider.semantic_status(source_id), **provider.semantic_schedule_status(source_id)}
    from .external import cdd_intelligence

    result["document_understanding"] = cdd_intelligence.status(provider, source_id)
    issue = embedding_configuration_issue("cdd")
    if issue:
        result = {**result, "configuration_issue": issue}
    return success_response(data=result)


@router.post("/web-sources/{source_id}/semantic-index")
@handle_api_errors
def sync_semantic_index(request: Request, source_id: str, batch_size: int = Query(128, ge=1, le=128)):
    require_elevated_admin(request)
    try:
        result = WebKnowledgeProvider().sync_semantic(source_id, batch_size=batch_size)
    except Exception as exc:
        logger.warning("Local semantic index synchronization failed: %s", type(exc).__name__)
        return error_response("本地语义索引未完成，请检查模型配置和公开 CDD 来源", 502)
    return success_response(data=result)


@router.get("/wiki-semantic-index")
@handle_api_errors
def wiki_semantic_status(request: Request):
    require_elevated_admin(request)
    provider = _federated_provider("android_internals")
    result = (provider.semantic_status() if provider is not None
              else {"status": "unavailable", "indexed": 0, "total": 0})
    result = {**result, **WebKnowledgeProvider().semantic_schedule_status("android_internals", wiki=True)}
    from .service import wiki_document_understanding

    try:
        result["document_understanding"] = wiki_document_understanding()
    except Exception:
        result["document_understanding"] = {"status": "error", "coverage_complete": False}
    issue = embedding_configuration_issue("android_internals")
    if issue:
        result = {**result, "configuration_issue": issue}
    return success_response(data=result)


@router.post("/wiki-semantic-index")
@handle_api_errors
def sync_wiki_semantic_index(request: Request, batch_size: int = Query(128, ge=1, le=128)):
    require_elevated_admin(request)
    provider = _federated_provider("android_internals")
    if provider is None:
        return error_response("Wiki 知识源未配置", 409)
    try:
        return success_response(data=provider.sync_semantic(batch_size=batch_size))
    except Exception as exc:
        logger.warning("Wiki semantic index synchronization failed: %s", type(exc).__name__)
        return error_response("语义索引未完成，请检查本地模型及已批准的 Wiki 索引", 502)


class PartnerGrantRequest(BaseModel):
    enabled: bool
    authorized_for_ai_processing: bool = False


@router.get('/partner-policy-grant')
@handle_api_errors
def get_partner_policy_grant(request: Request):
    require_elevated_admin(request)
    return success_response(data={'enabled': False, 'available': False})


@router.put('/partner-policy-grant')
@handle_api_errors
def update_partner_policy_grant(request: Request, payload: PartnerGrantRequest):
    user = require_elevated_admin(request)
    if payload.enabled:
        return error_response('当前扫描来源无法验证账号访问权限，请导入有权使用的规范文件', 409)
    WebKnowledgeProvider().revoke_partner_grant(user.resource_owner_id)
    return success_response(data={'enabled': False, 'available': False})
