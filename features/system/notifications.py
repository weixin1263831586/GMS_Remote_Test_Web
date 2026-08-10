"""通知管理 - 消息存储和推送"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from starlette.websockets import WebSocketState

from features.system.state import global_state
from foundation.config import (
    MAX_NOTIFICATIONS_PER_CLIENT,
    VALID_NOTIFICATION_LEVELS,
    settings,
)
from foundation.events import event_bus


logger = logging.getLogger(__name__)
_event_loop: asyncio.AbstractEventLoop | None = None


class NotificationStore:
    """Transactional, owner-scoped notification history."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self._schema_lock = threading.RLock()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _initialize(self) -> None:
        with self._schema_lock, self._open_connection() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS notifications (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    notification_id TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    title TEXT NOT NULL,
                    message TEXT NOT NULL,
                    level TEXT NOT NULL,
                    category TEXT NOT NULL,
                    is_read INTEGER NOT NULL DEFAULT 0,
                    data_json TEXT NOT NULL DEFAULT '{}',
                    UNIQUE(owner_id, notification_id)
                );
                CREATE INDEX IF NOT EXISTS idx_notifications_owner_sequence
                    ON notifications(owner_id, sequence DESC);
                """
            )
        self.db_path.chmod(0o600)

    def _open_connection(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.execute("PRAGMA busy_timeout=30000")
        conn.row_factory = sqlite3.Row
        return conn

    def _connect(self) -> sqlite3.Connection:
        conn = self._open_connection()
        schema_exists = conn.execute(
            """SELECT 1 FROM sqlite_master
               WHERE type='table' AND name='notifications'"""
        ).fetchone() is not None
        if not schema_exists:
            conn.close()
            self._initialize()
            conn = self._open_connection()
        return conn

    @staticmethod
    def _decode(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": str(row["notification_id"]),
            "timestamp": str(row["timestamp"]),
            "title": str(row["title"]),
            "message": str(row["message"]),
            "level": str(row["level"]),
            "category": str(row["category"]),
            "read": bool(row["is_read"]),
            "data": json.loads(str(row["data_json"])),
        }

    def upsert(self, owner_id: str, record: dict[str, Any]) -> dict[str, Any]:
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO notifications (
                       notification_id, owner_id, timestamp, title, message,
                       level, category, is_read, data_json
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(owner_id, notification_id) DO UPDATE SET
                       timestamp=excluded.timestamp,
                       title=excluded.title,
                       message=excluded.message,
                       level=excluded.level,
                       category=excluded.category,
                       is_read=excluded.is_read,
                       data_json=excluded.data_json""",
                (
                    record["id"],
                    owner_id,
                    record["timestamp"],
                    record["title"],
                    record["message"],
                    record["level"],
                    record["category"],
                    int(bool(record["read"])),
                    json.dumps(
                        record.get("data") or {},
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                ),
            )
            conn.execute(
                """DELETE FROM notifications
                   WHERE owner_id=? AND sequence NOT IN (
                       SELECT sequence FROM notifications
                       WHERE owner_id=? ORDER BY sequence DESC LIMIT ?
                   )""",
                (owner_id, owner_id, MAX_NOTIFICATIONS_PER_CLIENT),
            )
        return record

    def list(self, owner_id: str, limit: int) -> dict[str, Any]:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT * FROM notifications WHERE owner_id=?
                   ORDER BY sequence DESC LIMIT ?""",
                (owner_id, limit),
            ).fetchall()
            unread_count = int(conn.execute(
                """SELECT COUNT(*) FROM notifications
                   WHERE owner_id=? AND is_read=0""",
                (owner_id,),
            ).fetchone()[0])
        return {
            "records": [self._decode(row) for row in rows],
            "unread_count": unread_count,
        }

    def mark_read(self, owner_id: str, ids: list[str] | None) -> dict[str, Any]:
        with self._connect() as conn:
            if ids:
                unique_ids = list(dict.fromkeys(str(item) for item in ids))[:500]
                placeholders = ",".join("?" for _ in unique_ids)
                cursor = conn.execute(
                    f"""UPDATE notifications SET is_read=1
                        WHERE owner_id=? AND is_read=0
                        AND notification_id IN ({placeholders})""",
                    (owner_id, *unique_ids),
                )
            else:
                cursor = conn.execute(
                    """UPDATE notifications SET is_read=1
                       WHERE owner_id=? AND is_read=0""",
                    (owner_id,),
                )
            unread_count = int(conn.execute(
                """SELECT COUNT(*) FROM notifications
                   WHERE owner_id=? AND is_read=0""",
                (owner_id,),
            ).fetchone()[0])
        return {"updated": cursor.rowcount, "unread_count": unread_count}

    def clear(self, owner_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM notifications WHERE owner_id=?",
                (owner_id,),
            )
        return {"removed": cursor.rowcount, "unread_count": 0}


notification_store = NotificationStore(
    settings.data_root / "notifications" / "notifications.sqlite3"
)


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


async def broadcast_event(event_type: str, payload: dict[str, Any] | None = None):
    """Send a resource event to its owner, or globally when it is public."""
    public_payload = dict(payload or {})
    target_client_id = str(public_payload.pop("_target_client_id", "") or "")
    message = {"type": "event", "event": event_type, "payload": public_payload}
    with global_state.websocket_connections_lock:
        if target_client_id:
            client_ids = (
                [target_client_id]
                if target_client_id in global_state.websocket_connections
                else []
            )
        else:
            client_ids = list(global_state.websocket_connections.keys())
    await asyncio.gather(
        *(safe_websocket_send(client_id, message) for client_id in client_ids),
        return_exceptions=True,
    )


def bind_event_bus_loop(
    loop: asyncio.AbstractEventLoop | None = None,
) -> asyncio.AbstractEventLoop:
    """Bind the EventBus bridge to the ASGI loop used by this process."""
    global _event_loop

    _event_loop = loop or asyncio.get_running_loop()
    return _event_loop


def unbind_event_bus_loop(loop: asyncio.AbstractEventLoop | None = None) -> None:
    """Detach a previously bound ASGI loop during application shutdown."""
    global _event_loop

    if loop is None or _event_loop is loop:
        _event_loop = None


def _schedule_event_broadcast(event_type: str, payload: dict[str, Any]) -> None:
    loop = _event_loop
    if loop is None or loop.is_closed():
        return

    def create_task() -> None:
        task = loop.create_task(broadcast_event(event_type, payload))
        global_state.background_tasks.add(task)
        task.add_done_callback(global_state.background_tasks.discard)

    try:
        running_loop = asyncio.get_running_loop()
    except RuntimeError:
        running_loop = None
    try:
        if running_loop is loop:
            create_task()
        else:
            loop.call_soon_threadsafe(create_task)
    except RuntimeError:
        logger.debug("EventBus loop stopped before %s could be sent", event_type)


def _event_bus_listener(event_type: str, payload: dict[str, Any]) -> None:
    """Forward EventBus emissions to WebSocket clients via the ASGI loop.

    FastAPI runs synchronous endpoints in worker threads, so their current
    thread has no event loop.  The bridge therefore schedules onto the loop
    captured by the application lifespan.
    """
    _schedule_event_broadcast(event_type, dict(payload))


event_bus.subscribe("*", _event_bus_listener)


def store_notification(
    client_id: str,
    title: str,
    message: str = "",
    level: str = "info",
    category: str = "system",
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """持久化通知，供页面通知中心读取。

    前端同步通知时会通过 data._synced_id 和 data._synced_read
    传递原始 ID 和已读状态，此方法会自动识别并去重。
    """
    normalized_level = level if level in VALID_NOTIFICATION_LEVELS else 'info'
    # 在副本上移除传输元数据，避免修改调用方对象。
    data = dict(data or {})
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

    owner_id = str(client_id or '').strip()
    if not owner_id:
        raise ValueError('notification owner is required')
    return notification_store.upsert(owner_id, record)


async def push_notification(
    client_id: str,
    title: str,
    message: str = "",
    level: str = "info",
    category: str = "system",
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """保存并通过 WebSocket 推送通知。"""
    record = store_notification(client_id, title, message, level, category, data)
    await safe_websocket_send(client_id, {
        'type': 'notification',
        'notification': record
    })
    return record


def list_client_notifications(client_id: str, limit: int = 100) -> dict[str, Any]:
    """获取客户端通知列表"""
    limit = max(1, min(int(limit or 100), MAX_NOTIFICATIONS_PER_CLIENT))
    return notification_store.list(client_id, limit)


def mark_client_notifications_read(client_id: str, ids: list[str] | None = None) -> dict[str, Any]:
    """标记客户端通知为已读"""
    return notification_store.mark_read(client_id, ids)


def clear_client_notifications(client_id: str) -> dict[str, Any]:
    """清除客户端所有通知"""
    return notification_store.clear(client_id)
