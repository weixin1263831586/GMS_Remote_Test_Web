#!/usr/bin/env python3
"""
设备锁定管理模块
处理设备锁定、释放、状态查询等功能
"""

import threading
from datetime import datetime
from typing import Dict, Optional, Any


class DeviceLockManager:
    """设备锁定管理器"""

    def __init__(self):
        self.locks: Dict[str, Dict[str, Any]] = {}  # {device_id: lock_info}
        self.lock = threading.Lock()

    def lock_device(
        self,
        device_id: str,
        client_id: str,
        username: str = 'unknown'
    ) -> tuple[bool, Optional[str]]:
        """
        锁定设备

        返回: (success, message)
        """
        with self.lock:
            # 检查是否已被锁定
            if device_id in self.locks:
                lock_info = self.locks[device_id]
                # 同一客户端可以重复锁定
                if lock_info['client_id'] == client_id:
                    return True, f"设备 {device_id} 已锁定 (当前用户)"

                # 其他客户端锁定，检查是否过期（1小时）
                lock_time = datetime.fromisoformat(lock_info['timestamp'])
                if (datetime.now() - lock_time).total_seconds() < 3600:
                    return False, f"设备 {device_id} 已被 {lock_info['username']} 锁定"

            # 锁定设备
            self.locks[device_id] = {
                'device_id': device_id,
                'client_id': client_id,
                'username': username,
                'timestamp': datetime.now().isoformat()
            }

            return True, f"设备 {device_id} 锁定成功"

    def unlock_device(self, device_id: str, client_id: str) -> tuple[bool, Optional[str]]:
        """
        解锁设备

        返回: (success, message)
        """
        with self.lock:
            if device_id not in self.locks:
                return True, f"设备 {device_id} 未锁定"

            lock_info = self.locks[device_id]

            # 只能解锁自己的锁定
            if lock_info['client_id'] != client_id:
                return False, f"设备 {device_id} 被其他用户锁定，无法解锁"

            del self.locks[device_id]
            return True, f"设备 {device_id} 解锁成功"

    def get_lock_status(self, device_id: str) -> Optional[Dict[str, Any]]:
        """获取设备锁定状态"""
        with self.lock:
            if device_id not in self.locks:
                return None

            lock_info = self.locks[device_id]

            # 检查是否过期
            lock_time = datetime.fromisoformat(lock_info['timestamp'])
            if (datetime.now() - lock_time).total_seconds() >= 3600:
                del self.locks[device_id]
                return None

            return {
                'device_id': device_id,
                'locked': True,
                'locked_by': lock_info['client_id'],
                'locked_at': lock_info['timestamp']
            }

    def get_all_locks(self) -> Dict[str, Dict[str, Any]]:
        """获取所有设备锁定状态"""
        with self.lock:
            # 清理过期锁定
            now = datetime.now()
            expired = [
                device_id for device_id, info in self.locks.items()
                if (now - datetime.fromisoformat(info['timestamp'])).total_seconds() >= 3600
            ]
            for device_id in expired:
                del self.locks[device_id]

            return dict(self.locks)

    def unlock_all(self, client_id: str) -> int:
        """解锁客户端的所有设备"""
        with self.lock:
            unlocked_count = 0
            to_unlock = []

            for device_id, lock_info in self.locks.items():
                if lock_info['client_id'] == client_id:
                    to_unlock.append(device_id)

            for device_id in to_unlock:
                del self.locks[device_id]
                unlocked_count += 1

            return unlocked_count


# 全局实例
device_lock_manager = DeviceLockManager()
