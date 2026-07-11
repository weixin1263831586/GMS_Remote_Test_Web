#!/usr/bin/env python3
"""
设备锁定管理模块
处理设备锁定、释放、状态查询等功能
"""

import contextlib
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from features.auth import auth_service
from foundation.config import settings


LOCK_TTL_SECONDS = 3600


class DeviceLockManager:
    """SQLite-backed device lock manager."""

    def __init__(self, db_path: str | Path | None = None):
        self.db_path = Path(db_path) if db_path is not None else None
        self.lock = threading.RLock()
        self._memory_conn: sqlite3.Connection | None = None
        self._initialized = False

    def _connect(self) -> sqlite3.Connection:
        if self.db_path is None:
            if self._memory_conn is None:
                self._memory_conn = sqlite3.connect(":memory:", check_same_thread=False)
                self._memory_conn.row_factory = sqlite3.Row
            conn = self._memory_conn
        else:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
        if not self._initialized:
            self._init_schema(conn)
        return conn

    def _init_schema(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS device_locks (
                device_id TEXT PRIMARY KEY,
                client_id TEXT NOT NULL,
                username TEXT NOT NULL,
                timestamp TEXT NOT NULL
            )
            """
        )
        conn.commit()
        self._initialized = True

    def _close_if_needed(self, conn: sqlite3.Connection) -> None:
        if self.db_path is not None:
            conn.close()

    def _cleanup_expired(self, conn: sqlite3.Connection) -> None:
        now = datetime.now()
        rows = conn.execute("SELECT device_id, timestamp FROM device_locks").fetchall()
        expired = []
        for row in rows:
            try:
                lock_time = datetime.fromisoformat(row["timestamp"])
            except (TypeError, ValueError):
                expired.append(row["device_id"])
                continue
            if (now - lock_time).total_seconds() >= LOCK_TTL_SECONDS:
                expired.append(row["device_id"])
        if expired:
            conn.executemany("DELETE FROM device_locks WHERE device_id = ?", [(item,) for item in expired])
            conn.commit()

    def _display_username(self, client_id: str, username: str | None) -> str:
        cleaned = str(username or "").strip()
        if cleaned and cleaned != "unknown":
            return cleaned
        with contextlib.suppress(Exception):
            for user in auth_service.list_users():
                if str(user.get("username") or "") == str(client_id):
                    return str(user.get("display_name") or user.get("username") or client_id)
        return cleaned or client_id

    def lock_device(
        self,
        device_id: str,
        client_id: str,
        username: str = 'unknown'
    ) -> tuple[bool, str | None]:
        """
        锁定设备

        返回: (success, message)
        """
        with self.lock:
            conn = self._connect()
            try:
                self._cleanup_expired(conn)
                row = conn.execute(
                    "SELECT * FROM device_locks WHERE device_id = ?",
                    (device_id,),
                ).fetchone()
                if row:
                    if row["client_id"] == client_id:
                        return True, f"设备 {device_id} 已锁定 (当前用户)"
                    return False, f"设备 {device_id} 已被 {row['username']} 锁定"

                cursor = conn.execute(
                    """
                    INSERT OR IGNORE INTO device_locks
                        (device_id, client_id, username, timestamp)
                    VALUES (?, ?, ?, ?)
                    """,
                    (device_id, client_id, username, datetime.now().isoformat()),
                )
                conn.commit()
                if cursor.rowcount == 1:
                    return True, f"设备 {device_id} 锁定成功"

                # Another process may have acquired the lock after our SELECT.
                # Resolve that normal race as a conflict instead of leaking an
                # IntegrityError from SQLite to the API caller.
                row = conn.execute(
                    "SELECT client_id, username FROM device_locks WHERE device_id = ?",
                    (device_id,),
                ).fetchone()
                if row and row['client_id'] == client_id:
                    return True, f"设备 {device_id} 已锁定 (当前用户)"
                owner = row['username'] if row else '其他用户'
                return False, f"设备 {device_id} 已被 {owner} 锁定"
            finally:
                self._close_if_needed(conn)

    def unlock_device(self, device_id: str, client_id: str) -> tuple[bool, str | None]:
        """
        解锁设备

        返回: (success, message)
        """
        with self.lock:
            conn = self._connect()
            try:
                self._cleanup_expired(conn)
                row = conn.execute(
                    "SELECT * FROM device_locks WHERE device_id = ?",
                    (device_id,),
                ).fetchone()
                if not row:
                    return True, f"设备 {device_id} 未锁定"
                if row["client_id"] != client_id:
                    return False, f"设备 {device_id} 被其他用户锁定，无法解锁"
                conn.execute("DELETE FROM device_locks WHERE device_id = ?", (device_id,))
                conn.commit()
                return True, f"设备 {device_id} 解锁成功"
            finally:
                self._close_if_needed(conn)

    def force_unlock_device(self, device_id: str) -> tuple[bool, str | None]:
        """强制解锁设备，不校验锁的持有者。"""
        with self.lock:
            conn = self._connect()
            try:
                self._cleanup_expired(conn)
                row = conn.execute(
                    "SELECT * FROM device_locks WHERE device_id = ?",
                    (device_id,),
                ).fetchone()
                if not row:
                    return True, f"设备 {device_id} 未锁定"
                conn.execute("DELETE FROM device_locks WHERE device_id = ?", (device_id,))
                conn.commit()
                return True, f"设备 {device_id} 已强制释放"
            finally:
                self._close_if_needed(conn)

    def get_lock_status(self, device_id: str) -> dict[str, Any] | None:
        """获取设备锁定状态"""
        with self.lock:
            conn = self._connect()
            try:
                self._cleanup_expired(conn)
                row = conn.execute(
                    "SELECT * FROM device_locks WHERE device_id = ?",
                    (device_id,),
                ).fetchone()
                if not row:
                    return None
                return {
                    'device_id': row['device_id'],
                    'locked': True,
                    'locked_by': self._display_username(row['client_id'], row['username']),
                    'client_id': row['client_id'],
                    'username': self._display_username(row['client_id'], row['username']),
                    'locked_at': row['timestamp'],
                }
            finally:
                self._close_if_needed(conn)

    def get_all_locks(self) -> dict[str, dict[str, Any]]:
        """获取所有设备锁定状态"""
        with self.lock:
            conn = self._connect()
            try:
                self._cleanup_expired(conn)
                rows = conn.execute("SELECT * FROM device_locks").fetchall()
                return {
                    row["device_id"]: {
                        'device_id': row['device_id'],
                        'client_id': row['client_id'],
                        'username': self._display_username(row['client_id'], row['username']),
                        'timestamp': row['timestamp'],
                    }
                    for row in rows
                }
            finally:
                self._close_if_needed(conn)

    def refresh_locks(self, client_id: str, device_ids: list[str]) -> int:
        """Renew locks still owned by ``client_id`` and return the renewed count."""
        cleaned = [str(device_id).strip() for device_id in device_ids if str(device_id).strip()]
        if not cleaned:
            return 0
        with self.lock:
            conn = self._connect()
            try:
                self._cleanup_expired(conn)
                timestamp = datetime.now().isoformat()
                renewed = 0
                for device_id in cleaned:
                    cursor = conn.execute(
                        "UPDATE device_locks SET timestamp = ? WHERE device_id = ? AND client_id = ?",
                        (timestamp, device_id, client_id),
                    )
                    renewed += cursor.rowcount
                conn.commit()
                return renewed
            finally:
                self._close_if_needed(conn)

    def unlock_all(self, client_id: str) -> int:
        """解锁客户端的所有设备"""
        with self.lock:
            conn = self._connect()
            try:
                self._cleanup_expired(conn)
                rows = conn.execute(
                    "SELECT device_id FROM device_locks WHERE client_id = ?",
                    (client_id,),
                ).fetchall()
                conn.execute("DELETE FROM device_locks WHERE client_id = ?", (client_id,))
                conn.commit()
                return len(rows)
            finally:
                self._close_if_needed(conn)


# 全局实例
device_lock_manager = DeviceLockManager(settings.data_root / "device_locks.sqlite3")
