"""
全局状态管理 - 集中管理应用运行时的全局状态

提供 GlobalState 类，管理 SSH 连接池、设备缓存、WebSocket 连接、
上传进度、USB/IP 状态、终端会话、用户状态、通知等运行时数据
"""

import logging
import queue
import re
import threading
import time
from collections import OrderedDict
from datetime import datetime, timedelta

import paramiko
from starlette.websockets import WebSocketState

from foundation.common_utils import CommonUtils
from foundation.config import (
    APK_TASK_MAX_AGE_SECONDS,
    DEVICE_SSH_POOLS_MAX,
    FIRMWARE_UPLOAD_PROGRESS_MAX_ITEMS_PER_CLIENT,
    UPLOAD_PROGRESS_MAX_AGE_SECONDS,
    USBIP_STATE_MAX_AGE_SECONDS,
    USER_STATE_MAX_AGE_HOURS,
)
from features.devices.usbip import split_host_port

logger = logging.getLogger(__name__)


class GlobalState:
    """全局状态管理"""

    def __init__(self):
        self.running_tests = {}  # {client_id: test_info}
        self.test_logs = {}      # {client_id: log_entries}
        self.ssh_connections = {}  # {client_id: ssh_connection}
        self.scrcpy_sessions = {}  # {device_id: session_info}
        self.device_cache = {'devices': [], 'timestamp': 0}  # 3秒TTL
        self.device_cache_lock = threading.Lock()  # 设备缓存锁
        self.websocket_connections = {}  # {client_id: websocket}
        self.websocket_connections_lock = threading.Lock()  # WebSocket连接锁
        self.firmware_upload_progress = {}  # {client_id: {'progress': float, 'filename': str, 'uploaded_size': int, 'total_size': int, 'timestamp': float}}
        self.firmware_upload_progress_lock = threading.Lock()  # 上传进度锁
        self.usbip_states = {}  # {client_id: {'connected': bool, 'timestamp': float}}
        self.suite_download_tasks = {}  # {task_id: {'status': str, 'progress': float, ...}}
        self.suite_download_tasks_lock = threading.Lock()
        self.suite_extract_tasks = {}  # {task_id: {'status': str, 'progress': float, ...}}
        self.suite_extract_tasks_lock = threading.Lock()
        self.usbip_devices_source = {}  # {device_id: {'source': device_host, 'timestamp': float}}
        self.terminal_ssh_sessions = {}  # {session_id: {'ssh': ssh, 'channel': channel, 'websocket': websocket}}
        self.terminal_lock = threading.Lock()  # 终端会话锁
        self.user_states = {}  # {client_id: {running, devices, logs, created_at, last_seen}}
        self.user_states_lock = threading.Lock()  # 用户状态锁
        self.usbip_states_lock = threading.Lock()  # USB/IP状态锁
        self.usbip_devices_source_lock = threading.Lock()  # USB/IP设备来源锁
        self.test_logs_lock = threading.Lock()  # 测试日志锁
        self.last_saved_log_file = {}  # {client_id: log_file_path}
        self.notifications = {}  # {client_id: deque([notification])}
        self.notifications_lock = threading.Lock()
        self.device_ssh_pools = {}
        self.device_ssh_pools_lock = threading.Lock()
        self.device_ssh_pools_max = DEVICE_SSH_POOLS_MAX
        self.apk_analysis_tasks = {}  # {task_id: {'status': str, 'progress': int, 'apk_path': str, 'output_dir': str, 'filename': str, 'timestamp': float, 'error': str or None}}
        self.apk_analysis_tasks_lock = threading.Lock()
        self.apk_upload_locks = {}
        self.apk_upload_locks_lock = threading.Lock()
        self.background_tasks = set()

    def _close_ssh_safely(self, ssh):
        """安全关闭SSH连接"""
        try:
            if ssh and ssh.get_transport() is not None:
                ssh.close()
        except Exception:
            pass

    @staticmethod
    def _cleanup_expired(data_dict, lock, max_age_seconds):
        """通用过期清理辅助方法"""
        now_ts = time.time()
        with lock:
            expired = [k for k, v in data_dict.items()
                       if now_ts - v.get('timestamp', 0) > max_age_seconds]
            for k in expired:
                del data_dict[k]
        return expired

    def cleanup_old_user_states(self):
        """清理超过指定时间的旧用户状态，防止内存泄漏"""
        try:
            to_remove = []
            now = datetime.now()

            # 收集需要清理的client_id（在锁内快速遍历）
            with self.user_states_lock:
                for client_id, state in self.user_states.items():
                    if 'last_seen' in state:
                        try:
                            last_seen = datetime.fromisoformat(state['last_seen'])
                            if (now - last_seen) > timedelta(hours=USER_STATE_MAX_AGE_HOURS):
                                to_remove.append(client_id)
                        except (ValueError, TypeError):
                            to_remove.append(client_id)

                # 删除用户状态
                for client_id in to_remove:
                    del self.user_states[client_id]

            # 清理相关的测试日志（在user_states_lock外执行，避免嵌套锁）
            if to_remove:
                with self.test_logs_lock:
                    for client_id in to_remove:
                        self.test_logs.pop(client_id, None)
                        # 同时清理last_saved_log_file中的旧条目
                        self.last_saved_log_file.pop(client_id, None)

            # 以下清理逻辑独立于用户状态清理，每次都执行

            # 清理上传进度（过期超过10分钟的）
            expired_progress = self._cleanup_expired(
                self.firmware_upload_progress, self.firmware_upload_progress_lock, UPLOAD_PROGRESS_MAX_AGE_SECONDS)

            # 清理断开的终端SSH会话
            with self.terminal_lock:
                expired_sessions = []
                for sid, session in list(self.terminal_ssh_sessions.items()):
                    ws = session.get('websocket')
                    if ws is None or (hasattr(ws, 'client_state') and ws.client_state == WebSocketState.DISCONNECTED):
                        expired_sessions.append(sid)
                for sid in expired_sessions:
                    session = self.terminal_ssh_sessions.pop(sid)
                    self._close_ssh_safely(session.get('ssh'))
                    channel = session.get('channel')
                    if channel:
                        try:
                            channel.close()
                        except Exception:
                            pass

            # 清理USB/IP过期状态（超过24小时的）
            expired_usbip = self._cleanup_expired(
                self.usbip_states, self.usbip_states_lock, USBIP_STATE_MAX_AGE_SECONDS)

            expired_apk_tasks = self._cleanup_expired(
                self.apk_analysis_tasks, self.apk_analysis_tasks_lock, APK_TASK_MAX_AGE_SECONDS)

            with self.firmware_upload_progress_lock:
                for client_id in list(self.firmware_upload_progress.keys()):
                    entries = self.firmware_upload_progress[client_id]
                    if isinstance(entries, list) and len(entries) > FIRMWARE_UPLOAD_PROGRESS_MAX_ITEMS_PER_CLIENT:
                        self.firmware_upload_progress[client_id] = entries[-FIRMWARE_UPLOAD_PROGRESS_MAX_ITEMS_PER_CLIENT:]

            if to_remove or expired_progress or expired_sessions or expired_usbip or expired_apk_tasks:
                logger.info(f"Cleanup: {len(to_remove)} user states, {len(expired_progress)} upload progress, "
                           f"{len(expired_sessions)} SSH sessions, {len(expired_usbip)} USB/IP states, {len(expired_apk_tasks)} APK tasks")
        except Exception as e:
            logger.error(f"Error cleaning up user states: {e}")

    def device_ssh_pool_get(self, pool_key: str, config: dict, pool_size: int = 3):
        """
        从设备SSH连接池获取或创建连接

        使用FIFO策略清理最老的连接池,防止内存泄漏
        """
        with self.device_ssh_pools_lock:
            # 限制连接池数量,防止内存泄漏
            if pool_key not in self.device_ssh_pools:
                if len(self.device_ssh_pools) >= self.device_ssh_pools_max:
                    # 清理最老的连接池
                    oldest_key = next(iter(self.device_ssh_pools))
                    old_pool = self.device_ssh_pools.pop(oldest_key)
                    while not old_pool.empty():
                        ssh = old_pool.get_nowait()
                        self._close_ssh_safely(ssh)
                    logger.info(f"[Device SSH Pool] Cleaned oldest pool: {oldest_key}")

                self.device_ssh_pools[pool_key] = queue.Queue(maxsize=pool_size)

            pool = self.device_ssh_pools[pool_key]

        # 尝试从池中获取有效连接（最多尝试pool_size次）
        max_attempts = pool.maxsize
        for attempt in range(max_attempts):
            try:
                ssh = pool.get_nowait()
                # 健康检查
                try:
                    transport = ssh.get_transport() if ssh else None
                    if transport and transport.is_active():
                        logger.debug(f"[Device SSH Pool] Reused connection for {pool_key}")
                        return ssh
                    else:
                        logger.debug(f"[Device SSH Pool] Connection {attempt+1}/{max_attempts} is inactive")
                        self._close_ssh_safely(ssh)
                except Exception as e:
                    logger.debug(f"[Device SSH Pool] Connection {attempt+1}/{max_attempts} check failed: {e}")
                    self._close_ssh_safely(ssh)
            except queue.Empty:
                break

        # 池为空或所有连接都失效，创建新连接
        logger.debug(f"[Device SSH Pool] Creating new connection for {pool_key}")
        return self._create_device_ssh_connection(pool_key, config)

    def device_ssh_pool_return(self, pool_key: str, ssh):
        """
        归还连接到设备SSH连接池

        Args:
            pool_key: 连接池键值
            ssh: SSHClient 对象
        """
        with self.device_ssh_pools_lock:
            if pool_key in self.device_ssh_pools:
                try:
                    self.device_ssh_pools[pool_key].put_nowait(ssh)
                except queue.Full:
                    # 池已满，关闭连接
                    self._close_ssh_safely(ssh)
            else:
                # 池不存在，关闭连接
                self._close_ssh_safely(ssh)

    def _create_device_ssh_connection(self, pool_key: str, config: dict):
        """
        创建设备SSH连接

        Args:
            pool_key: 连接池键值（通常是 device_host）
            config: 配置字典

        Returns:
            SSHClient 对象，失败返回 None
        """
        device_host = config.get('device_host', pool_key)
        if not device_host:
            logger.error("[Device SSH Pool] No device host in config")
            return None

        if '@' not in device_host:
            logger.error(f"[Device SSH Pool] Device host format should be user@host: {device_host}")
            return None

        username, hostname = CommonUtils.parse_host_address(device_host)
        hostname, port = split_host_port(hostname)
        password = config.get('device_pswd', '')

        if not password:
            logger.error(f"[Device SSH Pool] No SSH password configured for {pool_key}")
            return None

        try:
            ssh = paramiko.SSHClient()
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            ssh.connect(hostname=hostname, port=port, username=username, password=password, timeout=10)
            logger.info(f"[Device SSH Pool] Connected to {pool_key}")
            return ssh
        except Exception as e:
            logger.error(f"[Device SSH Pool] Failed to connect to {pool_key}: {e}")
            return None


global_state = GlobalState()

# Redmine 问题ID缓存
REDMINE_ISSUE_ID_CACHE = OrderedDict()

# 预编译正则（供 core.network 等模块使用）
_IP_PATTERN = re.compile(r'^(\d{1,3}\.){3}\d{1,3}$')
_PING_RTT_PATTERN = re.compile(r'rtt min/avg/max/mdev = [\d.]+/([\d.]+)/[\d.]+/[\d.]+ ms')
_PING_AVG_PATTERN = re.compile(r'avg[=\s]+([\d.]+)', re.IGNORECASE)
_PING_LOSS_PATTERN = re.compile(r'(\d+)% packet loss')
