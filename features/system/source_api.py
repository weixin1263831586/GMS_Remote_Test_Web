"""SDK 源码搜索/读取 API。

- ``GET /api/sdk/sources``          列出管理员配置的 provider
- ``GET /api/sdk/revision``         解析 revision -> commit 元数据
- ``GET /api/sdk/search``           在指定 commit 中搜索
- ``GET /api/sdk/read``             通过 result_id 分段读取

所有端点要求 ``sdk.read`` scope；provider 配置只来自服务端 config。

Handler 全部为普通 ``def``：provider 内部是同步阻塞 I/O
（urllib 30s 超时、local_git 逐文件 ``git show`` 子进程），声明为
``async def`` 会把阻塞放到事件循环上，一次慢查询就卡停整个 Web 进程；
普通 ``def`` 由 FastAPI 丢线程池执行。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from features.auth import require_agent_scope
from foundation.errors import handle_api_errors

from .source_provider import SourceProviderError, source_registry


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/sdk")


def _error(exc: SourceProviderError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code, content={"success": False, "error": str(exc)}
    )


@router.get("/sources")
@handle_api_errors
def list_sdk_sources(request: Request):
    require_agent_scope("sdk.read")(request)
    try:
        sources = source_registry().list_sources()
    except SourceProviderError as exc:
        return _error(exc)
    return {"success": True, "data": {"sources": sources}}


@router.get("/revision")
@handle_api_errors
def sdk_revision_metadata(
    request: Request,
    source: str = Query(..., min_length=1, max_length=128),
    revision: str = Query("", max_length=256),
):
    require_agent_scope("sdk.read")(request)
    try:
        provider = source_registry().get(source)
        metadata = provider.revision_metadata(revision)
    except SourceProviderError as exc:
        return _error(exc)
    return {"success": True, "data": metadata}


@router.get("/search")
@handle_api_errors
def sdk_search(
    request: Request,
    source: str = Query(..., min_length=1, max_length=128),
    revision: str = Query(..., min_length=1, max_length=256),
    query: str = Query(..., min_length=1, max_length=256),
    path_filter: str = Query("", max_length=256),
    limit: int = Query(50, ge=1, le=200),
):
    require_agent_scope("sdk.read")(request)
    try:
        provider = source_registry().get(source)
        result = provider.search(
            revision, query, path_filter=path_filter, limit=limit
        )
    except SourceProviderError as exc:
        return _error(exc)
    return {"success": True, "data": result}


@router.get("/read")
@handle_api_errors
def sdk_read(
    request: Request,
    result_id: str = Query(..., min_length=8, max_length=2048),
    offset: int = Query(0, ge=0),
    limit: int = Query(400, ge=1, le=4000),
):
    """通过自包含 opaque result_id 分段读取；不接受自由 path/commit。"""
    require_agent_scope("sdk.read")(request)
    try:
        result = source_registry().read_signed(
            result_id, offset=offset, limit=limit
        )
    except SourceProviderError as exc:
        return _error(exc)
    return {"success": True, "data": result}
