"""Notifications router - client notification management APIs."""

import logging

from fastapi import APIRouter, Query, Request

from core.api_response import ApiResponse
from core.clients import get_client_id_from_request
from core.error_handling import handle_api_errors
from core.notifications import (
    clear_client_notifications,
    list_client_notifications,
    mark_client_notifications_read,
    store_notification,
)
from core.schemas import NotificationCreateRequest, NotificationReadRequest

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/api/notifications")
@handle_api_errors
async def get_notifications(request: Request, limit: int = Query(100, ge=1, le=200)):
    """获取当前客户端通知列表。"""
    client_id = get_client_id_from_request(request)
    return ApiResponse.success(list_client_notifications(client_id, limit))


@router.post("/api/notifications")
@handle_api_errors
async def create_notification(req: NotificationCreateRequest, request: Request):
    """创建当前客户端本地通知，用于前端检测到的状态变化。"""
    client_id = get_client_id_from_request(request)
    record = store_notification(
        client_id,
        req.title,
        req.message,
        req.level,
        req.category,
        req.data
    )
    return ApiResponse.success({'notification': record})


@router.post("/api/notifications/mark-read")
@handle_api_errors
async def mark_notifications_read(req: NotificationReadRequest, request: Request):
    """将当前客户端通知标记为已读；未传 ids 时标记全部。"""
    client_id = get_client_id_from_request(request)
    return ApiResponse.success(mark_client_notifications_read(client_id, req.ids))


@router.post("/api/notifications/clear")
@handle_api_errors
async def clear_notifications(request: Request):
    """清空当前客户端通知。"""
    client_id = get_client_id_from_request(request)
    return ApiResponse.success(clear_client_notifications(client_id))
