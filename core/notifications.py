"""通知管理 - 消息存储和推送"""

import logging
import uuid
from collections import deque
from datetime import datetime
from typing import Any, Dict, List, Optional

from starlette.websockets import WebSocketState

from core.settings import MAX_NOTIFICATIONS_PER_CLIENT, VALID_NOTIFICATION_LEVELS
from core.state import global_state

logger = logging.getLogger(__name__)


async def safe_websocket_send(client_id: str, message: dict):
    """线程安全地发送WebSocket消息（带背压检查）"""
    with global_state.websocket_connections_lock:
        ws = global_state.websocket_connections.get(client_id)

    if ws:
        try:
            if ws.client_state == WebSocketState.DISCONNECTED:
                logger.debug(f"WebSocket {client_id} already disconnected")
                return

            if hasattr(ws, '_queue') and ws._queue.qsize() > 100:
                logger.warning(f"WebSocket buffer full for {client_id}, dropping message")
                return

            await ws.send_json(message)
        except Exception:
            logger.debug(f"Failed to send WebSocket message to {client_id}")


def store_notification(
    client_id: str,
    title: str,
    message: str = "",
    level: str = "info",
    category: str = "system",
    data: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """保存通知到内存历史，供页面通知中心读取。

    前端同步通知时会通过 data._synced_id 和 data._synced_read
    传递原始 ID 和已读状态，此方法会自动识别并去重。
    """
    normalized_level = level if level in VALID_NOTIFICATION_LEVELS else 'info'
    data = data or {}
    synced_id = data.pop('_synced_id', None)
    synced_read = data.pop('_synced_read', None)

    record = {
        'id': str(synced_id) if synced_id else str(uuid.uuid4()),
        'timestamp': datetime.now().isoformat(timespec='seconds'),
        'title': str(title or '通知')[:120],
        'message': str(message or '')[:600],
        'level': normalized_level,
        'category': str(category or 'system')[:50],
        'read': bool(synced_read) if synced_read is not None else False,
        'data': data,
    }

    key = client_id or 'unknown'
    with global_state.notifications_lock:
        if key not in global_state.notifications:
            global_state.notifications[key] = deque(maxlen=MAX_NOTIFICATIONS_PER_CLIENT)
        # Deduplicate: if notification with same id already exists, update it
        existing = global_state.notifications[key]
        for i, item in enumerate(existing):
            if item.get('id') == record['id']:
                existing[i] = record
                return record
        existing.append(record)
    return record


async def push_notification(
    client_id: str,
    title: str,
    message: str = "",
    level: str = "info",
    category: str = "system",
    data: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """保存并通过 WebSocket 推送通知。"""
    record = store_notification(client_id, title, message, level, category, data)
    await safe_websocket_send(client_id, {
        'type': 'notification',
        'notification': record
    })
    return record


def list_client_notifications(client_id: str, limit: int = 100) -> Dict[str, Any]:
    """获取客户端通知列表"""
    limit = max(1, min(int(limit or 100), MAX_NOTIFICATIONS_PER_CLIENT))
    key = client_id or 'unknown'
    with global_state.notifications_lock:
        records = list(global_state.notifications.get(key, []))
    records = list(reversed(records))[:limit]
    unread_count = sum(1 for record in records if not record.get('read'))
    return {'records': records, 'unread_count': unread_count}


def mark_client_notifications_read(client_id: str, ids: Optional[List[str]] = None) -> Dict[str, Any]:
    """标记客户端通知为已读"""
    key = client_id or 'unknown'
    id_set = set(ids or [])
    updated = 0
    with global_state.notifications_lock:
        records = global_state.notifications.get(key, deque())
        for record in records:
            if not id_set or record.get('id') in id_set:
                if not record.get('read'):
                    record['read'] = True
                    updated += 1
        unread_count = sum(1 for record in records if not record.get('read'))
    return {'updated': updated, 'unread_count': unread_count}


def clear_client_notifications(client_id: str) -> Dict[str, Any]:
    """清除客户端所有通知"""
    key = client_id or 'unknown'
    with global_state.notifications_lock:
        removed = len(global_state.notifications.get(key, []))
        global_state.notifications[key] = deque(maxlen=MAX_NOTIFICATIONS_PER_CLIENT)
    return {'removed': removed, 'unread_count': 0}
