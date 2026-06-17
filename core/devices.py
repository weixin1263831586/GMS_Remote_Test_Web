"""
设备管理 - 核心业务逻辑
"""
import asyncio
import logging
import re
import subprocess
import time
from datetime import datetime
from typing import Any, Dict, List

from fastapi import HTTPException
from fastapi.responses import JSONResponse

from .ssh import ssh_manager
from .config import config_manager
from .device_utils import DeviceUtils
from .common_utils import CommonUtils
from .notifications import store_notification
from .notifications import safe_websocket_send  # noqa: F401  (re-exported for routers)
from .state import global_state
from modules.device_lock_manager import device_lock_manager

logger = logging.getLogger(__name__)


class DeviceManager:
    """
    设备管理器

    特性：
    - 设备列表查询
    - 设备信息获取
    - 设备锁定管理
    - 设备操作（重启、remount等）
    """

    def __init__(self):
        """初始化设备管理器"""
        self.ssh_manager = ssh_manager
        self.config_manager = config_manager

    def get_connected_devices(
        self,
        force_refresh: bool = False,
        ssh=None
    ) -> List[str]:
        """
        获取已连接的Android设备列表

        Args:
            force_refresh: 是否强制刷新
            ssh: SSH连接（如果不提供则创建新连接）

        Returns:
            设备ID列表
        """
        config = self.config_manager.load_config()

        if ssh is None and CommonUtils.is_local_host(config.get('ubuntu_host', '')):
            try:
                result = subprocess.run(
                    ['adb', 'devices'],
                    capture_output=True,
                    text=True,
                    timeout=10
                )
                if result.returncode != 0:
                    logger.warning(f"[Device] Local adb devices failed: {result.stderr.strip()}")
                    return []
                return DeviceUtils.parse_adb_devices(result.stdout)
            except FileNotFoundError:
                logger.warning("[Device] adb command not found on local host")
                return []
            except Exception as e:
                logger.error(f"[Device] Error getting local devices: {e}")
                return []

        if ssh is None:
            ssh = self.ssh_manager.get_connection(config)
            if not ssh:
                logger.error("[Device] Failed to get SSH connection")
                return []
            created_ssh = True
        else:
            created_ssh = False

        try:
            output, error, code = self.ssh_manager.execute_command(
                ssh,
                "adb devices",
                timeout=10
            )

            # 使用 DeviceUtils 解析设备列表
            return DeviceUtils.parse_adb_devices(output)

        except Exception as e:
            logger.error(f"[Device] Error getting devices: {e}")
            return []
        finally:
            if created_ssh and ssh:
                self.ssh_manager.return_connection(ssh)

    def get_device_info(
        self,
        device_id: str,
        ssh=None
    ) -> Dict[str, Any]:
        """
        获取设备详细信息

        Args:
            device_id: 设备ID
            ssh: SSH连接

        Returns:
            设备信息字典
        """
        config = self.config_manager.load_config()

        if ssh is None:
            ssh = self.ssh_manager.get_connection(config)
            if not ssh:
                return {}
            created_ssh = True
        else:
            created_ssh = False

        try:
            info = {}

            # 属性名到输出键名的映射
            prop_map = {
                'ro.serialno': 'serial_no',
                'ro.product.model': 'model',
                'ro.build.version.release': 'android_version',
                'ro.build.type': 'build_type',
                'ro.build.tags': 'build_tags',
                'ro.build.date': 'build_date',
                'ro.build.version.sdk': 'sdk_version',
                'ro.build.version.security_patch': 'security_patch',
                'ro.build.fingerprint': 'fingerprint',
            }

            # 使用 getprop 不带参数输出所有属性（key=value 格式），按 key 解析更健壮
            getprop_cmd = f"adb -s {device_id} shell getprop"

            try:
                output, _, _ = self.ssh_manager.execute_command(
                    ssh,
                    getprop_cmd,
                    timeout=15
                )
                # 解析 getprop 输出: [ro.serialno]: [value]
                for line in output.strip().split('\n'):
                    m = re.match(r'\[([\w.]+)\]:\s*\[(.*)\]', line)
                    if m:
                        prop_name, value = m.group(1), m.group(2)
                        if prop_name in prop_map:
                            info[prop_map[prop_name]] = value or "未知"
                # 补充未获取到的属性
                for key in prop_map.values():
                    if key not in info:
                        info[key] = "未知"
            except Exception:
                for key in prop_map.values():
                    info[key] = "未知"

            return info

        except Exception as e:
            logger.error(f"[Device] Error getting device info: {e}")
            return {}
        finally:
            if created_ssh and ssh:
                self.ssh_manager.return_connection(ssh)

    def reboot_device(
        self,
        device_id: str,
        ssh=None,
        wait_for_online: bool = True,
    ) -> Dict[str, Any]:
        """
        重启设备

        Args:
            device_id: 设备ID
            ssh: SSH连接

        Returns:
            结果字典
        """
        config = self.config_manager.load_config()

        if ssh is None:
            ssh = self.ssh_manager.get_connection(config)
            if not ssh:
                return {'success': False, 'error': 'SSH连接失败'}
            created_ssh = True
        else:
            created_ssh = False

        try:
            # 执行重启
            output, error, code = self.ssh_manager.execute_command(
                ssh,
                f"adb -s {device_id} reboot",
                timeout=30
            )

            if code != 0:
                return {
                    'success': False,
                    'error': error or '重启命令执行失败'
                }

            if not wait_for_online:
                return {
                    'success': True,
                    'back_online': False,
                    'wait_time': 0.0,
                    'message': '重启命令已发送，设备恢复由后台监控确认'
                }

            # 等待设备重新上线（最多60秒）
            start_time = time.time()
            while time.time() - start_time < 60:
                check_output, _, _ = self.ssh_manager.execute_command(
                    ssh,
                    f"adb -s {device_id} get-state",
                    timeout=10
                )
                if 'device' in check_output.lower():
                    wait_time = time.time() - start_time
                    return {
                        'success': True,
                        'back_online': True,
                        'wait_time': round(wait_time, 1)
                    }
                time.sleep(2)

            return {
                'success': True,
                'back_online': False,
                'wait_time': 60.0
            }

        except Exception as e:
            logger.error(f"[Device] Error rebooting device: {e}")
            return {'success': False, 'error': str(e)}
        finally:
            if created_ssh and ssh:
                self.ssh_manager.return_connection(ssh)

    def remount_device(
        self,
        device_id: str,
        ssh=None
    ) -> Dict[str, Any]:
        """
        Remount设备（root权限）

        Args:
            device_id: 设备ID
            ssh: SSH连接

        Returns:
            结果字典
        """
        config = self.config_manager.load_config()

        if ssh is None:
            ssh = self.ssh_manager.get_connection(config)
            if not ssh:
                return {'success': False, 'error': 'SSH连接失败'}
            created_ssh = True
        else:
            created_ssh = False

        try:
            # 执行 adb root
            output, error, code = self.ssh_manager.execute_command(
                ssh,
                f"adb -s {device_id} root",
                timeout=15
            )
            time.sleep(2)

            # 执行 remount
            remount_output, error, code = self.ssh_manager.execute_command(
                ssh,
                f"adb -s {device_id} remount",
                timeout=15
            )

            # 检查 veritymode
            verity_output, _, _ = self.ssh_manager.execute_command(
                ssh,
                f"adb -s {device_id} shell getprop ro.boot.veritymode",
                timeout=10
            )
            verity_mode = verity_output.strip()

            # 判断是否需要重启 - 基于实际的 remount 输出
            # 关键指示：如果输出包含 "Now reboot your device" 则需要重启
            # 如果输出包含 "Overlayfs enabled" 或 "Remount succeeded" (无重启提示) 则已完成
            needs_reboot = 'Now reboot your device' in remount_output
            overlayfs_enabled = 'Overlayfs enabled' in remount_output or 'overlayfs' in remount_output.lower()

            # 如果启用了 overlayfs，说明已经完成 remount，不需要重启
            if overlayfs_enabled:
                needs_reboot = False
                verity_mode = 'disabled'  # 逻辑上设置为 disabled

            result = {
                'success': code == 0,
                'verity_mode': verity_mode,
                'needs_reboot': needs_reboot,
                'overlayfs_enabled': overlayfs_enabled,
                'output': remount_output[-500:] if remount_output else error
            }

            if needs_reboot:
                result['warning'] = '设备需要重启才能使 remount 生效'
            elif overlayfs_enabled:
                result['info'] = '设备已启用 overlayfs，处于读写模式'

            return result

        except Exception as e:
            logger.error(f"[Device] Error remounting device: {e}")
            return {'success': False, 'error': str(e)}
        finally:
            if created_ssh and ssh:
                self.ssh_manager.return_connection(ssh)


# 全局设备管理器实例
device_manager = DeviceManager()


# ==================== Device route/service helpers ====================



# ==================== 设备锁管理 ====================

async def release_device_locks(client_id: str, device_ids: List[str], broadcast: bool = True):
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

async def broadcast_device_change(devices: List[str], disconnected: List[str] = None, connected: List[str] = None, source: str = 'usb_monitor'):
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

    logger.info(f"[Broadcast] Notifying {len(global_state.websocket_connections)} clients about device change (source: {source})")

    with global_state.websocket_connections_lock:
        clients = list(global_state.websocket_connections.items())

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
                    client_message['notification'] = store_notification(
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


async def notify_device_change(devices_to_remove: List[str], context: str = "USB/IP Stop"):
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


def format_device_list_info(devices: List[str]) -> str:
    """格式化设备列表用于显示

    Args:
        devices: 设备列表

    Returns:
        格式化后的字符串，如 " (device1, device2)" 或空字符串
    """
    return f" ({', '.join(devices)})" if devices else ""


async def broadcast_device_lock_update(device_ids: list = None):
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
            try:
                await ws.send_json(lock_msg)
            except Exception:
                pass

        await asyncio.gather(*[_send_lock_update(cid, ws) for cid, ws in global_state.websocket_connections.items()])

    except Exception as e:
        logger.error(f"[Broadcast Device Lock] 广播设备锁定更新失败: {e}")


# ==================== 用户状态管理 ====================

def get_or_create_user_state(client_id: str) -> dict:
    """获取或创建用户状态（不修正client_id，使用原始key）"""
    with global_state.user_states_lock:
        if client_id not in global_state.user_states:
            global_state.user_states[client_id] = {
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
            global_state.user_states[client_id]['last_seen'] = datetime.now().isoformat()
        return global_state.user_states[client_id]


def update_user_state_field(client_id: str, updates: dict):
    """更新用户状态的特定字段"""
    with global_state.user_states_lock:
        if client_id in global_state.user_states:
            global_state.user_states[client_id].update(updates)
            logger.info(f"[State] Updated {client_id}: {list(updates.keys())} = {updates}")
        else:
            logger.warning(f"[State] Client {client_id} not found in user_states")


# ==================== 并行设备操作 ====================

async def execute_on_devices_parallel(devices: List[str], operation_func, ssh, **kwargs) -> List[Dict]:
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
    async def process_device(device_id: str) -> Dict:
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

async def get_device_properties_optimized(device_id: str, ssh) -> Dict[str, str]:
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

    stdout, stderr, code = ssh_manager.execute_command(ssh, cmd, timeout=15)
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
        self.config = config or config_manager.load_config()
        self.ssh = None
        self._ssh_manager = ssh_manager

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
        self.config = config or config_manager.load_config()
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
        self.ssh = global_state.device_ssh_pool_get(self._pool_key, self.config)
        if not self.ssh:
            raise HTTPException(
                status_code=500,
                detail=f"无法连接到设备主机: {self._pool_key}"
            )
        return self.ssh

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.ssh and self._pool_key:
            try:
                global_state.device_ssh_pool_return(self._pool_key, self.ssh)
            except Exception as e:
                logger.error(f"Failed to return device SSH connection: {e}")


# ==================== 标准错误响应 ====================

def ssh_connection_failed_response():
    """SSH连接失败的标准错误响应"""
    return JSONResponse(
        content={'success': False, 'error': 'SSH connection failed'},
        status_code=500
    )
