"""Security audit router - audit logging and reporting APIs."""

import asyncio
import logging
import os
from collections import deque
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import FileResponse

from features.auth import CurrentUser, require_elevated_admin_when_auth_required
from features.system.models import SecurityPageViewRequest
from features.system.security_audit import security_audit_logger
from features.system.security_audit_utils import AUDIT_PAGE_VIEW_SKIP_PAGES
from features.system.state import global_state
from features.users import get_client_id_from_request, parse_client_id
from foundation.errors import handle_api_errors
from foundation.responses import ApiResponse


logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/api/security-audit/page-view")
@handle_api_errors
async def record_security_page_view(req: SecurityPageViewRequest, request: Request):
    """记录前端子页面访问，用于补齐 hash 路由无法被后端直接感知的问题。"""
    if req.page in AUDIT_PAGE_VIEW_SKIP_PAGES:
        return ApiResponse.success({'skipped': True})

    client_id = get_client_id_from_request(request)
    username, client_ip = parse_client_id(client_id)
    record = security_audit_logger.log_event({
        'action_type': 'page_view',
        'source': 'web',
        'operation': f"访问页面 {req.page}",
        'page': req.page,
        'title': req.title or '',
        'hash': req.hash or '',
        'method': request.method,
        'path': '/#' + req.page,
        'status_code': 200,
        'duration_ms': 0,
        'client_ip': client_ip,
        'client_id': client_id,
        'username': username,
        'user_agent': request.headers.get('user-agent', '')[:300],
    })
    return ApiResponse.success({'id': record['id']})


@router.get("/api/security-audit/logs")
@handle_api_errors
async def list_security_audit_logs(
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    source: str | None = Query(None),
    action_type: str | None = Query(None),
    q: str | None = Query(None, max_length=120),
    _admin: CurrentUser | None = Depends(require_elevated_admin_when_auth_required),
):
    """查询安全审计记录（支持分页）。"""
    if source and source not in {'web', 'cli'}:
        return ApiResponse.error("source 参数无效", status_code=400)
    if action_type and action_type not in {'api', 'page_view', 'page_visit'}:
        return ApiResponse.error("action_type 参数无效", status_code=400)
    result = await asyncio.to_thread(
        security_audit_logger.read_events,
        limit,
        offset,
        source,
        action_type,
        q
    )
    return ApiResponse.success(result)


def get_related_logs_for_audit(record: dict[str, Any], limit: int = 80) -> dict[str, Any]:
    client_id = record.get('client_id') or ''
    related = {
        'client_id': client_id,
        'recent_client_logs': [],
        'saved_log_file': '',
        'saved_log_tail': [],
    }
    if not client_id:
        return related

    with global_state.test_logs_lock:
        related['recent_client_logs'] = list(global_state.test_logs.get(client_id, []))[-limit:]
        saved_log_file = global_state.last_saved_log_file.get(client_id, '')

    if saved_log_file and os.path.exists(saved_log_file):
        related['saved_log_file'] = saved_log_file
        try:
            with open(saved_log_file, encoding='utf-8', errors='replace') as f:
                related['saved_log_tail'] = list(deque(f, maxlen=limit))
        except Exception as e:
            related['saved_log_tail'] = [f'读取关联日志失败: {e}']

    return related


@router.get("/api/security-audit/detail/{event_id}")
@handle_api_errors
async def get_security_audit_detail(
    event_id: str,
    _admin: CurrentUser | None = Depends(require_elevated_admin_when_auth_required),
):
    """获取单条安全审计详情，包括请求摘要、响应摘要和关联日志。"""
    record = await asyncio.to_thread(security_audit_logger.get_event, event_id)
    if not record:
        return ApiResponse.error("审计记录不存在", status_code=404)

    related_logs = await asyncio.to_thread(get_related_logs_for_audit, record)
    return ApiResponse.success({
        'record': record,
        'related_logs': related_logs,
    })


@router.get("/api/security-audit/export")
@handle_api_errors
async def export_security_audit_logs(
    _admin: CurrentUser | None = Depends(require_elevated_admin_when_auth_required),
):
    """导出安全审计 JSONL 文件。"""
    if not os.path.exists(security_audit_logger.log_path):
        return ApiResponse.error("暂无审计记录", status_code=404)
    return FileResponse(
        security_audit_logger.log_path,
        media_type='application/x-ndjson',
        filename='security_audit.json'
    )


@router.get("/api/security-audit/verify")
@handle_api_errors
async def verify_security_audit_chain(
    _admin: CurrentUser | None = Depends(require_elevated_admin_when_auth_required),
):
    """Verify the HMAC chain and report its current signed head."""
    result = await asyncio.to_thread(security_audit_logger.verify_chain)
    if not result.get('valid'):
        return ApiResponse.error(
            "审计日志完整性校验失败",
            status_code=409,
            data=result,
        )
    return ApiResponse.success(result)
