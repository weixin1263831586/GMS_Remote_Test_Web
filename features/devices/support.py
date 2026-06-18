from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import datetime

from fastapi import HTTPException
from fastapi.responses import JSONResponse

from . import runtime
from .locks import device_lock_manager
from .manager import device_manager


logger = logging.getLogger(__name__)

# ==================== Device route/service helpers ====================



# ==================== 设备锁管理 ====================

async def release_device_locks(client_id: str, device_ids: list[str], broadcast: bool = True):
    """
    批量释放设备锁并广播更新

    Args:
        client_id: 客户端ID
        device_ids: 要释放的设备ID列表
        broadcast: 是否广播设备锁更新
    """
    if not device_ids:
        return

    for device_id in device_ids:
        device_lock_manager.unlock_device(device_id, client_id)

    if broadcast:
        await broadcast_device_lock_update(device_ids)


# ==================== 设备变更广播 ====================

async def broadcast_device_change(devices: list[str], disconnected: list[str] | None = None, connected: list[str] | None = None, source: str = 'usb_monitor'):
    """广播设备变化事件到所有 WebSocket 客户端

    Args:
        devices: 当前设备列表
        disconnected: 断开的设备列表（可选）
        connected: 连接的设备列表（可选）
        source: 事件来源 ('usb_monitor' 或 'usbip_disconnect')
    """
    from starlette.websockets import WebSocketState

    message = {
        'type': 'devices_changed',
        'devices': devices,
        'source': source,
        'timestamp': datetime.now().isoformat()
    }
    if disconnected:
        message['disconnected'] = disconnected
    if connected:
        message['connected'] = connected

    logger.info(f"[Broadcast] Notifying {len(runtime.global_state.websocket_connections)} clients about device change (source: {source})")

    with runtime.global_state.websocket_connections_lock:
        clients = list(runtime.global_state.websocket_connections.items())

    notification_level = ''
    notification_title = ''
    notification_message = ''
    if disconnected:
        notification_level = 'warning'
        notification_title = 'USB设备断开'
        notification_message = '断开：' + ', '.join(disconnected)
    elif connected:
        notification_level = 'success'
        notification_title = 'USB设备已连接'
        notification_message = '连接：' + ', '.join(connected)

    async def _send_device_change(client_id, ws):
        try:
            if ws.client_state == WebSocketState.CONNECTED:
                client_message = dict(message)
                if notification_title:
                    client_message['notification'] = runtime.store_notification(
                        client_id,
                        notification_title,
                        notification_message,
                        notification_level,
                        'device',
                        {
                            'connected': connected or [],
                            'disconnected': disconnected or [],
                            'source': source,
                        }
                    )
                await ws.send_json(client_message)
        except Exception as e:
            logger.debug(f"Failed to broadcast to {client_id}: {e}")

    await asyncio.gather(*[_send_device_change(cid, ws) for cid, ws in clients])


async def notify_device_change(devices_to_remove: list[str], context: str = "USB/IP Stop"):
    """通知设备变化到 WebSocket 客户端

    Args:
        devices_to_remove: 已断开的设备列表
        context: 日志上下文标识
    """
    if not devices_to_remove:
        return

    try:
        # 使用 asyncio.to_thread 避免阻塞事件循环
        current_devices = await asyncio.to_thread(device_manager.get_connected_devices)
        logger.info(f"[{context}] Notifying device change: {current_devices}")
        await broadcast_device_change(current_devices, disconnected=devices_to_remove, source='usbip_disconnect')
    except Exception as e:
        logger.warning(f"[{context}] Failed to notify device change: {e}")


def format_device_list_info(devices: list[str]) -> str:
    """格式化设备列表用于显示

    Args:
        devices: 设备列表

    Returns:
        格式化后的字符串，如 " (device1, device2)" 或空字符串
    """
    return f" ({', '.join(devices)})" if devices else ""


async def broadcast_device_lock_update(device_ids: list | None = None):
    """广播设备锁定更新（快速版本，不需要SSH查询）"""
    try:
        # 获取所有锁定的设备信息
        all_locks = device_lock_manager.get_all_locks()

        # 构建设备更新消息
        device_updates = []
        if device_ids:
            # 只更新指定的设备
            for device_id in device_ids:
                if device_id in all_locks:
                    lock_info = all_locks[device_id]
                    locked_by = lock_info['client_id']
                    device_updates.append({
                        'device_id': device_id,
                        'locked': True,
                        'locked_by': locked_by,
                        'locked_at': lock_info['timestamp']
                    })
                else:
                    device_updates.append({
                        'device_id': device_id,
                        'locked': False
                    })
        else:
            # 更新所有锁定的设备
            for device_id, lock_info in all_locks.items():
                locked_by = lock_info['client_id']
                device_updates.append({
                    'device_id': device_id,
                    'locked': True,
                    'locked_by': locked_by,
                    'locked_at': lock_info['timestamp']
                })

        # 广播到所有连接的客户端
        lock_msg = {'type': 'device_lock_update', 'devices': device_updates}

        async def _send_lock_update(cid, ws):
            with contextlib.suppress(Exception):
                await ws.send_json(lock_msg)

        await asyncio.gather(*[_send_lock_update(cid, ws) for cid, ws in runtime.global_state.websocket_connections.items()])

    except Exception as e:
        logger.error(f"[Broadcast Device Lock] 广播设备锁定更新失败: {e}")


# ==================== 用户状态管理 ====================

def get_or_create_user_state(client_id: str) -> dict:
    """获取或创建用户状态（不修正client_id，使用原始key）"""
    with runtime.global_state.user_states_lock:
        if client_id not in runtime.global_state.user_states:
            runtime.global_state.user_states[client_id] = {
                'running': False,
                'devices': [],
                'logs': [],
                'ssh_connected': False,
                'log_file': None,
                'test_type': 'cts',
                'created_at': datetime.now().isoformat(),
                'client_id': client_id,
                'last_seen': datetime.now().isoformat()
            }
        else:
            # 更新last_seen时间
            runtime.global_state.user_states[client_id]['last_seen'] = datetime.now().isoformat()
        return runtime.global_state.user_states[client_id]


def update_user_state_field(client_id: str, updates: dict):
    """更新用户状态的特定字段"""
    with runtime.global_state.user_states_lock:
        if client_id in runtime.global_state.user_states:
            runtime.global_state.user_states[client_id].update(updates)
            logger.info(f"[State] Updated {client_id}: {list(updates.keys())} = {updates}")
        else:
            logger.warning(f"[State] Client {client_id} not found in user_states")


# ==================== 并行设备操作 ====================

async def execute_on_devices_parallel(devices: list[str], operation_func, ssh, **kwargs) -> list[dict]:
    """
    并行执行设备操作，替代串行循环

    Args:
        devices: 设备ID列表
        operation_func: 单设备操作函数，签名为 async def func(device_id, ssh, **kwargs) -> dict
        ssh: SSH连接对象
        **kwargs: 传递给operation_func的额外参数

    Returns:
        操作结果列表
    """
    async def process_device(device_id: str) -> dict:
        try:
            result = await operation_func(device_id, ssh, **kwargs)
            result['device'] = device_id
            result['success'] = True
        except Exception as e:
            logger.error(f"Error processing device {device_id}: {e}")
            result = {'device': device_id, 'success': False, 'error': str(e)}
        return result

    # 并行执行所有设备操作
    tasks = [process_device(device_id) for device_id in devices]
    return await asyncio.gather(*tasks)


# ==================== 设备属性获取 ====================

async def get_device_properties_optimized(device_id: str, ssh) -> dict[str, str]:
    """获取设备属性 - 一次SSH调用获取所有属性"""
    cmd = f"""adb -s {device_id} shell "
    getprop ro.boot.verifiedbootstate;
    getprop | grep api_level;
    getprop sys.gmali.version;
    getprop persist.sys.timezone;
    getprop persist.sys.locale;
    cat /proc/meminfo | grep -E 'MemTotal|MemFree';
    cat vendor/etc/fstab.rk30board 2>/dev/null | grep userdata || echo 'N/A'
" """

    stdout, _stderr, _code = runtime.ssh_manager.execute_command(ssh, cmd, timeout=15)
    lines = stdout.strip().split('\n')

    properties = {}
    for line in lines:
        line = line.strip()
        if 'verifiedbootstate' in line or line in ['green', 'orange', 'yellow']:
            properties['boot_state'] = line
        elif 'api_level' in line:
            properties['api_level'] = line.split(':')[-1].strip() if ':' in line else line
        elif 'sys.gmali.version' in line:
            properties['mali_version'] = line.split(':')[-1].strip() if ':' in line else line
        elif 'MemTotal' in line:
            properties['mem_total'] = line.split()[-2] if len(line.split()) > 1 else line
        elif 'MemFree' in line:
            properties['mem_free'] = line.split()[-2] if len(line.split()) > 1 else line
        elif 'persist.sys.timezone' in line:
            properties['timezone'] = line.split(':')[-1].strip() if ':' in line else line
        elif 'persist.sys.locale' in line:
            properties['locale'] = line.split(':')[-1].strip() if ':' in line else line
        elif 'userdata' in line:
            properties['data_partition'] = line.split()[-1] if len(line.split()) > 0 else line

    return properties


# ==================== SSH 连接上下文管理器 ====================

class SSHConnection:
    """SSH连接上下文管理器，自动处理连接获取和归还"""

    def __init__(self, config=None):
        self.config = config or runtime.config_manager.load_config()
        self.ssh = None
        self._ssh_manager = runtime.ssh_manager

    def __enter__(self):
        self.ssh = self._ssh_manager.get_connection(self.config)
        if not self.ssh:
            raise HTTPException(
                status_code=500,
                detail="SSH连接失败"
            )
        return self.ssh

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.ssh:
            try:
                self._ssh_manager.return_connection(self.ssh)
            except Exception as e:
                logger.error(f"Failed to return SSH connection: {e}")


class DeviceSSHConnection:
    """设备SSH连接上下文管理器，自动处理连接获取和归还（连接池）"""

    def __init__(self, config=None):
        self.config = config or runtime.config_manager.load_config()
        self.ssh = None
        self._pool_key = None

    def _get_pool_key(self):
        """生成连接池的键值，基于设备主机地址"""
        device_host = self.config.get('device_host', '')
        if not device_host:
            return None

        if '@' in device_host:
            # 格式: username@hostname
            return device_host
        return device_host

    def __enter__(self):
        self._pool_key = self._get_pool_key()
        if not self._pool_key:
            raise HTTPException(
                status_code=500,
                detail="无效的设备主机配置"
            )

        # 从连接池获取或创建连接
        self.ssh = runtime.global_state.device_ssh_pool_get(self._pool_key, self.config)
        if not self.ssh:
            raise HTTPException(
                status_code=500,
                detail=f"无法连接到设备主机: {self._pool_key}"
            )
        return self.ssh

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.ssh and self._pool_key:
            try:
                runtime.global_state.device_ssh_pool_return(self._pool_key, self.ssh)
            except Exception as e:
                logger.error(f"Failed to return device SSH connection: {e}")


# ==================== 标准错误响应 ====================

def ssh_connection_failed_response():
    """SSH连接失败的标准错误响应"""
    return JSONResponse(
        content={'success': False, 'error': 'SSH connection failed'},
        status_code=500
    )
