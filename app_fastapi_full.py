#!/usr/bin/env python3
"""
GMS Auto Test - 完整版FastAPI应用（端口5001）

完全替代Flask版本，实现所有60个端点
"""

import os
import sys
import logging
import subprocess
import configparser
import socket
import uuid
import paramiko
import threading
import time
import re
import json
import shlex
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional, Union
from contextlib import asynccontextmanager
from collections import deque
import asyncio

from fastapi import FastAPI, HTTPException, Depends, WebSocket, WebSocketDisconnect, UploadFile, File, Form, Request, Body, Query
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from starlette.websockets import WebSocketState
import json
from enum import Enum

# ==================== 枚举定义 ====================

class VerifiedBootState(str, Enum):
    """设备启动验证状态"""
    LOCKED = 'green'
    UNLOCKED_ORANGE = 'orange'
    UNLOCKED_YELLOW = 'yellow'

    @property
    def is_locked(self) -> bool:
        """返回是否已锁定"""
        return self == self.LOCKED

    @property
    def display_text(self) -> str:
        """返回显示文本"""
        return {
            'green': '已锁定 (GREEN)',
            'orange': '未锁定 (ORANGE)',
            'yellow': '未锁定 (YELLOW)',
        }[self.value]

class LogLevel(str, Enum):
    """日志级别"""
    INFO = 'info'
    WARNING = 'warning'
    ERROR = 'error'
    SUCCESS = 'success'

# ==================== 常量定义 ====================

# TRADEFED二进制文件映射
TRADEFED_BINARY_MAP = {
    'cts': 'cts-tradefed',
    'gsi': 'cts-tradefed',
    'gts': 'gts-tradefed',
    'sts': 'sts-tradefed',
    'vts': 'vts-tradefed',
    'xts': 'xts-tradefed'
}

# ==================== Lifespan 事件处理 ====================

@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    # 启动时执行
    logger.info("=" * 60)
    logger.info("Application startup")
    logger.info("=" * 60)

    # 启动定期清理任务
    async def periodic_cleanup_with_retry():
        """定期清理旧用户状态和测试日志（带错误恢复）"""
        while True:
            try:
                await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)
                global_state.cleanup_old_user_states()
                logger.info("Periodic cleanup completed")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Cleanup failed, retrying in 5 minutes: {e}")
                await asyncio.sleep(300)

    cleanup_task = asyncio.create_task(periodic_cleanup_with_retry())

    # 初始化USB监控
    try:
        # 创建一个队列用于跨线程通信
        import queue
        if not hasattr(app.state, 'usb_event_queue'):
            app.state.usb_event_queue = queue.Queue()

        def get_devices():
            """获取当前设备列表"""
            try:
                return device_manager.get_connected_devices()
            except Exception as e:
                logger.error(f"Error getting devices for USB monitor: {e}")
                return []

        def on_usb_devices_changed(devices):
            """USB设备变化回调"""
            logger.info(f"USB devices changed: {devices}")

            # 将事件放入队列，让后台任务处理
            try:
                app.state.usb_event_queue.put({
                    'type': 'devices_changed',
                    'devices': devices,
                    'timestamp': datetime.now().isoformat()
                })
                logger.info(f"USB device change event queued, current devices: {devices}")
            except Exception as e:
                logger.error(f"Error queuing device change event: {e}")

        # 初始化并启动USB监控
        init_usb_monitor(
            device_getter=get_devices,
            on_devices_changed=on_usb_devices_changed,
            check_interval=2.0,
            use_udev=True
        )
        start_usb_monitor()
        logger.info("USB monitor started successfully")
    except Exception as e:
        logger.error(f"Failed to start USB monitor: {e}")

    yield

    # 关闭时执行
    logger.info("Application shutdown")

    # 取消清理任务
    cleanup_task.cancel()
    try:
        await cleanup_task
    except asyncio.CancelledError:
        pass

    # 停止USB监控
    try:
        stop_usb_monitor()
        logger.info("USB monitor stopped")
    except Exception as e:
        logger.error(f"Error stopping USB monitor: {e}")

    logger.info("=" * 60)

from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
import uvicorn

# 添加项目根目录到Python路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 导入核心模块
from core.config import config_manager
from core.ssh import ssh_manager, SSHD_INSTALL_GUIDE
from core.device import device_manager
from core.test_runner import test_runner
from core.test_report import test_report_manager
from core.vnc import vnc_manager, calculate_window_positions
from core.adb_forward import adb_forward_manager
from core.usbip import usbip_manager
from core.claude_report_analyzer import ClaudeReportAnalyzer
from core.common_utils import CommonUtils
from report_analyzer import ReportAnalyzer
from test_report_db import test_report_db

# 导入管理模块
from modules.client_manager import client_manager
from modules.device_lock_manager import device_lock_manager
from modules.test_logs_manager import test_logs_manager

# 导入USB监控模块
from core.usb_monitor import init_usb_monitor, start_usb_monitor, stop_usb_monitor

# 日志配置
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s'
)
logger = logging.getLogger(__name__)

# ==================== FastAPI应用 ====================

# 自定义JSONResponse类，确保UTF-8编码
class UTF8JSONResponse(JSONResponse):
    def render(self, content) -> bytes:
        return json.dumps(
            content,
            ensure_ascii=False,
            allow_nan=False,
            indent=None,
            separators=(",", ":"),
        ).encode("utf-8")

app = FastAPI(
    title="GMS Auto Test - FastAPI Server (Port 5001)",
    description="完整的测试管理服务（替代Flask版本）",
    version="4.0.0",
    lifespan=lifespan,
    default_response_class=UTF8JSONResponse
)

# CORS中间件
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 挂载静态文件
static_dir = os.path.join(os.path.dirname(__file__), 'static')
if os.path.exists(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

# 配置Jinja2模板
templates_dir = os.path.join(os.path.dirname(__file__), 'templates')
templates = Jinja2Templates(directory=templates_dir)

# 自定义url_for函数，兼容Flask语法
def url_for_static(filename: str) -> str:
    """生成静态文件URL（兼容Flask语法）"""
    return f"/static/{filename}"

# 将自定义函数添加到模板全局变量中
templates.env.globals['url_for'] = lambda endpoint, filename='': (
    url_for_static(filename) if endpoint == 'static' else f'/{endpoint}'
)

# ==================== 常量定义 ====================
MAX_LOG_ENTRIES = 1000  # 最大日志条目数
CLEANUP_INTERVAL_SECONDS = 3600  # 定期清理间隔（1小时）
USER_STATE_MAX_AGE_HOURS = 24  # 用户状态最大存活时间

# ==================== 全局状态管理 ====================

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
        self.usbip_states = {}  # {client_id: {'connected': bool, 'timestamp': float}}
        self.usbip_devices_source = {}  # {device_id: {'source': device_host, 'timestamp': float}}
        self.terminal_ssh_sessions = {}  # {session_id: {'ssh': ssh, 'channel': channel, 'websocket': websocket}}
        self.terminal_lock = threading.Lock()  # 终端会话锁
        self.user_states = {}  # {client_id: {running, devices, logs, created_at, last_seen}}
        self.user_states_lock = threading.Lock()  # 用户状态锁
        self.usbip_states_lock = threading.Lock()  # USB/IP状态锁（与Flask一致）
        self.usbip_devices_source_lock = threading.Lock()  # USB/IP设备来源锁（与Flask一致）
        self.test_logs_lock = threading.Lock()  # 测试日志锁

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

                logger.info(f"Cleaned up {len(to_remove)} old user states (age > {USER_STATE_MAX_AGE_HOURS}h)")
        except Exception as e:
            logger.error(f"Error cleaning up user states: {e}")

global_state = GlobalState()

DEVICE_CACHE_TTL = 3

# ==================== 通用工具函数 ====================

# Pre-compiled regex patterns for efficiency
_ANSI_ESCAPE_PATTERN = re.compile(r'\x1B\[[0-?]*[ -/]*[@-~]')
_IP_PATTERN = re.compile(r'^(\d{1,3}\.){3}\d{1,3}$')
_PING_RTT_PATTERN = re.compile(r'rtt min/avg/max/mdev = [\d.]+/([\d.]+)/[\d.]+/[\d.]+ ms')
_PING_AVG_PATTERN = re.compile(r'avg[=\s]+([\d.]+)', re.IGNORECASE)
_PING_LOSS_PATTERN = re.compile(r'(\d+)% packet loss')

def strip_ansi_codes(text: str) -> str:
    """
    移除ANSI转义码

    Args:
        text: 包含ANSI转义码的文本

    Returns:
        清理后的文本
    """
    return _ANSI_ESCAPE_PATTERN.sub('', text)

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

# ==================== SSH 连接上下文管理器 ====================

class SSHConnection:
    """SSH连接上下文管理器，自动处理连接获取和归还"""

    def __init__(self, config=None):
        self.config = config or config_manager.load_config()
        self.ssh = None

    def __enter__(self):
        self.ssh = ssh_manager.get_connection(self.config)
        if not self.ssh:
            raise HTTPException(
                status_code=500,
                detail="SSH连接失败"
            )
        return self.ssh

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.ssh:
            try:
                ssh_manager.return_connection(self.ssh)
            except Exception as e:
                logger.error(f"Failed to return SSH connection: {e}")

# ==================== WebSocket 辅助函数 ====================

async def safe_websocket_send(client_id: str, message: dict):
    """线程安全地发送WebSocket消息（带背压检查）"""
    # 先获取WebSocket连接（使用锁）
    with global_state.websocket_connections_lock:
        ws = global_state.websocket_connections.get(client_id)

    # 在锁外执行异步操作
    if ws:
        try:
            # 检查WebSocket状态
            if ws.client_state == WebSocketState.DISCONNECTED:
                logger.debug(f"WebSocket {client_id} already disconnected")
                return

            # 检查缓冲区大小（防止内存泄漏）
            if hasattr(ws, '_queue') and ws._queue.qsize() > 100:
                logger.warning(f"WebSocket buffer full for {client_id}, dropping message")
                return

            await ws.send_json(message)
        except (WebSocketDisconnect, ConnectionError):
            logger.debug(f"WebSocket {client_id} disconnected during send")
        except Exception as e:
            logger.debug(f"Failed to send WebSocket message to {client_id}: {e}")

# ==================== 统一响应格式工具类 ====================

class ApiResponse:
    """统一的API响应格式（与Flask完全兼容）"""

    @staticmethod
    def success(data=None, message="操作成功"):
        """成功响应"""
        response = {'success': True}
        if data is not None:
            response['data'] = data
        if message:
            response['message'] = message
        return JSONResponse(
            content=response,
            headers={"Content-Type": "application/json; charset=utf-8"}
        )

    @staticmethod
    def error(error_message, status_code=500, **extra_fields):
        """错误响应（与Flask格式一致）"""
        response = {'success': False, 'error': error_message}
        response.update(extra_fields)
        return JSONResponse(
            content=response,
            status_code=status_code,
            headers={"Content-Type": "application/json; charset=utf-8"}
        )

    @staticmethod
    def device_results(results, operation_name):
        """设备批量操作结果"""
        success_count = sum(1 for r in results if r.get('success', False))
        fail_count = len(results) - success_count
        return ApiResponse.success({
            'results': results,
            'summary': {'total': len(results), 'success': success_count, 'failed': fail_count}
        }, f"{operation_name}完成: 成功 {success_count} 台, 失败 {fail_count} 台")

# ==================== 优化工具函数 ====================

from functools import wraps, lru_cache
import asyncio

def async_subprocess_run(cmd, **kwargs):
    """异步执行subprocess.run，避免阻塞事件循环"""
    return asyncio.to_thread(subprocess.run, cmd, **kwargs)

def handle_api_errors(func):
    """统一API错误处理装饰器"""
    @wraps(func)
    async def wrapper(*args, **kwargs):
        try:
            return await func(*args, **kwargs)
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error in {func.__name__}: {e}")
            return ApiResponse.error(str(e), status_code=500)
    return wrapper

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

async def get_device_properties_optimized(device_id: str, ssh) -> Dict[str, str]:
    """
    优化版设备属性获取 - 一次SSH调用获取所有属性

    原版本需要6次SSH调用，优化后只需1次
    """
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

@lru_cache(maxsize=128)
def cached_xml_analysis(xml_path: str, mtime: float) -> Dict:
    """带缓存的XML分析结果"""
    from core.test_report import ReportAnalyzer
    return ReportAnalyzer().analyze_file(xml_path)

def get_config_cached() -> Dict:
    """带缓存的配置加载"""
    return config_manager.load_config()

# ==================== 用户状态管理辅助函数 ====================

def save_test_report_to_db(
    client_id: str,
    config: Dict[str, Any],
    test_params: Dict[str, Any],
    user_logs: List[str]
) -> Optional[str]:
    """
    从测试日志中提取 RESULT DIRECTORY 并记录测试报告到数据库（与Flask版本一致）

    Args:
        client_id: 客户端ID
        config: 配置字典
        test_params: 测试参数
        user_logs: 用户日志列表

    Returns:
        报告时间戳，如果失败则返回 None
    """
    try:
        # 从日志中提取 RESULT DIRECTORY
        result_dir = None
        for log in reversed(user_logs):
            log_str = str(log)
            if 'RESULT DIRECTORY' in log_str:
                # 提取 RESULT DIRECTORY 后面的路径
                match = re.search(r'RESULT DIRECTORY\s*:\s*(/[^\s]+)', log_str)
                if match:
                    result_dir = match.group(1).strip()
                    logger.info(f"[ReportDB] 找到 RESULT DIRECTORY: {result_dir}")
                    break

        if not result_dir or not os.path.exists(result_dir):
            logger.warning(f"[ReportDB] 未找到 RESULT DIRECTORY 或目录不存在: {result_dir}")
            return None

        # 提取时间戳
        timestamp = os.path.basename(result_dir)

        # 检查是否已记录
        existing = test_report_db.get_report_by_timestamp(timestamp)
        if existing:
            logger.info(f"[ReportDB] 报告已存在: {timestamp}")
            return timestamp

        # 解析 test_result.xml
        xml_path = os.path.join(result_dir, 'test_result.xml')
        report_info = {
            'timestamp': timestamp,
            'test_type': test_params.get('test_type', 'UNKNOWN').upper(),
            'client_id': client_id,
            'devices': test_params.get('devices', []),
            'result_dir': result_dir,
            'suite_path': test_params.get('test_suite', ''),
            'status': 'completed'
        }

        # 提取用户名
        if '@' in client_id:
            report_info['user'] = client_id.split('@')[0]

        # 解析XML获取测试结果统计（使用缓存）
        if os.path.exists(xml_path):
            try:
                stat = os.stat(xml_path)
                result = cached_xml_analysis(xml_path, stat.st_mtime)
                if result:
                    report_info.update({
                        'pass': result['summary']['pass'],
                        'fail': result['summary']['fail'],
                        'total': result['summary']['total'],
                        'pass_rate': result['summary']['pass_rate'],
                        'device': result['details']['device'],
                        'start_time': result['details']['start_time']
                    })
            except Exception as e:
                logger.warning(f"[ReportDB] 解析 XML 失败: {e}")

        # 添加到数据库（使用add_report与Flask版本一致）
        if test_report_db.add_report(report_info):
            logger.info(f"[ReportDB] 报告已记录: {timestamp}")
            return timestamp

        return None

    except Exception as e:
        logger.error(f"[ERROR] 保存报告到数据库失败: {e}")
        return None

def get_client_id_from_request(request: Request) -> str:
    """从请求中获取client_id（优先从配置文件读取用户名）"""
    client_ip = get_client_ip(request)

    # 优先从配置文件读取client_hosts映射
    config = config_manager.load_config()
    client_hosts = config.get('client_hosts', {})

    # 如果client_hosts中有该IP的映射，使用映射的用户名
    if client_ip in client_hosts:
        username = client_hosts[client_ip]
    else:
        # 其次从请求头获取用户名
        username = request.headers.get('X-Client-Username')
        if not username or username == 'unknown':
            # 尝试动态检测用户名（通过已保存的 SSH 凭据）
            success, detected_username, _ = client_manager.detect_username(client_ip)
            if success and detected_username:
                username = detected_username
            else:
                # 无法识别用户，使用 unknown
                username = 'unknown'

    return client_manager.get_client_id(client_ip, username)

async def broadcast_device_lock_update(device_ids: list = None):
    """广播设备锁定更新（快速版本，不需要SSH查询）"""
    try:
        # 获取所有锁定的设备信息
        all_locks = device_lock_manager.get_all_locks()

        # 构建设备更新消息
        device_updates = []
        if device_ids:
            # 只更新指定的设备
            logger.debug(f"[Broadcast Device Lock] 更新指定设备: {device_ids}")
            for device_id in device_ids:
                if device_id in all_locks:
                    lock_info = all_locks[device_id]
                    # 直接使用client_id (username@ip格式)
                    locked_by = lock_info['client_id']
                    device_updates.append({
                        'device_id': device_id,
                        'locked': True,
                        'locked_by': locked_by,
                        'locked_at': lock_info['timestamp']
                    })
                    logger.debug(f"[Broadcast Device Lock] 设备 {device_id} 已锁定 by {locked_by}")
                else:
                    device_updates.append({
                        'device_id': device_id,
                        'locked': False
                    })
                    logger.debug(f"[Broadcast Device Lock] 设备 {device_id} 已解锁")
        else:
            # 更新所有锁定的设备
            logger.debug(f"[Broadcast Device Lock] 更新所有锁定设备")
            for device_id, lock_info in all_locks.items():
                # 直接使用client_id (username@ip格式)
                locked_by = lock_info['client_id']
                device_updates.append({
                    'device_id': device_id,
                    'locked': True,
                    'locked_by': locked_by,
                    'locked_at': lock_info['timestamp']
                })

        # 广播到所有连接的客户端
        logger.debug(f"[Broadcast Device Lock] 广播到 {len(global_state.websocket_connections)} 个客户端")
        for client_id, ws in global_state.websocket_connections.items():
            try:
                await ws.send_json({
                    'type': 'device_lock_update',
                    'devices': device_updates
                })
                logger.debug(f"[Broadcast Device Lock] 成功发送到客户端 {client_id}")
            except Exception as e:
                logger.warning(f"[Broadcast Device Lock] 发送到客户端 {client_id} 失败: {e}")

        logger.debug(f"[Broadcast Device Lock] 已发送设备锁定更新到 {len(global_state.websocket_connections)} 个客户端")
    except Exception as e:
        logger.error(f"[Broadcast Device Lock] 广播设备锁定更新失败: {e}")

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

# ==================== Pydantic数据模型 ====================

class ClientInfoRequest(BaseModel):
    """客户端信息请求"""
    username: Optional[str] = None
    password: Optional[str] = None
    ip: Optional[str] = None

class DeviceLockRequest(BaseModel):
    """设备锁定请求（支持单设备和批量操作）"""
    device_id: Optional[str] = None  # 单设备ID（旧格式）
    devices: Optional[List[str]] = None  # 设备ID列表（新格式，支持批量）
    action: str = 'lock'  # lock, unlock

class TestStartRequest(BaseModel):
    """测试启动请求"""
    test_type: str = "cts"
    test_module: str = ""
    test_case: str = ""
    retry_dir: str = ""
    test_suite: str = ""
    local_server: str = ""
    devices: List[str] = []
    client_id: str = "test_client"

class DeviceActionRequest(BaseModel):
    """设备操作请求"""
    devices: List[str] = Field(..., description="设备ID列表")

class WifiConnectRequest(DeviceActionRequest):
    """WiFi连接请求"""
    ssid: str = "AndroidWifi"
    password: str = "1234567890"

class VNCStartRequest(BaseModel):
    """VNC启动请求"""
    host: Optional[str] = None
    password: Optional[str] = None
    vnc_password: Optional[str] = None

class ADBForwardStartRequest(BaseModel):
    """ADB转发启动请求"""
    device_host: str
    device_password: Optional[str] = Field(default="", description="设备主机SSH密码")

class USBIPStartRequest(BaseModel):
    """USB/IP启动请求"""
    device_host: Optional[str] = None
    device_password: Optional[str] = Field(default="", description="设备主机SSH密码")

class AutocompleteSuiteRequest(BaseModel):
    """自动完成测试套件请求"""
    test_type: str
    base_path: str

class VPNConnectRequest(BaseModel):
    """VPN连接请求（所有字段可选，兼容前端无参数调用）"""
    host: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None

class FirmwareBurnRequest(BaseModel):
    """固件烧录请求 - 与Flask版本一致"""
    devices: List[str]
    system_img: str
    vendor_img: Optional[str] = ""
    misc_img: Optional[str] = ""

class GSIBurnRequest(BaseModel):
    """GSI烧录请求 - 与Flask版本一致"""
    devices: List[str]
    system_img: str
    vendor_img: Optional[str] = ""
    script_path: Optional[str] = ""

class SNBurnRequest(BaseModel):
    """SN烧录请求 - 与Flask版本一致"""
    devices: List[str]
    sn_code: str

class ScreenStartRequest(BaseModel):
    """屏幕录制启动请求"""
    device_id: str
    duration: int = 60

# ==================== 基础端点 ====================

@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    """主页 - 使用FastAPI专用模板（移除Socket.IO依赖）"""
    config = config_manager.load_config()

    # 使用FastAPI专用模板（移除了Socket.IO）
    response = templates.TemplateResponse(
        "index_fastapi.html",
        {
            "request": request,
            "config": config
        }
    )
    # 添加缓存控制头，防止浏览器缓存旧代码
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response

@app.get("/api/system/health")
async def health_check():
    """健康检查"""
    return JSONResponse(content={
        "status": "ok",
        "service": "GMS Auto Test - FastAPI Server (Port 5001)",
        "framework": "FastAPI",
        "version": "4.0.0",
        "timestamp": datetime.now().isoformat(),
        "websocket_connections": len(global_state.websocket_connections),
        "modules": {
            "config_manager": "✓",
            "device_manager": "✓",
            "test_runner": "✓",
            "test_report_manager": "✓",
            "vnc_manager": "✓",
            "adb_forward_manager": "✓",
            "usbip_manager": "✓",
            "client_manager": "✓",
            "device_lock_manager": "✓",
            "test_logs_manager": "✓"
        }
    })

# ==================== 客户端管理 ====================



@app.get("/api/config/validate")
async def validate_config():
    """验证配置文件"""
    try:
        config = config_manager.load_config()

        errors = []
        warnings = []

        # 检查必要字段
        required_fields = ['ubuntu_host', 'ubuntu_user', 'ubuntu_pswd', 'suites_path', 'script_path']
        for field in required_fields:
            if field not in config or not config[field]:
                errors.append(f"缺少必要字段: {field}")

        # 检查ubuntu_host
        ubuntu_host = config.get('ubuntu_host', '')
        if ubuntu_host in ['test', 'localhost', '127.0.0.1']:
            warnings.append(f"ubuntu_host '{ubuntu_host}' 可能无法从远程访问")

        # 检查路径是否存在
        for path_field in ['suites_path', 'script_path']:
            path = config.get(path_field, '')
            if path and not os.path.exists(path):
                warnings.append(f"路径不存在: {path_field} = {path}")

        # 检查SSH凭据
        if not config.get('ubuntu_pswd'):
            warnings.append("未设置ubuntu_pswd，可能影响SSH连接")

        return JSONResponse(content={
            "success": len(errors) == 0,
            "valid": len(errors) == 0,
            "errors": errors,
            "warnings": warnings
        })
    except Exception as e:
        logger.error(f"Error validating config: {e}")
        raise HTTPException(
                status_code=500,
                detail=f"{str(e)}. 请检查配置和参数是否正确。"
            )


@app.get("/api/config/values")
async def get_config_values():
    """获取配置值供前端使用（不包含敏感信息）"""
    config = config_manager.load_config()

    # 只返回前端需要的配置项
    safe_config = {
        'script_path': config.get('script_path', ''),
        'suites_path': config.get('suites_path', ''),
        'ubuntu_user': config.get('ubuntu_user', ''),
        'ubuntu_host': config.get('ubuntu_host', 'localhost'),
        'local_server': config.get('local_server', '')
        # 不返回密码
    }

    return JSONResponse(content={"success": True, "data": safe_config})

@app.get("/api/users/current")
async def get_client_info(request: Request):
    """获取客户端信息（返回client_id用于WebSocket连接）"""
    # 使用统一的client_id获取逻辑（优先从client_hosts读取）
    client_id = get_client_id_from_request(request)

    # 确保用户状态存在
    get_or_create_user_state(client_id)

    # 解析client_id获取IP和用户名
    parts = client_id.split('@')
    username = parts[0] if len(parts) > 0 else 'unknown'
    client_ip = parts[1] if len(parts) > 1 else 'unknown'

    logger.info(f"[ClientInfo] GET - IP: {client_ip} | Username: {username} | ClientID: {client_id}")

    return JSONResponse(content={
        "ip": client_ip,
        "client_id": client_id,
        "username": username
    })


def get_client_ip(request: Request, fallback_ip: Optional[str] = None) -> str:
    """提取客户端真实IP地址（支持代理）"""
    if fallback_ip:
        return fallback_ip
    return (
        request.headers.get('X-Forwarded-For', '').split(',')[0].strip() or
        request.headers.get('X-Real-IP') or
        request.client.host if request.client else 'unknown'
    )

@app.post("/api/users/detect")
async def detect_client(req: ClientInfoRequest, request: Request):
    """自动检测客户端用户名"""
    client_ip = get_client_ip(request, req.ip)

    success, username, error = client_manager.detect_username(
        client_ip,
        req.username,
        req.password
    )

    if success:
        return JSONResponse(content={
            "success": True,
            "username": username
        })
    else:
        return JSONResponse(content={
            "success": False,
            "error": error
        }, status_code=401)

@app.post("/api/users/set-username")
async def set_client_username(req: ClientInfoRequest, request: Request):
    """手动设置客户端用户名（不需要SSH密码）"""
    client_ip = get_client_ip(request, req.ip)
    username = req.username

    if not username or username == 'unknown':
        return JSONResponse(content={
            "success": False,
            "error": "用户名不能为空或unknown"
        }, status_code=400)

    # 加载配置
    config = config_manager.load_config()
    client_hosts = config.get('client_hosts', {})

    # 保存映射
    client_hosts[client_ip] = username
    config['client_hosts'] = client_hosts

    # 保存到配置文件
    if config_manager.save_dynamic_config(config):
        # 更新内存中的映射
        client_manager.client_hosts = client_hosts

        logger.info(f"[Set Username] {client_ip} -> {username}")

        return JSONResponse(content={
            "success": True,
            "username": username,
            "ip": client_ip,
            "client_id": f"{username}@{client_ip}"
        })
    else:
        return JSONResponse(content={
            "success": False,
            "error": "保存配置失败"
        }, status_code=500)

@app.get("/api/client-info")
async def handle_client_info_get(request: Request):
    """获取客户端IP（兼容Flask路由）"""
    client_ip = get_client_ip(request)
    return JSONResponse(content={'ip': client_ip})

@app.post("/api/client-info")
async def handle_client_info_post(req: ClientInfoRequest, request: Request):
    """记录客户端信息（兼容Flask路由）"""
    client_ip = get_client_ip(request, req.ip)
    username = req.username

    # 如果前端未提供用户名或为'unknown'，尝试动态检测
    if not username or username == 'unknown':
        success, detected_username, _ = client_manager.detect_username(client_ip)
        if success and detected_username:
            username = detected_username
        else:
            username = 'unknown'

    # 更新用户状态
    client_id = client_manager.get_client_id(client_ip, username)
    get_or_create_user_state(client_id)
    update_user_state_field(client_id, {
        'client_ip': client_ip,
        'client_username': username,
        'last_seen': datetime.now().isoformat()
    })

    logger.info(f"[ClientInfo] IP: {client_ip} | Username: {username} | ClientID: {client_id}")

    return ApiResponse.success({'client_id': client_id})

@app.post("/api/client-info/detect")
async def detect_client_info(req: ClientInfoRequest, request: Request):
    """自动检测客户端用户名（兼容Flask路由）"""
    return await detect_client(req, request)

@app.get("/api/users/list")
async def list_users():
    """获取所有在线用户列表"""
    users = []
    now = datetime.now()

    # 本地地址列表，不显示在用户列表中
    local_addresses = {'127.0.0.1', 'localhost', '::1', '0.0.0.0'}

    # VPN网关地址列表（不显示在用户列表中）
    vpn_gateway_addresses = {'10.10.10.1'}

    with global_state.user_states_lock:
        # 收集所有用户
        temp_users = {}
        for client_id, state in global_state.user_states.items():
            # 检查会话是否活跃（最近24小时内有活动）
            if 'last_seen' in state:
                try:
                    last_seen = datetime.fromisoformat(state['last_seen'])
                    if (now - last_seen) > timedelta(hours=24):
                        continue
                except (ValueError, TypeError):
                    continue

            # 解析client_id (username@ip)
            parts = client_id.split('@')
            username_from_id = parts[0] if len(parts) > 0 else 'unknown'
            ip = parts[1] if len(parts) > 1 else 'unknown'

            # 优先使用state中存储的username（更准确）
            username = state.get('client_username', username_from_id)
            if username == 'unknown':
                username = username_from_id

            # 过滤本地地址和VPN网关地址
            if ip in local_addresses or ip in vpn_gateway_addresses:
                continue

            # 过滤unknown用户（用户名识别失败的情况）
            if username == 'unknown':
                continue

            # 如果同一个IP有多个用户记录，优先保留非unknown的用户
            if ip in temp_users:
                existing_user = temp_users[ip]
                if existing_user['username'] == 'unknown' and username != 'unknown':
                    # 用真实用户替换unknown用户
                    temp_users[ip] = {
                        'client_id': client_id,
                        'username': username,
                        'ip': ip,
                        'running': state.get('running', False),
                        'devices': state.get('devices', []),
                        'last_seen': state.get('last_seen', ''),
                        'created_at': state.get('created_at', '')
                    }
                # 否则保留第一个
            else:
                temp_users[ip] = {
                    'client_id': client_id,
                    'username': username,
                    'ip': ip,
                    'running': state.get('running', False),
                    'devices': state.get('devices', []),
                    'last_seen': state.get('last_seen', ''),
                    'created_at': state.get('created_at', '')
                }

        users = list(temp_users.values())

    return JSONResponse(content={
        'total': len(users),
        'users': users
    })

# ==================== 配置管理 ====================

@app.get("/api/config/read")
async def get_config(request: Request):
    """获取配置 - 与Flask版本一致，直接返回配置对象"""
    # 跟踪用户访问
    client_id = get_client_id_from_request(request)
    get_or_create_user_state(client_id)

    config = config_manager.load_config()
    # 直接返回配置对象，与Flask版本一致
    return JSONResponse(content=config)

@app.post("/api/config/update")
async def update_config(req: dict):
    """更新配置 - 只修改动态配置，禁止修改config.json"""
    existing_dynamic = config_manager._load_dynamic_config() or {}

    # 动态配置字段（保存在 config_dynamic.json）
    # 所有可修改的配置项都在这里
    # 注意：client_ip 和 client_username 是运行时状态，不应保存到配置文件
    dynamic_keys = {
        'device_host', 'device_pswd',
        'client_hosts', 'client_ssh_credentials',
        'ubuntu_user', 'ubuntu_host', 'ubuntu_pswd',
        'local_server', 'suites_path', 'usbip_vid_pid'
    }

    # 更新动态配置
    dynamic_updates = existing_dynamic.copy()

    for key, value in req.items():
        if key in dynamic_keys:
            # 空字符串不覆盖现有值（用于密码字段）
            if value != '' or key not in existing_dynamic:
                dynamic_updates[key] = value
        # 忽略不在dynamic_keys中的字段（不允许修改config.json）

    # 只保存动态配置，不修改config.json
    if config_manager.save_dynamic_config(dynamic_updates):
        return JSONResponse(content={'success': True})
    else:
        raise HTTPException(status_code=500, detail="保存配置失败")

# 向后兼容别名
@app.get("/api/config")
async def get_config_legacy(request: Request):
    """获取配置（向后兼容别名）"""
    return await get_config(request)

@app.post("/api/config")
async def update_config_legacy(req: dict):
    """更新配置（向后兼容别名）"""
    return await update_config(req)

# ==================== 设备管理 ====================

@app.get("/api/devices/list")
async def get_connected_devices(
    request: Request,
    help: bool = Query(False)
):
    """获取所有已连接的设备列表（与adb devices相同）"""
    # 检查是否需要显示帮助
    if help:
        help_text = generate_per_api_help_text("GET", "/api/devices/list")
        if help_text:
            return PlainTextResponse(
                content=help_text,
                headers={
                    "Content-Type": "text/plain; charset=utf-8",
                    "Cache-Control": "public, max-age=300"
                }
            )

    # 获取设备列表 - 与Flask一致，直接返回数组
    try:
        # 跟踪用户访问
        client_id = get_client_id_from_request(request)
        get_or_create_user_state(client_id)

        config = config_manager.load_config()

        # 先刷新设备列表（需要最新状态来清理记录）
        devices = device_manager.get_connected_devices()

        # 清理已不存在的设备来源记录（与Flask版本一致）
        # 如果设备已不在当前设备列表中，说明设备已断开/移除，应该清除其来源记录
        current_device_set = set(devices)
        devices_to_remove = [
            dev_id for dev_id in global_state.usbip_devices_source.keys()
            if dev_id not in current_device_set
        ]
        if devices_to_remove:
            logger.info(f"[Devices API] Cleaning up removed devices: {devices_to_remove}")
            with global_state.usbip_devices_source_lock:
                for dev_id in devices_to_remove:
                    del global_state.usbip_devices_source[dev_id]

        # 检查缓存（清理后检查）
        now = datetime.now().timestamp()
        if now - global_state.device_cache['timestamp'] < DEVICE_CACHE_TTL:
            cached_devices = global_state.device_cache['devices']
            return JSONResponse(content=cached_devices)

        devices_with_status = []

        for device_id in devices:
            device_info = {
                'device_id': device_id,
                'status': 'online',
                'locked': False
            }

            # 检查锁定状态
            client_ip = get_client_ip(request)
            client_id = client_manager.get_client_id(client_ip)
            lock_status = device_lock_manager.get_lock_status(device_id)

            if lock_status:
                device_info['locked'] = True
                device_info['locked_by'] = lock_status['locked_by']
                device_info['locked_by_self'] = lock_status.get('client_id') == client_id
                device_info['locked_at'] = lock_status['locked_at']
            else:
                device_info['locked_by'] = ''
                device_info['locked_by_self'] = False

            # 检查USB/IP来源
            if device_id in global_state.usbip_devices_source:
                source = global_state.usbip_devices_source[device_id]
                device_info['source'] = source['source']
                device_info['is_usbip'] = True

            devices_with_status.append(device_info)

        # 更新缓存（使用专用锁确保原子性）
        with global_state.device_cache_lock:
            global_state.device_cache = {
                'devices': devices_with_status,
                'timestamp': now
            }

        # 直接返回数组（与Flask一致）
        return JSONResponse(content=devices_with_status)
    except Exception as e:
        logger.error(f"Error listing devices: {e}")
        raise HTTPException(
                status_code=500,
                detail=f"{str(e)}. 请检查配置和参数是否正确。"
            )

@app.post("/api/devices/bootloader-lock")
async def lock_bootloader(
    request: Request,
    help: bool = Query(False),
    req: DeviceLockRequest = Body(None)
):
    """锁定设备Bootloader（使用run_Device_Lock.sh脚本）"""
    # 检查是否需要显示帮助
    if help:
        help_text = generate_per_api_help_text("POST", "/api/devices/bootloader-lock")
        if help_text:
            return PlainTextResponse(
                content=help_text,
                headers={
                    "Content-Type": "text/plain; charset=utf-8",
                    "Cache-Control": "public, max-age=300"
                }
            )

    try:
        # 兼容两种请求格式：单设备（device_id）和批量（devices）
        devices = req.devices if req.devices else []
        if req.device_id:
            devices = [req.device_id]

        if not devices:
            return ApiResponse.error("未选择设备", status_code=400)

        # 固定为锁定操作
        action = "lock"
        config = config_manager.load_config()

        with ssh_manager.connection(config) as ssh:
            results = []

            # 本地脚本路径 - 使用tools目录
            local_script = os.path.join(os.path.dirname(__file__), 'tools', 'run_Device_Lock.sh')
            # 远程脚本路径
            remote_script = f"/home/{config['ubuntu_user']}/GMS-Suite/run_Device_Lock.sh"

            # 检查本地脚本是否存在
            if not os.path.exists(local_script):
                return ApiResponse.error(f'脚本文件不存在: {local_script}', status_code=404)

            # 上传脚本到远程服务器
            try:
                with ssh.open_sftp() as sftp:
                    sftp.put(local_script, remote_script)
                # 设置执行权限
                ssh_manager.execute_command(ssh, f"chmod +x '{remote_script}'")
            except Exception as e:
                return ApiResponse.error(f'上传脚本失败: {str(e)}', status_code=500)

            # 对每个设备执行锁定/解锁操作
            for device_id in devices:
                try:
                    # 执行脚本
                    cmd = f"bash '{remote_script}' '{device_id}' '{action}'"
                    output, error, code = ssh_manager.execute_command(ssh, cmd)

                    # 等待设备重新上线
                    if code == 0:
                        start_time = time.time()
                        while time.time() - start_time < 60:  # 等待最多60秒
                            check_cmd = f"adb -s {device_id} get-state"
                            check_output, _, check_code = ssh_manager.execute_command(ssh, check_cmd)
                            if 'device' in check_output.lower():
                                break
                    await asyncio.sleep(2)

                    results.append({
                        'device': device_id,
                        'success': code == 0,
                        'output': output[-200:] if output else error
                    })
                except Exception as e:
                    results.append({
                        'device': device_id,
                        'success': False,
                        'error': str(e)
                    })

            return ApiResponse.success({'results': results}, '设备锁定操作完成')

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error managing device lock: {e}")
        return ApiResponse.error(str(e), status_code=500)

@app.post("/api/devices/bootloader-unlock")
async def unlock_bootloader(
    request: Request,
    help: bool = Query(False),
    req: DeviceLockRequest = Body(None)
):
    """解锁设备Bootloader（使用run_Device_Lock.sh脚本）"""
    # 强制设置action为unlock
    if req:
        req.action = 'unlock'
    else:
        req = DeviceLockRequest(devices=[], action='unlock')

    # 调用lock_bootliner函数
    return await lock_bootliner(request, None, False, req)

@app.post("/api/devices/bootloader-status")
async def check_bootloader_status(req: DeviceActionRequest):
    """检查设备Bootloader锁状态（GREEN=锁定, ORANGE=未锁定）"""
    try:
        with SSHConnection() as ssh:
            # 并行检查所有设备的锁定状态
            async def check_single_device(device_id: str) -> Dict:
                # Check verified boot state (GREEN = locked, ORANGE = unlocked)
                output, error, code = ssh_manager.execute_command(
                    ssh,
                    f"adb -s {device_id} shell getprop ro.boot.verifiedbootstate"
                )
                state = output.strip()

                # 根据状态判断是否锁定（使用枚举）
                try:
                    boot_state = VerifiedBootState(state)
                    is_locked = boot_state.is_locked
                    status_text = boot_state.display_text
                except ValueError:
                    is_locked = False
                    status_text = f'未知状态 ({state})'

                return {
                    'device': device_id,
                    'locked': is_locked,
                    'state': state,
                    'status': status_text
                }

            results = await asyncio.gather(*[check_single_device(d) for d in req.devices])

            return ApiResponse.success({'results': results}, '锁定状态检查完成')

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error checking lock status: {e}")
        return ApiResponse.error(str(e), status_code=500)

@app.post("/api/devices/info")
async def get_device_info(req: DeviceActionRequest):
    """获取设备详细信息 - 优化版，并行获取所有设备信息"""
    try:
        with SSHConnection() as ssh:
            # 并行获取所有设备信息
            async def get_single_device_info(device_id: str) -> Dict:
                device_info = {'device': device_id, 'properties': {}}

                # 使用device_manager获取基本信息
                base_info = device_manager.get_device_info(device_id, ssh)

                # 添加base_info中的字段
                field_mapping = {
                    'serial_no': '设备序列号',
                    'model': '设备型号',
                    'android_version': 'Android版本',
                    'build_type': '编译类型',
                    'build_tags': '编译标签',
                    'build_date': '编译时间',
                    'sdk_version': 'SDK版本',
                    'security_patch': '安全补丁',
                    'fingerprint': '指纹'
                }

                for key, label in field_mapping.items():
                    if key in base_info:
                        device_info['properties'][label] = base_info[key]

                # 优化：一次SSH调用获取所有额外属性
                extra_props = await get_device_properties_optimized(device_id, ssh)

                # 映射属性到中文标签
                prop_mapping = {
                    'boot_state': ('启动状态', lambda x: x if x else '未知'),
                    'api_level': ('API级别', lambda x: x.split('[')[-1].replace(']', '') if '[' in x else (x or '未知')),
                    'mali_version': ('Mali库版本', lambda x: x or '未知'),
                    'mem_total': ('总内存', lambda x: f"{x} KB" if x else '未知'),
                    'mem_free': ('可用内存', lambda x: f"{x} KB" if x else '未知'),
                    'timezone': ('时区', lambda x: x or '未知'),
                    'locale': ('语言', lambda x: x or '未知'),
                    'data_partition': ('DATA分区', lambda x: x.split()[-1] if x and 'userdata' in x else '未知')
                }

                for key, (label, formatter) in prop_mapping.items():
                    if key in extra_props:
                        device_info['properties'][label] = formatter(extra_props[key])

                return device_info

            # 并行执行所有设备的信息获取
            results = await asyncio.gather(*[get_single_device_info(d) for d in req.devices])

            return ApiResponse.success({'results': results}, '设备信息获取完成')

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting device info: {e}")
        return ApiResponse.error(str(e), status_code=500)

@app.get("/api/devices/management")
async def devices_management():
    """设备管理页面（获取所有设备的详细管理信息）"""
    try:
        config = config_manager.load_config()

        # 从持久化文件加载USB/IP设备来源
        import json
        try:
            with open(config_manager.dynamic_config_path, 'r') as f:
                dynamic_config = json.load(f)
                persisted_usbip_sources = dynamic_config.get('usbip_devices_source', {})
        except (FileNotFoundError, json.JSONDecodeError, KeyError):
            persisted_usbip_sources = {}

        with SSHConnection(config) as ssh:
            # 获取基本设备列表
            output, _, _ = ssh_manager.execute_command(ssh, "adb devices", timeout=5)
            device_ids = [
                line.split('\t')[0]
                for line in output.split('\n')[1:]
                if line.strip() and '\tdevice' in line
            ]

            if not device_ids:
                return JSONResponse(content={'devices': []})

            # 获取设备锁定状态
            client_ip = '127.0.0.1'  # 本地调用
            client_id = client_manager.get_client_id(client_ip)
            locks = device_lock_manager.get_all_locks()

            # 批量获取设备属性（包括电池电量）
            device_props_cmd = " && ".join([
                f"adb -s {device_id} shell 'echo \"===DEVICE:{device_id}===\" && getprop ro.serialno && getprop ro.product.model && getprop ro.build.version.release && dumpsys battery | grep level | cut -d: -f2 | tr -d \" \"'"
                for device_id in device_ids
            ])

            props_output, _, _ = ssh_manager.execute_command(ssh, device_props_cmd, timeout=15)

            # 解析批量输出
            device_data = {}
            current_device = None

            for line in props_output.split('\n'):
                line = line.strip()
                if line.startswith('===DEVICE:'):
                    current_device = line.split('===DEVICE:')[1].split('===')[0]
                    device_data[current_device] = {'serial_no': '', 'model': '', 'android_version': '', 'battery_level': ''}
                elif current_device and line:
                    if not device_data[current_device]['serial_no']:
                        device_data[current_device]['serial_no'] = line
                    elif not device_data[current_device]['model']:
                        device_data[current_device]['model'] = line
                    elif not device_data[current_device]['android_version']:
                        device_data[current_device]['android_version'] = line
                    elif not device_data[current_device]['battery_level']:
                        device_data[current_device]['battery_level'] = line

            # 构建响应（与Flask版本保持一致）
            devices_info = []
            ubuntu_host = config.get("ubuntu_host", "")
            ubuntu_user = config.get("ubuntu_user", "")

            # 合并所有USB/IP设备来源字典（包括持久化的）
            all_usbip_sources = {**global_state.usbip_devices_source, **usbip_manager.device_sources, **persisted_usbip_sources}

            # 清理已不存在的设备来源记录（与Flask版本一致）
            current_device_set = set(device_ids)
            devices_to_remove = [dev_id for dev_id in all_usbip_sources if dev_id not in current_device_set]

            if devices_to_remove:
                logger.info(f"[Device Management] Cleaning up removed devices: {devices_to_remove}")
                # 从全局状态中清除
                with global_state.usbip_devices_source_lock:
                    for dev_id in devices_to_remove:
                        global_state.usbip_devices_source.pop(dev_id, None)
                # 从usbip_manager中清除
                for dev_id in devices_to_remove:
                    usbip_manager.device_sources.pop(dev_id, None)

            for device_id in device_ids:
                props = device_data.get(device_id, {})
                lock_info = locks.get(device_id, {})

                # 判断设备来源类型（与Flask版本一致）
                if device_id in all_usbip_sources:
                    source_type = 'usbip'
                    source_host = all_usbip_sources.get(device_id, {}).get('source', 'Unknown')
                else:
                    source_type = 'local'
                    source_host = f'{ubuntu_user}@{ubuntu_host}'

                device_info = {
                    'device_id': device_id,
                    'serial_no': props.get('serial_no', device_id),
                    'model': props.get('model', ''),
                    'android_version': props.get('android_version', ''),
                    'battery_level': props.get('battery_level', ''),
                    'source_type': source_type,
                    'source_host': source_host,
                    'status': 'online',
                    'locked_by': lock_info.get('client_id', '') if device_id in locks else '',
                    'locked_by_self': lock_info.get('client_id') == client_id if device_id in locks else False
                }
                devices_info.append(device_info)

            return JSONResponse(content={'devices': devices_info})

    except Exception as e:
        logger.error(f"Error getting devices management: {e}")
        return JSONResponse(
            content={"success": False, "error": str(e)},
            status_code=500
        )

@app.get("/api/devices/user-locked")
async def list_user_locks():
    """列出所有用户锁定设备（多用户环境下的设备占用状态）"""
    return JSONResponse(content={
        "success": True,
        "data": device_lock_manager.get_all_locks()
    })

@app.post("/api/devices/reboot")
async def reboot_devices(req: DeviceActionRequest):
    """重启设备 - 优化版，并行重启"""
    try:
        with SSHConnection() as ssh:
            # 并行重启所有设备
            async def reboot_single_device(device_id: str) -> Dict:
                result = device_manager.reboot_device(device_id, ssh)
                result['device'] = device_id
                return result

            results = await asyncio.gather(*[reboot_single_device(d) for d in req.devices])
            return ApiResponse.device_results(results, "设备重启")

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error rebooting devices: {e}")
        return ApiResponse.error(str(e), status_code=500)

@app.post("/api/devices/remount")
async def remount_devices(req: DeviceActionRequest, request: Request):
    """Remount设备 - 优化版，并行remount"""
    try:
        # 获取client_id
        client_id = get_client_id_from_request(request)

        with SSHConnection() as ssh:
            # 并行remount所有设备
            async def remount_single_device(device_id: str) -> Dict:
                # 执行 adb root
                output, error, code = ssh_manager.execute_command(
                    ssh,
                    f"adb -s {device_id} root",
                    timeout=15
                )

                # 发送输出到前端
                await safe_websocket_send(client_id, {
                    'type': 'log_update',
                    'log': f"[{device_id}] adb root: {output.strip()}",
                    'log_type': 'info'
                })

                await asyncio.sleep(2)

                # 执行 remount
                output, error, code = ssh_manager.execute_command(
                    ssh,
                    f"adb -s {device_id} remount",
                    timeout=15
                )

                # 发送输出到前端
                await safe_websocket_send(client_id, {
                    'type': 'log_update',
                    'log': f"[{device_id}] adb remount: {output.strip()}",
                    'log_type': 'info'
                })

                # 使用device_manager的remount方法获取完整结果
                result = device_manager.remount_device(device_id, ssh)
                result['device'] = device_id
                return result

            results = await asyncio.gather(*[remount_single_device(d) for d in req.devices])
            return ApiResponse.device_results(results, "设备Remount")

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error remounting devices: {e}")
        return ApiResponse.error(str(e), status_code=500)

@app.post("/api/devices/connect-wifi")
async def connect_wifi(req: WifiConnectRequest):
    """连接WiFi"""
    try:
        config = config_manager.load_config()
        ssh = ssh_manager.get_connection(config)
        if not ssh:
            raise HTTPException(status_code=500, detail="SSH连接失败")

        results = []
        for device_id in req.devices:
            enable_cmd = f"adb -s {device_id} shell cmd wifi set-wifi-enabled enabled"
            connect_cmd = f'adb -s {device_id} shell cmd wifi connect-network "{req.ssid}" wpa2 "{req.password}"'
            full_cmd = f"{enable_cmd} && sleep 2 && {connect_cmd}"

            output, error, code = ssh_manager.execute_command(ssh, full_cmd)
            results.append({'device': device_id, 'success': code == 0})

        ssh_manager.return_connection(ssh)

        success_count = sum(1 for r in results if r.get('success', False))
        return JSONResponse(content={
            "success": True,
            "results": results,
            "summary": {
                "total": len(results),
                "success": success_count,
                "failed": len(results) - success_count
            }
        })
    except Exception as e:
        logger.error(f"Error connecting WiFi: {e}")
        raise HTTPException(
                status_code=500,
                detail=f"{str(e)}. 请检查配置和参数是否正确。"
            )

class DeviceShellRequest(BaseModel):
    """设备Shell请求"""
    serial_no: str = Field(..., description="设备序列号")

@app.post("/api/devices/shell")
async def open_device_shell(req: DeviceShellRequest, request: Request):
    """打开设备ADB Shell - 为终端页面准备设备连接"""
    try:
        config = config_manager.load_config()
        ssh = ssh_manager.get_connection(config)
        if not ssh:
            return JSONResponse(
                content={"success": False, "message": "SSH连接失败"},
                status_code=500
            )

        # 验证设备是否在线
        check_cmd = f"adb -s {req.serial_no} shell echo 'ready'"
        output, error, code = ssh_manager.execute_command(ssh, check_cmd)

        ssh_manager.return_connection(ssh)

        if code == 0 and 'ready' in output:
            # 将设备信息保存到会话中,供WebSocket终端使用
            client_id = get_client_id_from_request(request)

            # 保存到全局状态
            if not hasattr(global_state, 'device_shells'):
                global_state.device_shells = {}

            global_state.device_shells[client_id] = {
                'serial_no': req.serial_no,
                'connected_at': datetime.now().isoformat()
            }

            return JSONResponse(content={
                "success": True,
                "message": f"设备 {req.serial_no} 已准备就绪",
                "serial_no": req.serial_no
            })
        else:
            return JSONResponse(
                content={"success": False, "message": f"设备 {req.serial_no} 不在线或无响应"},
                status_code=400
            )
    except Exception as e:
        logger.error(f"Error opening device shell: {e}")
        return JSONResponse(
            content={"success": False, "message": f"打开Shell失败: {str(e)}"},
            status_code=500
        )

@app.post("/api/terminal/push")
async def push_terminal_file(request: Request, file: UploadFile = File(...)):
    """终端文件上传 - 上传到远程主机/tmp目录,供用户手动执行adb push"""
    try:
        config = config_manager.load_config()
        ssh = ssh_manager.get_connection(config)
        if not ssh:
            return JSONResponse(
                content={"success": False, "error": "SSH连接失败"},
                status_code=500
            )

        try:
            # 读取文件内容
            file_content = await file.read()

            # 上传到远程主机的/tmp目录
            remote_path = f"/tmp/{file.filename}"
            with ssh.open_sftp() as sftp:
                with sftp.file(remote_path, 'w') as remote_file:
                    remote_file.write(file_content)

            ssh_manager.return_connection(ssh)

            logger.info(f"[Terminal Upload] File uploaded: {file.filename} -> {remote_path} ({len(file_content)} bytes)")

            return JSONResponse(content={
                "success": True,
                "remote_path": remote_path,
                "filename": file.filename,
                "size": len(file_content),
                "message": f"文件已上传到 {remote_path}"
            })

        except Exception as e:
            ssh_manager.return_connection(ssh)
            logger.error(f"[Terminal Upload] Upload failed: {e}")
            return JSONResponse(
                content={"success": False, "error": f"上传失败: {str(e)}"},
                status_code=500
            )

    except Exception as e:
        logger.error(f"[Terminal Upload] Error: {e}")
        return JSONResponse(
            content={"success": False, "error": f"服务器错误: {str(e)}"},
            status_code=500
        )

# ==================== OpenGrok源码搜索 ====================

@app.post("/api/opengrok/search")
async def opengrok_search(request: Request):
    """OpenGrok源码搜索 - 调用OpenGrok插件进行源码搜索"""
    try:
        # 获取请求参数
        data = await request.json()
        query = data.get("query", "").strip()
        search_field = data.get("search_field", "smart")
        project = data.get("project", "")
        file_type = data.get("type", "")
        limit = data.get("limit", 15)

        if not query:
            return JSONResponse(
                content={"success": False, "error": "请输入搜索关键词"},
                status_code=400
            )

        # OpenGrok插件路径
        plugin_dir = "/home/hcq/remote-run-server/plugins/commands/opengrok"
        run_script = os.path.join(plugin_dir, "run.py")

        if not os.path.exists(run_script):
            logger.warning(f"[OpenGrok] Plugin not found: {run_script}")
            return JSONResponse(
                content={"success": False, "error": f"OpenGrok插件不存在: {run_script}"},
                status_code=500
            )

        # 构建命令
        cmd = [
            "python3",
            run_script,
            "search",
            "--query", query,
            "--search-field", search_field,
            "--limit", str(limit)
        ]

        # 添加可选参数
        if project:
            cmd.extend(["--project", project])
        if file_type:
            cmd.extend(["--type", file_type])

        logger.info(f"[OpenGrok Search] Command: {' '.join(cmd)}")

        # 执行搜索（使用异步版本避免阻塞）
        try:
            result = await async_subprocess_run(
                cmd,
                capture_output=True,
                text=True,
                timeout=60,
                cwd=plugin_dir
            )

            if result.returncode != 0:
                logger.error(f"[OpenGrok Search] Plugin error: {result.stderr}")
                return JSONResponse(
                    content={
                        "success": False,
                        "error": f"搜索失败: {result.stderr}"
                    },
                    status_code=500
                )

            # 解析输出
            output = result.stdout.strip()
            results = []

            if output:
                # OpenGrok插件输出格式为纯文本，按行解析
                for line in output.split('\n'):
                    line = line.strip()
                    if line and '|' in line:
                        # 格式: file_path | line_number | context_text
                        parts = line.split('|', 2)
                        if len(parts) >= 3:
                            file_path = parts[0].strip()
                            line_num = parts[1].strip()
                            context = parts[2].strip()
                            results.append({
                                "file": file_path,
                                "line": line_num,
                                "context": context
                            })

            logger.info(f"[OpenGrok Search] Found {len(results)} results for query: {query}")

            return JSONResponse(content={
                "success": True,
                "query": query,
                "search_field": search_field,
                "project": project,
                "type": file_type,
                "count": len(results),
                "results": results
            })

        except subprocess.TimeoutExpired:
            logger.error(f"[OpenGrok Search] Timeout after 60s")
            return JSONResponse(
                content={"success": False, "error": "搜索超时(60秒)"},
                status_code=500
            )
        except Exception as e:
            logger.error(f"[OpenGrok Search] Execution error: {e}")
            return JSONResponse(
                content={"success": False, "error": f"搜索执行失败: {str(e)}"},
                status_code=500
            )

    except Exception as e:
        logger.error(f"[OpenGrok Search] Error: {e}")
        return JSONResponse(
            content={"success": False, "error": f"服务器错误: {str(e)}"},
            status_code=500
        )

# ==================== 测试管理 ====================

@app.post("/api/test/start")
async def start_test(
    request: Request,
    h: Optional[str] = Query(None),
    help: bool = Query(False),
    req: TestStartRequest = Body(None)
):
    """启动测试 - 与Flask版本逻辑一致（后台执行，立即返回）"""
    # 检查是否需要显示帮助（支持 ?h 或 ?help 或 ?h=1 或 ?help=true）
    if help:
        help_text = generate_per_api_help_text("POST", "/api/test/start")
        if help_text:
            return PlainTextResponse(
                content=help_text,
                headers={
                    "Content-Type": "text/plain; charset=utf-8",
                    "Cache-Control": "public, max-age=300"
                }
            )

    # 如果req为None（显示帮助时没有body），返回错误
    if req is None:
        return JSONResponse(
            content={'success': False, 'error': 'Missing request body'},
            status_code=400
        )

    # 从请求中获取client_id
    client_id = get_client_id_from_request(request)

    # 检查用户是否已有测试在运行
    user_state = get_or_create_user_state(client_id)
    if user_state.get('running', False):
        return JSONResponse(
            content={'success': False, 'error': '您已有测试正在运行'},
            status_code=400
        )

    # 检查设备锁定状态
    devices = req.devices
    if not devices:
        return JSONResponse(
            content={'success': False, 'error': 'No devices selected'},
            status_code=400
        )

    # 获取用户名
    config = config_manager.load_config()
    username = config.get('client_username', 'unknown')

    # 尝试锁定设备
    locked_devices = []
    failed_devices = []

    for device_id in devices:
        success, message = device_lock_manager.lock_device(device_id, client_id, username)
        if success:
            locked_devices.append(device_id)
        else:
            failed_devices.append({'device_id': device_id, 'error': message})

    # 如果有设备锁定失败，释放已锁定的设备并返回错误
    if failed_devices:
        await release_device_locks(client_id, locked_devices, broadcast=False)

        error_msg = "以下设备已被其他用户占用：\n"
        for fail in failed_devices:
            error_msg += f"- {fail['device_id']} ({fail['error']})\n"

        return JSONResponse(
            content={
                'success': False,
                'error': error_msg.strip(),
                'failed_devices': failed_devices
            },
            status_code=409
        )

    # 立即广播设备锁定状态（不等待后台任务）
    if locked_devices:
        logger.info(f"[TestStart] Broadcasting device lock for: {locked_devices}")
        await broadcast_device_lock_update(locked_devices)

    # 准备测试参数
    test_params = req.model_dump()
    test_params['client_id'] = client_id

    # 确保用户状态存在（重要：在更新之前先创建）
    user_state = get_or_create_user_state(client_id)
    logger.info(f"[TestStart] Client state created/loaded: {client_id}")

    # 立即更新用户状态为运行中（与Flask版本一致）
    logger.info(f"[TestStart] Setting running=True for {client_id}")
    update_user_state_field(client_id, {
        'running': True,
        'devices': devices,
        'test_type': req.test_type,
        'logs': []  # 初始化日志列表
    })

    # 在后台任务中执行测试（不阻塞HTTP响应）
    asyncio.create_task(
        run_test_background(
            config,
            test_params,
            client_id,
            locked_devices
        )
    )

    # 立即返回响应（与Flask版本一致）
    return JSONResponse(content={"success": True, "message": "测试已启动"})


async def run_test_background(
    config: Dict[str, Any],
    test_params: Dict[str, Any],
    client_id: str,
    locked_devices: List[str]
):
    """
    后台运行测试（与Flask版本的run_test_suite逻辑一致）
    """
    ssh = None

    # 定义日志回调
    async def log_callback(message: str, log_type: Union[LogLevel, str] = LogLevel.INFO):
        # 构建时间戳
        timestamp_str = datetime.now().strftime('%H:%M:%S')

        # 使用与Flask版本一致的字符串格式
        log_str = f"[{timestamp_str}] {message}"

        # 处理log_type参数（兼容枚举和字符串）
        if isinstance(log_type, str):
            log_type_str = log_type
        else:
            log_type_str = log_type.value

        # 保存到全局状态（限制数量，防止内存溢出）
        with global_state.test_logs_lock:
            if client_id not in global_state.test_logs:
                global_state.test_logs[client_id] = deque(maxlen=MAX_LOG_ENTRIES)
            global_state.test_logs[client_id].append({
                'message': message,
                'type': log_type_str,
                'timestamp': datetime.now().isoformat()
            })

        # 保存到用户状态（限制数量，防止内存溢出）
        user_state = get_or_create_user_state(client_id)
        if 'logs' not in user_state:
            user_state['logs'] = deque(maxlen=MAX_LOG_ENTRIES)
        user_state['logs'].append(log_str)

        # 通过WebSocket推送
        await safe_websocket_send(client_id, {
            'type': 'log_update',
            'log': message,
            'log_type': log_type_str
        })

    try:
        # 检查测试是否仍在运行（可能被停止）
        user_state = get_or_create_user_state(client_id)
        if not user_state.get('running', False):
            await log_callback("测试已取消", 'warning')
            return

        # 生成进程组ID
        import time
        process_group_id = f"gms_test_{client_id.replace('@', '_')}_{int(time.time() * 1000)}"
        update_user_state_field(client_id, {'process_group_id': process_group_id})

        await log_callback(f"🔖 进程组ID: {process_group_id}", 'info')

        # 建立SSH连接
        ssh = ssh_manager.get_connection(config)
        if not ssh:
            await log_callback("❌ SSH连接失败", 'error')
            update_user_state_field(client_id, {'running': False})
            return

        await log_callback("✅ SSH 连接成功", 'success')

        # 上传测试脚本
        local_script = os.path.join(
            os.path.dirname(__file__),
            'tools',
            'run_GMS_Test_Auto.sh'
        )

        if os.path.exists(local_script):
            suites_path = config.get('suites_path', '/home/hcq/GMS-Suite')
            remote_script = os.path.join(suites_path, 'run_GMS_Test_Auto.sh')

            script_size = os.path.getsize(local_script)
            size_kb = script_size / 1024

            await log_callback(f"📤 上传文件: run_GMS_Test_Auto.sh → {remote_script} ({size_kb:.2f}KB)", 'info')

            try:
                with ssh.open_sftp() as sftp:
                    sftp.put(local_script, remote_script)

                # 设置可执行权限
                stdin, stdout, stderr = ssh.exec_command(f"chmod +x '{remote_script}'")
                stdout.read()

                await log_callback(f"✅ 上传完成 ({size_kb:.2f}KB)", 'success')
            except Exception as e:
                await log_callback(f"⚠️ 脚本上传失败: {str(e)}", 'warning')
        else:
            await log_callback("⚠️ 本地脚本不存在，使用远程脚本", 'warning')

        # 构建测试命令
        test_type = test_params.get('test_type', 'cts')
        test_module = test_params.get('test_module', '')
        test_case = test_params.get('test_case', '')
        retry_dir = test_params.get('retry_dir', '')
        test_suite = test_params.get('test_suite', '')

        # 修复：将testcases路径转换为tools路径（因为cts-tradefed在tools目录）
        if test_suite and 'testcases' in test_suite:
            test_suite_tools = test_suite.replace('/testcases', '/tools')
            await log_callback(f"🔧 转换测试套件路径: testcases -> tools", 'info')
        else:
            test_suite_tools = test_suite

        # 调试：打印test_params和config中的local_server
        await log_callback(f"🔍 test_params中的local_server: '{test_params.get('local_server', 'KEY_NOT_FOUND')}'", 'info')
        await log_callback(f"🔍 config中的local_server: '{config.get('local_server', 'NOT_FOUND')}'", 'info')

        # 修复：只有当test_params中没有local_server时才从config读取
        local_server = test_params.get('local_server') or config.get('local_server', '')
        devices = test_params.get('devices', [])

        suites_path = config.get('suites_path', '/home/hcq/GMS-Suite')
        remote_script = os.path.join(suites_path, 'run_GMS_Test_Auto.sh')

        # 构建命令参数
        cmd_parts = [remote_script]

        # 添加测试类型
        if retry_dir:
            timestamp = os.path.basename(retry_dir.strip().rstrip('/'))
            cmd_parts.extend([test_type, "retry", timestamp])
            await log_callback(f"Retry mode: {timestamp}", 'info')
        else:
            cmd_parts.append(test_type)
            if test_module:
                cmd_parts.append(test_module)
                await log_callback(f"Test module: {test_module}", 'info')
            if test_case:
                cmd_parts.append(test_case)
                await log_callback(f"Test case: {test_case}", 'info')

        # 添加设备参数
        if devices:
            device_args_list = []
            if len(devices) > 1:
                device_args_list.extend(["--shard-count", str(len(devices))])
                await log_callback(f"Sharding across {len(devices)} devices", 'info')
            for device in devices:
                device_args_list.extend(["-s", device])

            device_args_str = " ".join(device_args_list)
            cmd_parts.extend(["--device-args", device_args_str])
            await log_callback(f"Devices: {', '.join(devices)}", 'info')

        # 添加测试套件（使用tools路径）
        if test_suite_tools:
            cmd_parts.extend(["--test-suite", test_suite_tools])
            await log_callback(f"📂 测试套件: {test_suite_tools}", 'info')

        # 添加本地服务器
        await log_callback(f"🔍 local_server参数值: '{local_server}'", 'info')
        if local_server:
            cmd_parts.extend(["--local-server", local_server])
            await log_callback(f"🌐 本地主机: {local_server}", 'info')
        else:
            await log_callback("⚠️ local_server为空，测试可能失败", 'warning')

        # 构建最终命令
        import shlex
        command = ' '.join(shlex.quote(part) for part in cmd_parts)
        command_full = f"cd {os.path.dirname(remote_script)} && {command}"

        await log_callback(f"🚀 执行命令: {command}", 'info')

        # 执行测试命令（使用PTY获取实时输出）
        stdin, stdout, stderr = ssh.exec_command(command_full, get_pty=True)

        # 实时读取输出
        while not stdout.channel.exit_status_ready():
            # 检查是否被停止
            user_state = get_or_create_user_state(client_id)
            if not user_state.get('running', False):
                await log_callback("⏹️ 测试已被用户停止", 'warning')
                # 终止进程
                try:
                    ssh.exec_command("pkill -f 'run_GMS_Test_Auto.sh'")
                except (WebSocketDisconnect, ConnectionError, KeyError):
                    pass
                break

            if stdout.channel.recv_ready():
                try:
                    data = stdout.channel.recv(65536).decode('utf-8', errors='replace')
                    if data:
                        lines = data.split('\n')
                        for line in lines:
                            if line.strip():
                                await log_callback(line.strip(), 'info')
                except Exception as e:
                    logger.error(f"Error reading stdout: {e}")

            if stderr.channel.recv_stderr_ready():
                try:
                    error = stderr.channel.recv_stderr(65536).decode('utf-8', errors='replace')
                    if error:
                        lines = error.split('\n')
                        for line in lines:
                            if line.strip():
                                await log_callback(line.strip(), 'error')
                except Exception as e:
                    logger.error(f"Error reading stderr: {e}")

            await asyncio.sleep(0.05)

        # 获取退出码
        exit_code = stdout.channel.recv_exit_status()

        # 读取剩余的输出
        if stdout.channel.recv_ready():
            try:
                remaining_data = stdout.channel.recv(65536).decode('utf-8', errors='replace')
                if remaining_data:
                    lines = remaining_data.split('\n')
                    for line in lines:
                        if line.strip():
                            await log_callback(line.strip(), 'info')
            except Exception as e:
                logger.error(f"Error reading remaining stdout: {e}")

        # 读取剩余的错误输出
        if stderr.channel.recv_stderr_ready():
            try:
                remaining_error = stderr.channel.recv_stderr(65536).decode('utf-8', errors='replace')
                if remaining_error:
                    lines = remaining_error.split('\n')
                    for line in lines:
                        if line.strip():
                            await log_callback(line.strip(), 'error')
            except Exception as e:
                logger.error(f"Error reading remaining stderr: {e}")

        if exit_code == 0:
            await log_callback(f"✅ Test completed successfully (exit code: {exit_code})", 'success')
        else:
            await log_callback(f"❌ Test failed with exit code: {exit_code}", 'error')

    except Exception as e:
        logger.error(f"Error in run_test_background: {e}")
        await log_callback(f"❌ 测试执行出错: {str(e)}", 'error')

    finally:
        # 保存测试日志
        try:
            user_state = get_or_create_user_state(client_id)
            user_logs = user_state.get('logs', [])

            # 记录测试报告到数据库（从 RESULT DIRECTORY 获取）
            report_timestamp = save_test_report_to_db(client_id, config, test_params, user_logs)
            if report_timestamp:
                await log_callback(f"📊 测试报告已记录: {report_timestamp}", 'success')

        except Exception as e:
            logger.error(f"保存测试报告失败: {e}")
            import traceback
            traceback.print_exc()

        # 清理资源
        if ssh:
            ssh_manager.return_connection(ssh)

        # 释放设备锁
        await release_device_locks(client_id, locked_devices)
        logger.info(f"[Device Lock] 测试完成，已广播设备解锁状态: {locked_devices}")

        # 更新状态为停止
        update_user_state_field(client_id, {'running': False, 'devices': []})

        # 发送test_complete事件
        if client_id in global_state.websocket_connections:
            try:
                await global_state.websocket_connections[client_id].send_json({
                    'type': 'test_complete'
                })
            except Exception as e:
                logger.debug(f"WebSocket send failed (client disconnected): {e}")

@app.post("/api/test/stop")
async def stop_test(
    request: Request,
    h: Optional[str] = Query(None),
    help: bool = Query(False)
):
    """停止测试 - 与Flask版本逻辑一致"""
    # 检查是否需要显示帮助（支持 ?h 或 ?help）
    if help:
        help_text = generate_per_api_help_text("POST", "/api/test/stop")
        if help_text:
            return PlainTextResponse(
                content=help_text,
                headers={
                    "Content-Type": "text/plain; charset=utf-8",
                    "Cache-Control": "public, max-age=300"
                }
            )

    client_id = get_client_id_from_request(request)
    user_state = get_or_create_user_state(client_id)
    process_group_id = user_state.get('process_group_id')

    # 立即设置running=False（与Flask版本一致）
    update_user_state_field(client_id, {'running': False})

    # 添加停止日志
    timestamp_str = datetime.now().strftime('%H:%M:%S')
    log_str = f"[{timestamp_str}] ⏹️ 用户请求停止测试..."
    if 'logs' not in user_state:
        user_state['logs'] = []
    user_state['logs'].append(log_str)

    # 立即释放设备锁（与Flask版本一致）
    devices_to_release = user_state.get('devices', [])
    logger.info(f"[TestStop] Releasing device locks for: {devices_to_release}")
    for device_id in devices_to_release:
        device_lock_manager.unlock_device(device_id, client_id)

    # 广播设备解锁状态更新
    if devices_to_release:
        logger.info(f"[TestStop] Broadcasting device unlock for: {devices_to_release}")
        await broadcast_device_lock_update(devices_to_release)

    update_user_state_field(client_id, {'devices': []})

    # 通过SSH杀死测试进程
    config = config_manager.load_config()
    ssh = ssh_manager.get_connection(config)
    if not ssh:
        return JSONResponse(
            content={'success': False, 'error': 'SSH连接失败'},
            status_code=500
        )

    try:
        killed_count = 0

        # 方法1: 使用进程组ID杀死进程（多用户隔离）
        if process_group_id:
            # 通过环境变量 GMS_TEST_PGID 来查找和杀死相关进程
            find_cmd = f"ps eww -e | grep 'GMS_TEST_PGID={process_group_id}' | grep -v grep | awk '{{print $1}}'"
            user_state['logs'].append(f"[{datetime.now().strftime('%H:%M:%S')}] 🧹 正在终止测试进程组: {process_group_id}...")

            # 获取进程ID并杀死
            output, error, code = ssh_manager.execute_command(ssh, find_cmd, timeout=10)
            if output.strip():
                pids = output.strip().split('\n')
                for pid in pids:
                    if pid.strip():
                        ssh_manager.execute_command(ssh, f"kill -9 {pid.strip()} 2>/dev/null")
                        # 杀死子进程
                        ssh_manager.execute_command(ssh, f"pkill -9 -P {pid.strip()} 2>/dev/null")
                        killed_count += 1

                # 等待进程终止
                await asyncio.sleep(1)

                user_state['logs'].append(f"[{datetime.now().strftime('%H:%M:%S')}] ✅ 已终止 {killed_count} 个测试进程")
                ssh_manager.return_connection(ssh)
                return JSONResponse(content={"success": True, "message": "测试已停止"})

            # 回退：尝试通过命令行参数查找
            fallback_cmd = f"ps aux | grep -- '--pgid {process_group_id}' | grep -v grep | awk '{{print $2}}'"
            output2, error2, code2 = ssh_manager.execute_command(ssh, fallback_cmd, timeout=10)
            if output2.strip():
                pids = output2.strip().split('\n')
                for pid in pids:
                    if pid.strip():
                        ssh_manager.execute_command(ssh, f"kill -9 {pid.strip()} 2>/dev/null")
                        ssh_manager.execute_command(ssh, f"pkill -9 -P {pid.strip()} 2>/dev/null")
                        killed_count += 1

                await asyncio.sleep(1)
                user_state['logs'].append(f"[{datetime.now().strftime('%H:%M:%S')}] ✅ 已终止 {killed_count} 个测试进程（命令行匹配）")
                ssh_manager.return_connection(ssh)
                return JSONResponse(content={"success": True, "message": "测试已停止"})

        # 方法2: 回退到传统方法（杀死tradefed进程）
        test_type = user_state.get('test_type', 'cts')
        tradefed_bin = TRADEFED_BINARY_MAP.get(test_type, 'tradefed')
        kill_cmd = f"pkill -f '[./]?{tradefed_bin}.*run commandAndExit'"

        output, error, code = ssh_manager.execute_command(ssh, kill_cmd, timeout=10)
        ssh_manager.return_connection(ssh)

        if code == 0:
            user_state['logs'].append(f"[{datetime.now().strftime('%H:%M:%S')}] ✅ {test_type.upper()} tradefed 进程已终止")
            return JSONResponse(content={"success": True, "message": "测试已停止"})
        else:
            user_state['logs'].append(f"[{datetime.now().strftime('%H:%M:%S')}] ⚠️ 未找到运行中的测试进程")
            return JSONResponse(content={"success": True, "message": "测试已停止"})

    except Exception as e:
        ssh_manager.return_connection(ssh)
        user_state['logs'].append(f"[{datetime.now().strftime('%H:%M:%S')}] ❌ 停止测试时出错: {str(e)}")
        logger.error(f"Error stopping test: {e}")
        return JSONResponse(
            content={'success': False, 'error': str(e)},
            status_code=500
        )

@app.post("/api/test/clean")
async def clean_test_logs(request: Request):
    """清理当前用户的测试日志"""
    try:
        client_id = get_client_id_from_request(request)

        # 清除当前用户的日志
        user_state = get_or_create_user_state(client_id)
        user_state['logs'] = []
        update_user_state_field(client_id, {'logs': []})

        logger.info(f"[Clean Logs] 用户 {client_id} 清除了测试日志")

        return JSONResponse(content={
            "success": True,
            "message": "日志已清除"
        })
    except Exception as e:
        logger.error(f"Error cleaning logs: {e}")
        raise HTTPException(
                status_code=500,
                detail=f"{str(e)}. 请检查配置和参数是否正确。"
            )

# 存储最后保存的日志文件路径（用于GET下载）
last_saved_log_file = {}

@app.get("/api/test/logs/current")
async def download_current_log(request: Request):
    """下载当前测试日志"""
    global last_saved_log_file

    try:
        client_id = get_client_id_from_request(request)
        log_file = last_saved_log_file.get(client_id)

        if not log_file or not os.path.exists(log_file):
            from pathlib import Path
            logs_dir = Path(os.path.join(os.path.dirname(__file__), 'logs'))
            if logs_dir.exists():
                existing_files = [(f, f.stat().st_mtime)
                                 for f in logs_dir.glob('*.log')
                                 if f.exists()]
                if existing_files:
                    log_file = str(max(existing_files, key=lambda x: x[1])[0])

        if not log_file or not os.path.exists(log_file):
            user_state = get_or_create_user_state(client_id)
            log_file = user_state.get('log_file')

        if not log_file or not os.path.exists(log_file):
            raise HTTPException(status_code=404, detail="No log file available")

        from fastapi.responses import FileResponse as FastAPIFileResponse
        filename = os.path.basename(log_file)

        return FastAPIFileResponse(
            log_file,
            media_type='text/plain',
            filename=filename
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error downloading current log: {e}")
        raise HTTPException(
            status_code=500,
            detail=str(e)
        )

# 向后兼容别名
@app.get("/api/test/logs/download")
async def download_current_log_legacy(request: Request):
    """下载当前测试日志（向后兼容别名）"""
    return await download_current_log(request)

@app.post("/api/test/logs/batch")
async def download_test_logs(req: dict):
    """批量下载测试日志（ZIP压缩包）"""
    try:
        file_paths = req.get('files', [])
        if not file_paths:
            raise HTTPException(status_code=400, detail="未选择文件")

        result = test_logs_manager.download_logs(file_paths)

        if result['success']:
            return FileResponse(
                result['zip_path'],
                media_type='application/zip',
                filename=f"logs_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"
            )
        else:
            raise HTTPException(status_code=500, detail=result['error'])
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error downloading logs: {e}")
        raise HTTPException(
                status_code=500,
                detail=f"{str(e)}. 请检查配置和参数是否正确。"
            )

# 向后兼容别名
@app.post("/api/test/logs/download")
async def download_test_logs_legacy(req: dict):
    """批量下载测试日志（向后兼容别名）"""
    return await download_test_logs(req)

@app.post("/api/test/logs/save-current")
async def save_current_log(req: dict):
    """保存当前日志"""
    global last_saved_log_file

    log_content = req.get('content', '')
    client_id = req.get('client_id', 'test_client')
    test_type = req.get('test_type', '').strip()

    if not log_content:
        raise HTTPException(status_code=400, detail='No log content provided')

    try:
        logs_dir = os.path.join(os.path.dirname(__file__), 'logs')
        os.makedirs(logs_dir, exist_ok=True)

        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        config = config_manager.load_config()

        display_test_type = "MANUAL" if not test_type or test_type.lower() == 'unknown' else test_type.upper()

        if client_id == 'test_client':
            user_id = config.get('ubuntu_user', 'hcq')
        else:
            user_id = client_id.split('@')[0] if '@' in client_id else client_id

        log_filename = f"{user_id}_{display_test_type}_{timestamp}.log"
        log_path = os.path.join(logs_dir, log_filename)

        from pathlib import Path
        log_file = Path(log_path)

        log_file.write_text(
            f"GMS 测试日志 - {display_test_type}\n"
            f"保存时间: {timestamp}\n"
            f"用户标识: {user_id}\n"
            f"完整Client ID: {client_id}\n"
            f"{'=' * 80}\n\n"
            f"{log_content}",
            encoding='utf-8'
        )

        last_saved_log_file[client_id] = str(log_file)

        return JSONResponse(content={
            'success': True,
            'log_file': str(log_file),
            'filename': log_filename,
            'message': f'日志已保存: {log_filename}'
        })

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error saving log: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"{str(e)}. 请检查配置和参数是否正确。"
        )

@app.get("/api/test/logs/list")
async def list_test_logs():
    """列出测试日志"""
    try:
        result = test_logs_manager.list_log_files()
        return JSONResponse(content=result)
    except Exception as e:
        logger.error(f"Error listing logs: {e}")
        raise HTTPException(
                status_code=500,
                detail=f"{str(e)}. 请检查配置和参数是否正确。"
            )

@app.post("/api/test/autocomplete-suite")
async def autocomplete_suite(req: AutocompleteSuiteRequest):
    """Auto-complete test suite path with tools subdirectory"""
    try:
        test_type = req.test_type.lower()
        base_path = req.base_path

        config = config_manager.load_config()
        ssh = ssh_manager.get_connection(config)
        if not ssh:
            return JSONResponse(content={'success': False, 'error': 'SSH connection failed'}, status_code=500)

        try:
            if not base_path:
                ssh_manager.return_connection(ssh)
                return JSONResponse(content={'success': False, 'error': 'Base path is required'}, status_code=400)

            # Map test types to their suite directories and binaries (same as GUI)
            suite_map = {
                'cts': {'subdir': 'android-cts', 'binary': 'cts-tradefed'},
                'gsi': {'subdir': 'android-cts', 'binary': 'cts-tradefed'},
                'gts': {'subdir': 'android-gts', 'binary': 'gts-tradefed'},
                'sts': {'subdir': 'android-sts', 'binary': 'sts-tradefed'},
                'vts': {'subdir': 'android-vts', 'binary': 'vts-tradefed'},
                'apts': {'subdir': 'android-gts', 'binary': 'gts-tradefed'}
            }

            config_info = suite_map.get(test_type)
            if not config_info:
                ssh_manager.return_connection(ssh)
                return JSONResponse(content={'success': False, 'error': f'不支持的测试类型: {test_type}'}, status_code=400)

            subdir = config_info['subdir']
            binary = config_info['binary']

            # Try multiple path patterns to find the test suite
            candidates = []

            # Pattern 1: {base_path}/{subdir}/tools (standard structure)
            candidates.append(f"{base_path}/{subdir}/tools")

            # Pattern 2: Search for {subdir} in subdirectories of base_path
            # This handles structures like: base_path/android-gts-13.1-R1/android-gts/tools
            find_cmd = f"find '{base_path}' -maxdepth 3 -type d -name '{subdir}' 2>/dev/null | head -5"
            find_output, _, _ = ssh_manager.execute_command(ssh, find_cmd, timeout=10)

            if find_output.strip():
                for line in find_output.strip().split('\n'):
                    # Add tools subdirectory to each found subdir
                    candidates.append(f"{line}/tools")

            # Pattern 3: Check if base_path itself is already the tools directory
            # Check for binary directly in base_path
            check_direct = f"[ -x '{base_path}/{binary}' ] && echo '{base_path}' || echo ''"
            direct_output, _, _ = ssh_manager.execute_command(ssh, check_direct)
            if direct_output.strip():
                ssh_manager.return_connection(ssh)
                return JSONResponse(content={
                    'success': True,
                    'path': base_path,
                    'binary': binary,
                    'autocompleted': True
                })

            # Try each candidate path
            for candidate in candidates:
                check_cmd = f"[ -x '{candidate}/{binary}' ] && echo '{candidate}' || echo ''"
                output, error, code = ssh_manager.execute_command(ssh, check_cmd)

                if output.strip():
                    final_path = output.strip()
                    ssh_manager.return_connection(ssh)
                    return JSONResponse(content={
                        'success': True,
                        'path': final_path,
                        'binary': binary,
                        'autocompleted': True
                    })

            # If binary not found, return original path with warning (GUI behavior)
            ssh_manager.return_connection(ssh)
            return JSONResponse(content={
                'success': True,
                'path': base_path,
                'autocompleted': False,
                'warning': f'未找到 {binary}，请确认路径正确'
            })

        except Exception as e:
            ssh_manager.return_connection(ssh)
            raise
    except Exception as e:
        logger.error(f"Error autocompleting suite: {e}")
        return JSONResponse(
            content={'success': False, 'error': str(e)},
            status_code=500
        )

@app.get("/api/test/status")
async def get_status(
    request: Request,
    h: Optional[str] = Query(None),
    help: bool = Query(False)
):
    """获取测试状态 - 优化版本，减少数据传输"""
    # 检查是否需要显示帮助（支持 ?h 或 ?help）
    if help:
        help_text = generate_per_api_help_text("GET", "/api/test/status")
        if help_text:
            return PlainTextResponse(
                content=help_text,
                headers={
                    "Content-Type": "text/plain; charset=utf-8",
                    "Cache-Control": "public, max-age=300"
                }
            )
    try:
        # 处理USB事件队列（如果有）
        if hasattr(app.state, 'usb_event_queue'):
            import queue
            try:
                while True:
                    event = app.state.usb_event_queue.get_nowait()
                    # 向所有连接的WebSocket客户端发送设备变化通知
                    for client_id, ws in list(global_state.websocket_connections.items()):
                        try:
                            await ws.send_json(event)
                            logger.info(f"Sent USB event to client {client_id}: {event.get('type')}")
                        except Exception as e:
                            logger.error(f"Error sending USB event to client {client_id}: {e}")
            except queue.Empty:
                pass  # 队列为空，正常情况

        # 跟踪用户访问
        client_id = get_client_id_from_request(request)
        user_state = get_or_create_user_state(client_id)

        logger.info(f"[Status] Client {client_id} running={user_state.get('running', False)}")

        # 获取请求参数
        since = request.query_params.get('since')
        include_logs = request.query_params.get('logs', 'true').lower() == 'true'

        response = {
            'running': user_state.get('running', False),
            'devices': user_state.get('devices', []),
        }

        # 添加 USB 监控器状态信息
        from core.usb_monitor import get_usb_monitor
        usb_monitor = get_usb_monitor()
        if usb_monitor:
            response['usb_monitor'] = {
                'mode': usb_monitor.mode,
                'running': usb_monitor.is_running,
                'pyudev_available': usb_monitor.pyudev_available
            }

        # 只在需要时返回日志
        if include_logs:
            logs = user_state.get('logs', [])
            if since is not None and since.isdigit():
                since_int = int(since)
                if 0 <= since_int < len(logs):
                    # 只返回新日志（增量）
                    response['logs'] = logs[since_int:]
                    response['log_count'] = len(logs)
                else:
                    # 返回所有日志
                    response['logs'] = logs
                    response['log_count'] = len(logs)
            else:
                # 返回所有日志
                response['logs'] = logs
                response['log_count'] = len(logs)

        return JSONResponse(content=response)
    except Exception as e:
        logger.error(f"Error getting status: {e}")
        raise HTTPException(
                status_code=500,
                detail=f"{str(e)}. 请检查配置和参数是否正确。"
            )

@app.get("/api/test/logs/stream")
async def stream_test_logs(request: Request):
    """
    流式输出测试日志（纯文本格式）

    提供实时日志流，适合:
    - 命令行工具（curl, wget等）
    - 脚本自动化
    - 日志收集系统

    返回格式: 纯文本流，每行一条日志
    """
    client_id = get_client_id_from_request(request)

    async def log_stream():
        """生成纯文本日志流"""
        try:
            last_log_count = 0

            while True:
                user_state = get_or_create_user_state(client_id)
                running = user_state.get('running', False)
                logs = user_state.get('logs', [])
                current_log_count = len(logs)

                # 发送新日志
                if current_log_count > last_log_count:
                    for i in range(last_log_count, current_log_count):
                        log_entry = logs[i]
                        # 直接输出日志内容，每行一个日志
                        yield f"{log_entry}\n"
                    last_log_count = current_log_count

                # 如果测试结束，退出
                if not running and last_log_count > 0:
                    yield "=== 测试完成 ===\n"
                    break

                # 等待一段时间再检查
                await asyncio.sleep(0.5)

        except Exception as e:
            logger.error(f"Error in stream: {e}")
            yield f"错误: {str(e)}\n"

    return StreamingResponse(
        log_stream(),
        media_type="text/plain",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "Access-Control-Allow-Origin": "*",
            "X-Accel-Buffering": "no"  # 禁用nginx缓冲
        }
    )

# ==================== 报告管理 ====================
@app.get("/api/reports/list")
async def list_reports(request: Request, user_only: bool = False):
    """
    从数据库获取测试报告列表

    Args:
        user_only: 是否只显示当前用户的报告，默认 False 显示所有用户的报告
    """
    try:
        # 从数据库获取报告
        all_reports = test_report_db.get_reports(limit=100)

        # 如果要求只显示当前用户的报告，进行过滤
        if user_only:
            # 获取当前用户ID
            client_id = get_client_id_from_request(request)

            # 对于本地访问（127.0.0.1或::1），也显示配置文件中client_ip对应的报告
            config = config_manager.load_config()
            configured_ip = config.get('client_ip', '')
            username = config.get('client_username', 'unknown')

            # 构建可能的client_id列表
            possible_client_ids = [client_id]
            if configured_ip:
                # 如果当前是本地访问，添加配置文件IP对应的client_id
                if '@127.0.0.1' in client_id or '@::1' in client_id or '@localhost' in client_id:
                    possible_client_ids.append(f"{username}@{configured_ip}")

            # 过滤当前用户的报告（支持多个可能的client_id）
            all_reports = [
                r for r in all_reports
                if r.get('client_id') in possible_client_ids
            ]

        # 返回报告列表
        return JSONResponse(content={'reports': all_reports})

    except Exception as e:
        logger.error(f"获取报告列表失败: {e}")
        return JSONResponse(content={'reports': []})

@app.get("/api/reports/files/{report_timestamp}")
async def list_report_files(report_timestamp: str):
    """从数据库获取报告目录并列出文件（与Flask版本一致）"""
    try:
        # 从数据库获取报告信息
        report = test_report_db.get_report_by_timestamp(report_timestamp)

        if not report:
            return JSONResponse(
                content={'success': False, 'error': '报告不存在'},
                status_code=404
            )

        # 获取 result_dir 路径
        report_dir = report.get('result_dir')
        if not report_dir or not os.path.exists(report_dir):
            return JSONResponse(
                content={'success': False, 'error': '报告目录不存在'},
                status_code=404
            )

        # 列出文件
        files = []
        for root, dirs, filenames in os.walk(report_dir):
            for filename in filenames:
                file_path = os.path.join(root, filename)
                # 相对于报告目录的路径，不包括时间戳目录名
                rel_path = os.path.relpath(file_path, report_dir)

                # 获取文件大小
                try:
                    file_size = os.path.getsize(file_path)
                except (FileNotFoundError, OSError):
                    file_size = 0

                files.append({
                    'name': filename,
                    'path': file_path,
                    'relative_path': rel_path,
                    'size': file_size
                })

                # 限制返回数量
                if len(files) >= 100:
                    break

            if len(files) >= 100:
                break

        return JSONResponse(content={'success': True, 'files': files})

    except Exception as e:
        logger.error(f"Error listing report files: {e}")
        raise HTTPException(
            status_code=500,
            detail=str(e)
        )

@app.get("/api/reports/analyze/{report_timestamp}")
async def analyze_report(report_timestamp: str):
    """从数据库分析测试报告（与Flask版本一致）"""
    try:
        # 从数据库获取报告信息
        report = test_report_db.get_report_by_timestamp(report_timestamp)

        if not report:
            return JSONResponse(
                content={'success': False, 'error': '报告不存在'},
                status_code=404
            )

        # 获取 result_dir 路径
        result_dir = report.get('result_dir')
        if not result_dir or not await asyncio.to_thread(os.path.exists, result_dir):
            return JSONResponse(
                content={'success': False, 'error': '报告目录不存在'},
                status_code=404
            )

        # 查找 test_result.xml
        result_xml = os.path.join(result_dir, 'test_result.xml')
        if not await asyncio.to_thread(os.path.exists, result_xml):
            return JSONResponse(
                content={'success': False, 'error': 'test_result.xml 不存在'},
                status_code=404
            )

        # 使用 ReportAnalyzer 解析 XML
        analyzer = ReportAnalyzer()
        result = analyzer.analyze_file(result_xml)

        if not result:
            return JSONResponse(
                content={'success': False, 'error': '解析 XML 失败'},
                status_code=500
            )

        # 转换为前端需要的格式（与Flask版本一致）
        analysis = {
            'summary': result['summary'],
            'device_info': {
                'device': result['details']['device'],
                'android_version': result['details']['android_version']
            },
            'test_info': {
                'start_time': result['details']['start_time'],
                'test_type': result['details']['test_type']
            },
            'failures': result['failures']
        }

        return JSONResponse(content={'success': True, 'data': analysis})

    except Exception as e:
        logger.error(f"Error analyzing report: {e}")
        raise HTTPException(
            status_code=500,
            detail=str(e)
        )

@app.get("/api/reports/view")
async def view_report_file(request: Request):
    """查看报告文件内容（与Flask版本一致，通过SSH读取远程文件）"""
    try:
        file_path = request.query_params.get('path')
        if not file_path:
            return JSONResponse(
                content={'success': False, 'error': 'File path is required'},
                status_code=400
            )

        config = config_manager.load_config()
        ssh = ssh_manager.get_connection(config)
        if not ssh:
            return JSONResponse(
                content={'success': False, 'error': 'SSH connection failed'},
                status_code=500
            )

        try:
            # 读取文件内容
            cat_cmd = f"cat '{file_path}' 2>/dev/null"
            output, error, code = ssh_manager.execute_command(ssh, cat_cmd, timeout=30)

            ssh_manager.return_connection(ssh)

            # 确定内容类型
            file_ext = os.path.splitext(file_path)[1].lower()
            if file_ext in ['.xml', '.html']:
                content_type = 'text/html'
            elif file_ext == '.json':
                content_type = 'application/json'
            elif file_ext in ['.log', '.txt']:
                content_type = 'text/plain'
            else:
                content_type = 'text/plain'

            return JSONResponse(content={
                'success': True,
                'content': output,
                'content_type': content_type
            })

        except Exception as e:
            ssh_manager.return_connection(ssh)
            raise

    except Exception as e:
        logger.error(f"Error viewing report file: {e}")
        return JSONResponse(
            content={'success': False, 'error': str(e)},
            status_code=500
        )

@app.get("/api/reports/download/{report_timestamp}")
async def download_report(request: Request, report_timestamp: str):
    """下载测试报告（打包为ZIP文件）"""
    import io
    import zipfile
    from fastapi.responses import Response

    try:
        logger.info(f"[DOWNLOAD] 请求下载报告: timestamp='{report_timestamp}'")

        # 从数据库获取报告信息
        report = test_report_db.get_report_by_timestamp(report_timestamp)
        logger.info(f"[DOWNLOAD] 查询报告结果: {report is not None}")

        if not report:
            logger.error(f"[DOWNLOAD] 报告不存在: {report_timestamp}")
            # 尝试列出所有报告以供调试
            all_reports = test_report_db.get_reports(limit=10)
            logger.info(f"[DOWNLOAD] 数据库中的报告列表: {[r['timestamp'] for r in all_reports]}")
            return JSONResponse(
                content={'success': False, 'error': f'报告不存在: {report_timestamp}'},
                status_code=404
            )

        # 获取 result_dir 路径
        report_dir = report.get('result_dir')
        logger.info(f"[DOWNLOAD] 报告目录: {report_dir}, 存在: {os.path.exists(report_dir) if report_dir else False}")

        if not report_dir or not os.path.exists(report_dir):
            logger.error(f"[DOWNLOAD] 报告目录不存在: {report_dir}")
            return JSONResponse(
                content={'success': False, 'error': f'报告目录不存在: {report_dir}'},
                status_code=404
            )

        # 创建ZIP文件到内存
        zip_buffer = io.BytesIO()
        zip_filename = f"report_{report_timestamp}.zip"
        file_count = 0

        with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
            for root, dirs, filenames in os.walk(report_dir):
                for filename in filenames:
                    file_path = os.path.join(root, filename)
                    # 计算相对路径，保持目录结构
                    arcname = os.path.relpath(file_path, os.path.dirname(report_dir))

                    try:
                        zip_file.write(file_path, arcname)
                        file_count += 1
                    except Exception as e:
                        logger.warning(f"无法添加文件到ZIP: {file_path}, 错误: {e}")

        logger.info(f"创建ZIP文件: {zip_filename}, 包含 {file_count} 个文件")

        # 获取ZIP数据
        zip_data = zip_buffer.getvalue()

        if len(zip_data) == 0:
            return JSONResponse(
                content={'success': False, 'error': 'ZIP文件创建失败'},
                status_code=500
            )

        # 返回ZIP文件
        return Response(
            content=zip_data,
            media_type="application/zip",
            headers={
                "Content-Disposition": f"attachment; filename=\"{zip_filename}\""
            }
        )

    except Exception as e:
        logger.error(f"Error downloading report: {e}", exc_info=True)
        return JSONResponse(
            content={'success': False, 'error': str(e)},
            status_code=500
        )

@app.delete("/api/reports/delete")
async def delete_report(request: Request, timestamp: str = Query(..., description="报告时间戳")):
    """删除测试报告（仅限报告所有者）"""
    try:
        # 获取当前客户端信息
        client_id = get_client_id_from_request(request)

        # 从数据库获取报告
        report = test_report_db.get_report_by_timestamp(timestamp)

        if not report:
            return JSONResponse(
                content={'success': False, 'error': '报告不存在'},
                status_code=404
            )

        # 权限校验：只允许报告的所有者删除
        report_client_id = report.get('client_id')
        if report_client_id != client_id:
            logger.warning(f"[DELETE] 权限拒绝: 客户端 {client_id} 尝试删除客户端 {report_client_id} 的报告")
            return JSONResponse(
                content={'success': False, 'error': '您没有权限删除此报告'},
                status_code=403
            )

        # 删除报告目录
        result_dir = report.get('result_dir')
        if result_dir and os.path.exists(result_dir):
            import shutil
            try:
                shutil.rmtree(result_dir)
                logger.info(f"已删除报告目录: {result_dir}")
            except Exception as e:
                logger.error(f"删除报告目录失败: {e}")
                return JSONResponse(
                    content={'success': False, 'error': f'删除报告目录失败: {str(e)}'},
                    status_code=500
                )

        # 从数据库删除记录
        success = test_report_db.delete_report(timestamp)

        if success:
            return JSONResponse(content={'success': True, 'message': '报告已删除'})
        else:
            return JSONResponse(
                content={'success': False, 'error': '删除数据库记录失败'},
                status_code=500
            )

    except Exception as e:
        logger.error(f"Error deleting report: {e}")
        return JSONResponse(
            content={'success': False, 'error': str(e)},
            status_code=500
        )

@app.post("/api/reports/analyze")
async def analyze_test_report(
    file: Optional[UploadFile] = File(default=None),
    files: Optional[List[UploadFile]] = File(default=None),
    files_array: Optional[List[UploadFile]] = File(default=None, alias='files[]')
):
    """
    分析上传的测试报告文件或文件夹（使用新的简化分析器模块）

    Request: multipart/form-data
        - 'file': 单个文件上传（XML、ZIP、TAR.GZ）
        - 'files': 多文件上传（文件夹模式）
        - 'files[]': 多文件上传（HTML标准格式，兼容Flask版本）

    Response:
        {
            "success": true,
            "data": {
                "test_type": "GTS",
                "device": "device_serial",
                "android_version": "15",
                "start_time": "2025-12-02 09:35:01",
                "total": 100,
                "pass_count": 95,
                "fail_count": 5,
                "pass_rate": "95.00%",
                "failures": [
                    {
                        "name": "com.example.Test#testMethod",
                        "reason": "Failure reason...",
                        "module": "ModuleName"
                    }
                ]
            }
        }
    """
    import tempfile

    # 支持多种上传方式 - 优先使用 files[] 参数（Flask兼容）
    all_files = []
    if file:
        all_files = [file]
    elif files_array:
        all_files = files_array
    elif files:
        all_files = files

    if not all_files or len(all_files) == 0:
        return JSONResponse(
            status_code=400,
            content={
                'success': False,
                'error': '没有上传文件'
            }
        )

    if len(all_files) == 1 and all_files[0].filename == '':
        return JSONResponse(
            status_code=400,
            content={
                'success': False,
                'error': '文件名为空'
            }
        )

    try:
        # 保存上传文件到临时位置
        with tempfile.TemporaryDirectory() as temp_dir:
            # 如果是单文件（XML、ZIP、TAR.GZ）
            if len(all_files) == 1:
                uploaded_file = all_files[0]
                temp_file_path = os.path.join(temp_dir, uploaded_file.filename)

                # 保存文件内容
                with open(temp_file_path, 'wb') as f:
                    content = await uploaded_file.read()
                    f.write(content)

                # 使用 ReportAnalyzer 分析报告
                analyzer = ReportAnalyzer(temp_dir=temp_dir)
                result = analyzer.analyze_file(temp_file_path)

                if result:
                    return JSONResponse(content={
                        'success': True,
                        'data': result
                    })
                else:
                    return JSONResponse(
                        status_code=400,
                        content={
                            'success': False,
                            'error': '无法解析报告文件',
                            'message': '请确保文件是有效的XML或压缩包格式'
                        }
                    )

            # 如果是多文件（文件夹上传）
            else:
                # 保存所有文件到临时目录
                for uploaded_file in all_files:
                    if uploaded_file.filename:
                        # 保持相对路径结构
                        file_path = os.path.join(temp_dir, uploaded_file.filename)
                        # 确保目录存在
                        os.makedirs(os.path.dirname(file_path), exist_ok=True)
                        # 保存文件内容
                        with open(file_path, 'wb') as f:
                            content = await uploaded_file.read()
                            f.write(content)

                # 查找 test_result.xml 或 host_log（支持两种模式）
                analyzer = ReportAnalyzer(temp_dir=temp_dir)
                xml_path = analyzer.file_handler.find_xml_file()

                # 如果没有 test_result.xml，尝试使用日志分析器
                if not xml_path:
                    logger.info(f"未找到 test_result.xml，尝试使用HostLog日志分析器")
                    result = analyzer.analyze_log_dir(temp_dir)

                    if not result:
                        return JSONResponse(
                            status_code=400,
                            content={
                                'success': False,
                                'error': '未找到 test_result.xml 或 host_log 文件',
                                'message': f'已接收 {len(all_files)} 个文件，但文件夹中既不包含 test_result.xml 也不包含 host_log'
                            }
                        )

                    # 标记为日志分析结果
                    result['report_type'] = 'log'
                    return JSONResponse(content={
                        'success': True,
                        'data': result
                    })

                # 分析报告（使用 analyze_file 方法来获得正确的字典格式）
                result = analyzer.analyze_file(xml_path)

                if result:
                    return JSONResponse(content={
                        'success': True,
                        'data': result
                    })
                else:
                    return JSONResponse(
                        status_code=400,
                        content={
                            'success': False,
                            'error': '无法解析报告文件',
                            'message': 'test_result.xml 文件格式无效或损坏'
                        }
                    )

    except Exception as e:
        logger.error(f"报告分析失败: {e}")
        return JSONResponse(
            status_code=500,
            content={
                'success': False,
                'error': '报告分析失败',
                'message': str(e)
            }
        )

# ==================== 测试分析辅助函数 ====================

# ==================== IP和网络工具函数 ====================
import ipaddress

def extract_ip_from_host(host_string: str) -> str:
    """从user@host或host字符串中提取IP地址"""
    return host_string.split('@')[-1] if '@' in host_string else host_string

def get_client_ip_from_request_headers(request: Request) -> str:
    """从请求头中提取客户端IP，支持代理"""
    forwarded_for = request.headers.get('X-Forwarded-For', '').strip()
    if forwarded_for:
        return forwarded_for.split(',')[0].strip()

    real_ip = request.headers.get('X-Real-IP')
    if real_ip:
        return real_ip

    if request.client:
        return request.client.host

    return 'unknown'

def are_same_network(ip1: str, ip2: str, prefix_len: int = 24) -> bool:
    """检查两个IP是否在同一网段"""
    try:
        network1 = ipaddress.IPv4Network(f"{ip1}/{prefix_len}", strict=False)
        network2 = ipaddress.IPv4Network(f"{ip2}/{prefix_len}", strict=False)
        return network1 == network2
    except (ipaddress.AddressValueError, ValueError):
        # 如果IP格式无效，回退到字符串比较
        parts1 = ip1.split('.')
        parts2 = ip2.split('.')
        if len(parts1) == 4 and len(parts2) == 4:
            return parts1[:3] == parts2[:3]
        return False

def parse_cts_failure_info(test_name, error_message):
    """
    解析CTS失败信息，提取关键信息

    Args:
        test_name: 测试用例名称，如 com.google.android.gts.multiuser.RestrictedProfileHostTest#testUserIsRestricted
        error_message: 错误消息

    Returns:
        dict: 包含解析后的信息
    """
    result = {
        'class_name': None,
        'method_name': None,
        'package': None,
        'error_type': None,
        'error_keywords': []
    }

    # 解析测试名称
    if test_name and '#' in test_name:
        class_part, method_part = test_name.split('#', 1)
        result['class_name'] = class_part.strip()
        result['method_name'] = method_part.strip()

        # 提取包名
        if '.' in result['class_name']:
            parts = result['class_name'].split('.')
            result['package'] = '.'.join(parts[:-1])  # 去掉最后的类名

    # 解析错误类型
    if error_message:
        error_patterns = [
            r'(java\.lang\.(\w+Exception))',
            r'(java\.lang\.(\w+Error))',
            r'(android\.view\.(\w+Exception))',
            r'(android\.util\.(\w+Exception))',
        ]

        for pattern in error_patterns:
            match = re.search(pattern, error_message)
            if match:
                result['error_type'] = match.group(1)
                break

        # 提取错误关键词
        keyword_patterns = [
            r'Process crashed',
            r'Instrumentation run failed',
            r'Permission denied',
            r'SecurityException',
            r'NullPointerException',
            r'IllegalArgumentException',
            r'package not found',
            r'Unable to resolve',
            r'Connection refused',
        ]

        for pattern in keyword_patterns:
            if re.search(pattern, error_message, re.IGNORECASE):
                result['error_keywords'].append(pattern)

    return result


def construct_source_search_url(search_term, search_type='full'):
    """
    构造OpenGrok源码搜索URL

    Args:
        search_term: 搜索词（通常是类名）
        search_type: 搜索类型 (full, path, symbol, def)

    Returns:
        str: OpenGrok搜索命令提示
    """
    # 返回OpenGrok搜索命令的提示信息
    # 实际搜索通过OpenGrok插件完成
    return f"使用OpenGrok搜索: {search_term} (字段: {search_type})"


def analyze_test_failure_class(class_name, error_type=None):
    """
    分析测试失败的类，提供可能的源码位置和修复建议

    Args:
        class_name: 类名（如 com.google.android.gts.multiuser.RestrictedProfileHostTest）
        error_type: 错误类型（如 java.lang.AssertionError）

    Returns:
        dict: 分析结果
    """
    analysis = {
        'test_type': 'unknown',
        'possible_causes': [],
        'source_links': [],
        'suggestions': []
    }

    # 判断测试类型
    if class_name:
        if 'GmsCore' in class_name or 'gmscore' in class_name.lower():
            analysis['test_type'] = 'GMS Core测试'
            analysis['possible_causes'].append('GMS Core相关功能缺失或配置错误')
            analysis['source_links'].append({
                'title': 'GMS Core源码',
                'url': construct_source_search_url('GmsCore')
            })
        elif 'Multiuser' in class_name or 'multiuser' in class_name.lower():
            analysis['test_type'] = '多用户测试'
            analysis['possible_causes'].append('多用户功能实现不完整')
            analysis['source_links'].append({
                'title': '多用户管理源码',
                'url': construct_source_search_url('UserManagerService', 'full')
            })
        elif 'Permission' in class_name or 'permission' in class_name.lower():
            analysis['test_type'] = '权限测试'
            analysis['possible_causes'].append('权限配置缺失或不正确')
            analysis['source_links'].append({
                'title': '权限管理源码',
                'url': construct_source_search_url('PermissionManager')
            })

    # 根据错误类型添加建议
    if error_type:
        if 'AssertionError' in error_type:
            analysis['suggestions'].append('检查测试条件是否符合预期')
            analysis['suggestions'].append('验证相关功能的实现是否正确')
        elif 'SecurityException' in error_type:
            analysis['suggestions'].append('检查权限声明')
            analysis['suggestions'].append('验证签名和证书配置')
        elif 'NullPointerException' in error_type:
            analysis['suggestions'].append('检查空指针引用')
            analysis['suggestions'].append('验证初始化流程')

    # 如果没有特定建议，添加通用建议
    if not analysis['suggestions']:
        analysis['suggestions'].extend([
            '检查相关功能的完整实现',
            '验证系统配置是否符合要求',
            '查看CTS测试文档了解详细要求'
        ])

    return analysis


def extract_suggestions_from_text(text):
    """从文本中提取建议"""
    suggestions = []
    lines = text.split('\n')

    for i, line in enumerate(lines):
        line = line.strip()
        # 查找包含建议关键词的行
        if any(keyword in line for keyword in ['建议', '应该', '需要', '可以', '解决', '修复', '检查']):
            suggestions.append(line)
            # 包含后续几行（如果它们是详细的说明）
            for j in range(i + 1, min(i + 3, len(lines))):
                next_line = lines[j].strip()
                if next_line and (next_line.startswith(' ') or next_line.startswith('\t')):
                    suggestions[-1] += ' ' + next_line
                elif next_line:
                    break

    return suggestions[:5]  # 最多返回5条建议


def parse_ai_response(ai_response):
    """
    解析AI的响应，结构化返回

    Args:
        ai_response: AI返回的文本

    Returns:
        dict: 结构化的分析结果
    """
    result = {
        'raw_response': ai_response,
        'analysis': '',
        'suggestions': [],
        'root_cause': '',
        'related_docs': []
    }

    # 尝试从AI响应中提取结构化信息
    lines = ai_response.split('\n')
    current_section = None

    for line in lines:
        line = line.strip()
        if not line:
            continue

        # 识别章节
        if '根本原因' in line or '问题分析' in line:
            current_section = 'root_cause'
            result['root_cause'] = line.split(':', 1)[1].strip() if ':' in line else ''
        elif '解决' in line or '修复' in line or '方案' in line:
            current_section = 'suggestions'
        elif '分析' in line and '根本原因' not in line:
            current_section = 'analysis'
        elif line.startswith(('-', '*', '•')) or (line[0].isdigit() and '.' in line):
            # 列表项
            item = line.lstrip('-*•0123456789. ').strip()
            if current_section == 'suggestions' and item:
                result['suggestions'].append(item)
            elif current_section == 'root_cause' and item:
                result['root_cause'] += ' ' + item
            elif current_section == 'analysis' and item:
                result['analysis'] += ' ' + item
        else:
            # 普通文本
            if current_section == 'analysis':
                result['analysis'] += line + '\n'
            elif current_section == 'root_cause':
                result['root_cause'] += line + '\n'
            else:
                result['analysis'] += line + '\n'

    # 如果没有提取到结构化信息，将整个响应作为分析
    if not result['analysis'] and not result['root_cause']:
        result['analysis'] = ai_response

    # 如果没有建议，从分析中提取
    if not result['suggestions']:
        result['suggestions'] = extract_suggestions_from_text(ai_response)

    return result


def call_ai_api(api_url, api_key, model, prompt):
    """调用AI API进行分析"""
    headers = {
        'Content-Type': 'application/json',
        'Authorization': f'Bearer {api_key}'
    }

    # 根据不同的API提供商构建请求体
    if 'openai' in api_url.lower():
        data = {
            'model': model or 'gpt-3.5-turbo',
            'messages': [
                {'role': 'system', 'content': '你是一个专业的Android测试分析专家，精通CTS/GTS测试和Android系统开发。'},
                {'role': 'user', 'content': prompt}
            ],
            'temperature': 0.7
        }
    elif 'anthropic' in api_url.lower():
        data = {
            'model': model or 'claude-3-sonnet-20240229',
            'max_tokens': 2000,
            'messages': [
                {'role': 'user', 'content': prompt}
            ]
        }
    else:
        # 通用格式
        data = {
            'model': model,
            'prompt': prompt,
            'max_tokens': 2000
        }

    req = urllib.request.Request(
        api_url,
        data=json.dumps(data).encode('utf-8'),
        headers=headers,
        method='POST'
    )

    with urllib.request.urlopen(req, timeout=30) as response:
        result = json.loads(response.read().decode('utf-8'))

    # 解析返回结果
    if 'choices' in result:  # OpenAI格式
        ai_response = result['choices'][0]['message']['content']
    elif 'completion' in result:  # 其他格式
        ai_response = result['completion']
    else:
        ai_response = str(result)

    return parse_ai_response(ai_response)


def call_ollama(prompt):
    """
    调用本地ollama模型进行分析
    """
    logger = logging.getLogger(__name__)

    try:
        # 检查ollama是否安装
        check_cmd = ['which', 'ollama']
        result = subprocess.run(check_cmd, capture_output=True, text=True, timeout=5)

        if result.returncode != 0:
            logger.info("Ollama未安装")
            raise Exception('Ollama未安装')

        # 使用ollama API（默认运行在localhost:11434）
        data = {
            'model': 'llama2',  # 默认模型
            'prompt': prompt,
            'stream': False
        }

        req = urllib.request.Request(
            'http://localhost:11434/api/generate',
            data=json.dumps(data).encode('utf-8'),
            headers={'Content-Type': 'application/json'},
            method='POST'
        )

        with urllib.request.urlopen(req, timeout=10) as response:
            result = json.loads(response.read().decode('utf-8'))
            ai_response = result.get('response', '')

        return parse_ai_response(ai_response)

    except subprocess.TimeoutExpired:
        logger.warning("Ollama检查超时")
        raise Exception('Ollama检查超时')
    except FileNotFoundError:
        logger.info("Ollama命令未找到")
        raise Exception('Ollama未安装')
    except urllib.error.URLError as e:
        logger.warning(f"Ollama服务连接失败: {str(e)}")
        raise Exception(f'Ollama服务不可用: {str(e)}')
    except Exception as e:
        logger.warning(f"Ollama调用失败: {str(e)}")
        raise Exception(f'Ollama调用失败: {str(e)}')


# ==================== OpenGrok源码搜索辅助函数 ====================

def search_opengrok_sources(class_names):
    """
    使用OpenGrok搜索源码

    Args:
        class_names: 类名列表

    Returns:
        list: 搜索结果列表
    """
    logger = logging.getLogger(__name__)

    # OpenGrok插件路径
    plugin_dir = "/home/hcq/remote-run-server/plugins/commands/opengrok"
    run_script = os.path.join(plugin_dir, "run.py")

    if not os.path.exists(run_script):
        logger.warning(f"OpenGrok插件不存在: {run_script}")
        return []

    results = []

    for class_name in class_names:
        try:
            # 构建命令
            cmd = [
                "python3",
                run_script,
                "search",
                "--query", class_name,
                "--search-field", "def",  # 搜索定义
                "--limit", "5"
            ]

            logger.info(f"[OpenGrok] Searching for: {class_name}")

            # 执行搜索
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
                cwd=plugin_dir
            )

            if result.returncode == 0 and result.stdout.strip():
                # 解析输出
                for line in result.stdout.strip().split('\n'):
                    if '|' in line:
                        parts = line.split('|', 2)
                        if len(parts) >= 3:
                            results.append({
                                'class_name': class_name,
                                'file': parts[0].strip(),
                                'line': parts[1].strip(),
                                'context': parts[2].strip()
                            })
        except subprocess.TimeoutExpired:
            logger.warning(f"[OpenGrok] Timeout searching for: {class_name}")
        except Exception as e:
            logger.warning(f"[OpenGrok] Error searching for {class_name}: {e}")

    return results


def rule_based_analysis(test_name, error_message, stack_trace, module):
    """
    基于规则的分析（当AI不可用时）

    Args:
        test_name: 测试用例名称
        error_message: 错误消息
        stack_trace: 堆栈跟踪
        module: 测试模块

    Returns:
        dict: 分析结果
    """
    # 解析失败信息
    failure_info = parse_cts_failure_info(test_name, error_message)

    analysis_parts = []
    suggestions = []
    root_cause = ""
    related_docs = []

    # 根据错误类型分析
    if 'Process crashed' in error_message or 'Instrumentation run failed' in error_message:
        root_cause = "测试进程崩溃，可能是由于目标应用或服务异常退出导致"
        analysis_parts.append("测试执行过程中进程异常终止")
        suggestions.extend([
            "检查设备日志（logcat）查找崩溃原因",
            "验证被测试的应用是否正常安装和运行",
            "检查设备内存是否充足",
            "查看系统日志中是否有ANR或FC信息"
        ])
        related_docs.append({
            'title': 'Android调试指南',
            'url': 'https://source.android.com/docs/core/debug'
        })

    elif 'Permission' in error_message or 'SecurityException' in error_message:
        root_cause = "权限相关错误，缺少必要的权限声明或配置"
        analysis_parts.append("测试用例需要特定权限但未获得授权")
        suggestions.extend([
            "检查AndroidManifest.xml中的权限声明",
            "验证runtime permission是否正确请求",
            "检查签名是否匹配",
            "确认premission-level是否正确"
        ])
        related_docs.append({
            'title': 'Android权限文档',
            'url': 'https://developer.android.com/guide/topics/permissions/overview'
        })

    elif 'AssertionError' in error_message:
        root_cause = "断言失败，测试条件不满足"
        analysis_parts.append("测试断言检查失败")

        if 'multiuser' in test_name.lower():
            analysis_parts.append("多用户功能测试失败")
            suggestions.extend([
                "检查UserManager服务是否正常",
                "验证多用户配置是否正确",
                "确认restricted profile功能已实现",
                "检查用户切换相关API"
            ])
            related_docs.append({
                'title': 'Android多用户文档',
                'url': 'https://source.android.com/docs/core/architecture/configuration/multi-user'
            })

        if 'GmsCore' in test_name or 'gmscore' in test_name.lower():
            analysis_parts.append("GMS Core相关测试失败")
            suggestions.extend([
                "检查GMS Core包是否正确安装",
                "验证GMS服务权限配置",
                "检查Google Play Services版本",
                "确认GMS证书配置正确"
            ])
            related_docs.append({
                'title': 'GMS Core文档',
                'url': 'https://developer.android.com/google/play/services'
            })

    elif 'package not found' in error_message.lower():
        root_cause = "目标包未找到或未安装"
        suggestions.extend([
            "确认目标应用已正确安装",
            "检查包名是否正确",
            "验证应用是否与当前Android版本兼容"
        ])

    # 通用建议
    if not suggestions:
        suggestions = [
            "查看完整的测试日志了解详细错误信息",
            "检查设备状态是否正常",
            "验证测试环境配置",
            "查阅CTS/GTS测试文档了解测试要求"
        ]

    # 组合分析结果
    analysis = "\n".join(analysis_parts) if analysis_parts else "测试执行失败，请查看详细错误信息"

    # 如果没有根本原因，从错误消息中推断
    if not root_cause:
        if failure_info.get('error_type'):
            root_cause = f"错误类型: {failure_info['error_type']}"
        else:
            root_cause = "测试执行过程中出现异常"

    return {
        'analysis': analysis,
        'suggestions': suggestions[:8],  # 最多8条建议
        'root_cause': root_cause,
        'related_docs': related_docs,
        'ai_enabled': False  # 标记这不是AI分析
 }


def analyze_with_ai(test_name, error_message, stack_trace='', module='', class_names=None):
    """
    调用大模型API分析测试失败（支持多个AI提供商，自动获取源码）

    Args:
        test_name: 测试用例名称
        error_message: 错误消息
        stack_trace: 堆栈跟踪
        module: 测试模块名称
        class_names: 从堆栈中提取的类名列表

    Returns:
        dict: AI分析结果（包含源码分析）
    """
    logger = logging.getLogger(__name__)

    if class_names is None:
        class_names = []

    # 使用OpenGrok搜索相关源码
    opengrok_results = []
    if class_names:
        opengrok_results = search_opengrok_sources(class_names[:3])  # 限制搜索前3个类名
        logger.info(f"OpenGrok搜索到 {len(opengrok_results)} 条源码结果")

    # 优先使用通用AI分析器
    try:
        from core.universal_ai import get_universal_analyzer

        # 获取通用AI分析器
        ai_analyzer = get_universal_analyzer()

        # 解析测试信息
        failure_info = parse_cts_failure_info(test_name, error_message)

        # 调用AI分析（自动获取源码）
        result = ai_analyzer.analyze_test_failure(
            class_name=failure_info.get('class_name', ''),
            method_name=failure_info.get('method_name'),
            error_message=error_message,
            stack_trace=stack_trace,
            auto_fetch_source=True  # 启用自动源码获取
        )

        if result['success']:
            # 转换为旧格式以保持兼容性
            provider_name = result.get('provider', 'unknown')
            provider_display = {
                'zhipu': 'GLM-4 (智谱AI)',
                'ollama': 'Ollama本地模型',
                'openai': 'GPT-4 (OpenAI)',
                'anthropic': 'Claude (Anthropic)'
            }.get(provider_name, provider_name)

            response = {
                'analysis': result.get('analysis', ''),
                'suggestions': result.get('suggestions', []),
                'root_cause': result.get('solution', {}).get('problem_description', ''),
                'related_docs': [],
                'ai_enabled': True,
                'ai_model': provider_display,
                'ai_provider': provider_name
            }

            # 添加源码信息
            if result.get('source_info'):
                source_info = result['source_info']
                response['source_code_fetched'] = True
                response['source_url'] = source_info.get('source_url', '')
                response['source_file_path'] = source_info.get('file_path', '')
                logger.info(f"分析包含源码: {source_info.get('file_path', 'unknown')}")

            # 添加OpenGrok搜索结果
            if opengrok_results:
                response['opengrok_results'] = opengrok_results
                logger.info(f"添加了 {len(opengrok_results)} 条OpenGrok搜索结果")

            return response
        else:
            logger.warning(f"AI分析失败: {result.get('error')}")
            raise Exception(result.get('error', 'AI分析失败'))

    except ImportError:
        logger.info("通用AI分析器未安装")
    except Exception as e:
        logger.warning(f"通用AI分析失败: {str(e)}")

    # 回退到旧的AI分析方法
    try:
        # 构建分析提示词
        prompt = f"""请分析以下CTS测试失败信息，给出详细的原因分析和解决方案：

测试用例: {test_name}
测试模块: {module if module else '未知'}
错误信息: {error_message}

{f'''堆栈跟踪:
{stack_trace}
''' if stack_trace else ''}

请提供：
1. 问题根本原因分析
2. 具体的解决方案和修复步骤
3. 需要检查的系统配置或代码位置
4. 相关的Android源码模块或类

请用中文回答，格式清晰，包含具体的操作步骤。"""

        # 尝试调用本地安装的AI模型（如通过ollama）
        # 首先检查配置中是否有AI API设置
        config = config_manager.load_config()
        ai_api_key = config.get('ai_api_key', '')
        ai_api_url = config.get('ai_api_url', '')
        ai_model = config.get('ai_model', '')

        logger.info(f"AI配置检查: api_url={ai_api_url}, api_key_set={bool(ai_api_key)}, model={ai_model}")

        # 如果配置了API，使用API调用
        if ai_api_url and ai_api_key:
            logger.info("使用AI API进行分析")
            return call_ai_api(ai_api_url, ai_api_key, ai_model, prompt)
        else:
            # 尝试使用本地ollama
            logger.info("尝试使用本地ollama进行分析")
            return call_ollama(prompt)
    except Exception as e:
        # 如果AI调用失败，返回基于规则的分析
        logger.warning(f"AI调用失败，使用基于规则的分析: {str(e)}")
        try:
            return rule_based_analysis(test_name, error_message, stack_trace, module)
        except Exception as rule_error:
            logger.error(f"规则分析也失败: {str(rule_error)}")
            # 最后的兜底响应
            return {
                'analysis': f'测试分析遇到错误: {str(e)}',
                'suggestions': ['检查服务器日志了解详细错误信息'],
                'root_cause': '分析服务异常',
                'related_docs': [],
                'ai_enabled': False
            }


def get_source_code_suggestions(test_name, error_message, stack_trace=None):
    """
    根据测试失败信息获取源码查询链接和分析建议

    Args:
        test_name: 测试用例名称
        error_message: 错误消息
        stack_trace: 堆栈跟踪（可选）

    Returns:
        dict: 包含搜索链接和分析建议
    """
    # 解析失败信息
    failure_info = parse_cts_failure_info(test_name, error_message)

    # 分析测试失败
    analysis = analyze_test_failure_class(
        failure_info.get('class_name', ''),
        failure_info.get('error_type')
    )

    result = {
        'test_info': {
            'name': test_name,
            'class': failure_info.get('class_name'),
            'method': failure_info.get('method_name'),
            'package': failure_info.get('package')
        },
        'error_info': {
            'type': failure_info.get('error_type'),
            'message': error_message[:500] if error_message else '',
            'keywords': failure_info.get('error_keywords', [])
        },
        'analysis': analysis,
        'search_links': [],
        'source_analysis': None  # 新增：源码分析结果
    }

    # 尝试进行源码分析（异步，不阻塞主流程）
    try:
        from core.source_analyzer import source_analyzer

        class_name = failure_info.get('class_name', '')
        if class_name:
            # 提取简单类名
            simple_class_name = class_name.split('.')[-1]

            # 执行源码分析
            source_analysis_result = source_analyzer.analyze_failure_with_source(
                class_name=simple_class_name,
                method_name=failure_info.get('method_name'),
                error_message=error_message,
                stack_trace=stack_trace
            )

            result['source_analysis'] = source_analysis_result

            # 如果找到了源码，添加分析结果和建议
            if source_analysis_result.get('source_found'):
                # 合并源码分析的结果到主分析中
                if source_analysis_result.get('analysis'):
                    analysis['possible_causes'].extend(source_analysis_result['analysis'])
                if source_analysis_result.get('suggestions'):
                    analysis['suggestions'].extend(source_analysis_result['suggestions'])

                # 添加源码链接
                if source_analysis_result.get('source_url'):
                    result['search_links'].insert(0, {
                        'title': f'查看源码: {simple_class_name}.java',
                        'url': source_analysis_result['source_url']
                    })

    except Exception as e:
        logger.warning(f"源码分析失败: {e}")
        result['source_analysis'] = {
            'source_found': False,
            'error': str(e)
        }

    # 生成搜索链接
    if failure_info.get('class_name'):
        # 只搜索类名（不含包名）
        class_name = failure_info["class_name"]
        simple_class_name = class_name.split('.')[-1]  # 提取简单类名

        result['search_links'].append({
            'title': f'搜索测试类: {simple_class_name}',
            'url': construct_source_search_url(simple_class_name)
        })

    # 搜索错误类型
    if failure_info.get('error_type'):
        result['search_links'].append({
            'title': f'搜索错误类型: {failure_info["error_type"]}',
            'url': construct_source_search_url(failure_info["error_type"])
        })

    # 如果有堆栈跟踪，提取相关类
    if stack_trace:
        # 提取at行中的类名
        at_pattern = r'at\s+([a-zA-Z0-9.$_]+)\.'
        classes_found = set(re.findall(at_pattern, stack_trace))

        for cls in list(classes_found)[:3]:  # 最多3个
            if not cls.startswith(failure_info.get('package', '')):
                result['search_links'].append({
                    'title': f'搜索相关类: {cls}',
                    'url': construct_source_search_url(cls)
                })

    # 添加通用搜索链接
    if failure_info.get('error_keywords'):
        keyword = failure_info['error_keywords'][0]
        result['search_links'].append({
            'title': f'搜索问题: {keyword}',
            'url': construct_source_search_url(keyword)
        })

    return result


@app.post("/api/reports/analyze-source")
async def analyze_test_source(req: dict):
    """
    分析测试失败并提供Android源码查询链接

    Request body:
        {
            "test_name": "com.google.android.gts.multiuser.RestrictedProfileHostTest#testUserIsRestricted",
            "error_message": "java.lang.AssertionError: ...",
            "stack_trace": "..."  // 可选
        }

    Response:
        {
            "success": true,
            "data": {
                "test_info": {...},
                "error_info": {...},
                "analysis": {...},
                "search_links": [...]
            }
        }
    """
    try:
        test_name = req.get('test_name', '')
        error_message = req.get('error_message', '')
        stack_trace = req.get('stack_trace', '')

        if not test_name:
            raise HTTPException(status_code=400, detail="缺少test_name参数")

        # 获取源码建议
        result = get_source_code_suggestions(test_name, error_message, stack_trace)

        return JSONResponse(content={'success': True, 'data': result})

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"源码分析失败: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"源码分析失败: {str(e)}"
        )


@app.post("/api/reports/analyze-ai")
async def ai_analyze_failure(req: dict):
    """
    使用AI分析测试失败（自动获取源码并分析，使用OpenGrok源码搜索）

    功能说明：
    - 使用OpenGrok自动获取测试用例源码
    - 使用OpenGrok搜索失败堆栈中的相关类源码
    - 结合源码、错误信息和测试逻辑进行综合分析
    - 提供诊断结果和修复建议

    Request body:
        {
            "test_name": "com.google.android.gts.multiuser.RestrictedProfileHostTest#testUserIsRestricted",
            "error_message": "java.lang.AssertionError: ...",
            "stack_trace": "...",  // 可选
            "module": "GtsGmscoreHostTestCases",  // 可选
            "class_names": ["com.android.server.xxx", "MyClass"]  // 可选：提取的类名列表
        }

    Response:
        {
            "success": true,
            "data": {
                "analysis": "...",  // AI分析结果（包含源码分析）
                "suggestions": [...],  // 解决建议
                "root_cause": "...",  // 根本原因
                "source_code_fetched": true,  // 是否成功获取源码
                "source_url": "...",  // 源码链接（如果获取成功）
                "source_file_path": "...",  // 源码文件路径
                "opengrok_results": [...],  // OpenGrok搜索结果（如果有）
                "ai_model": "GLM-4 (智谱AI)",  // 使用的AI模型
                "related_docs": [...]
            }
        }
    """
    try:
        test_name = req.get('test_name', '')
        error_message = req.get('error_message', '')
        stack_trace = req.get('stack_trace', '')
        module = req.get('module', '')
        class_names = req.get('class_names', [])  # 新增：提取的类名列表

        if not test_name:
            raise HTTPException(status_code=400, detail="缺少test_name参数")

        # 调用AI分析（包含OpenGrok源码搜索）
        result = analyze_with_ai(test_name, error_message, stack_trace, module, class_names)

        return JSONResponse(content={'success': True, 'data': result})

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"AI分析失败: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"AI分析失败: {str(e)}"
        )

# ==================== VNC管理 ====================
@app.get("/api/desktop/vnc/status")
async def get_desktop_vnc_status():
    """获取VNC状态"""
    try:
        result = vnc_manager.get_vnc_status()
        return JSONResponse(content={
            "success": True,
            "data": result
        })
    except Exception as e:
        logger.error(f"Error getting VNC status: {e}")
        raise HTTPException(
                status_code=500,
                detail=f"{str(e)}. 请检查配置和参数是否正确。"
            )

@app.post("/api/desktop/vnc/start")
async def start_desktop_vnc(req: Optional[VNCStartRequest] = Body(default=None)):
    """启动桌面VNC（Ubuntu桌面的VNC服务）"""
    if req is None:
        # 如果没有提供请求体，使用配置文件的默认值
        config = config_manager.load_config()
        host = f"{config.get('ubuntu_user', 'hcq')}@{config.get('ubuntu_host', 'localhost')}"
        password = config.get('ubuntu_pswd', '')
        vnc_password = ''
    else:
        host = req.host
        password = req.password
        vnc_password = req.vnc_password or ''

    result = vnc_manager.start_vnc(host, password, vnc_password)
    return JSONResponse(content=result)

@app.post("/api/desktop/vnc/stop")
async def stop_desktop_vnc():
    """停止桌面VNC"""
    result = vnc_manager.stop_vnc()
    return JSONResponse(content=result)


@app.post("/api/desktop/validate")
async def validate_desktop_host(req: dict = Body(...)):
    """验证桌面主机连接并检查VNC服务"""
    try:
        host_connection = req.get('host', '')
        password = req.get('password', '')

        if not host_connection or '@' not in host_connection:
            return JSONResponse(
                content={'success': False, 'error': '无效的主机格式'},
                status_code=400
            )

        try:
            user, ip = host_connection.split('@', 1)
        except ValueError:
            return JSONResponse(
                content={'success': False, 'error': '主机格式错误'},
                status_code=400
            )

        # 检查是否是本地主机
        is_local = CommonUtils.is_local_host(ip)

        if is_local:
            # 本地主机直接验证成功
            return JSONResponse(content={
                'success': True,
                'message': '本地主机验证成功',
                'needs_password': False,
                'local': True
            })

        # 远程主机验证
        ssh = None
        try:
            # 使用 ssh_manager 获取连接
            config = {
                'hostname': ip,
                'username': user,
                'password': password,
                'timeout': 10
            }
            ssh = ssh_manager.create_connection(config)
            if not ssh:
                return JSONResponse(
                    content={'success': False, 'error': 'SSH连接失败', 'needs_password': True},
                    status_code=401
                )

            return JSONResponse(content={
                'success': True,
                'message': '主机验证成功',
                'needs_password': False,
                'password': password if password else ''
            })
        finally:
            # 确保SSH连接返回到连接池
            if ssh:
                try:
                    ssh_manager.return_connection(ssh)
                except Exception as e:
                    logger.warning(f"Failed to return SSH connection: {e}")

    except paramiko.AuthenticationException:
        return JSONResponse(
            content={'success': False, 'error': 'SSH认证失败', 'needs_password': True},
            status_code=401
        )
    except Exception as e:
        logger.error(f"Error validating host: {e}")
        return JSONResponse(
            content={'success': False, 'error': str(e)},
            status_code=500
        )

@app.post("/api/devices/screen")
async def show_device_screens(req: DeviceActionRequest):
    """显示设备屏幕（启动scrcpy投屏）"""
    try:
        devices = req.devices

        config = config_manager.load_config()
        ubuntu_user = config.get('ubuntu_user', 'hcq')
        ubuntu_host = config.get('ubuntu_host', '')

        if not devices:
            # 尝试从已连接设备列表获取
            ssh = ssh_manager.get_connection(config)
            if ssh:
                try:
                    stdout, stderr, code = ssh_manager.execute_command(ssh, "adb devices", timeout=5)
                    ssh_manager.return_connection(ssh)
                    if code == 0 and stdout:
                        # 解析设备列表
                        lines = stdout.strip().split('\n')[1:]  # 跳过第一行 "List of devices attached"
                        devices = [line.split()[0] for line in lines if line.strip() and '\tdevice' in line]
                except (WebSocketDisconnect, ConnectionError, KeyError):
                    pass

        if not devices:
            return JSONResponse(content={'success': False, 'error': 'No devices selected'}, status_code=400)

        ssh = ssh_manager.get_connection(config)
        if not ssh:
            return JSONResponse(content={'success': False, 'error': 'SSH connection failed'}, status_code=500)

        try:
            # Check VNC service status
            vnc_check_cmd = f"curl -s -o /dev/null -w '%{{http_code}}' http://{ubuntu_host}:6080 --connect-timeout 3"
            vnc_output, _, _ = ssh_manager.execute_command(ssh, vnc_check_cmd, timeout=5)
            vnc_available = vnc_output.strip() == '200'

            # Check scrcpy availability
            scrcpy_path = config.get("scrcpy_path", "")
            if scrcpy_path:
                # Substitute ubuntu_user in path
                scrcpy_path = scrcpy_path.replace('${ubuntu_user}', ubuntu_user)
                scrcpy_check_cmd = f"test -f '{scrcpy_path}' && echo 'exists' || echo 'not_found'"
                scrcpy_output, _, scrcpy_code = ssh_manager.execute_command(ssh, scrcpy_check_cmd)

                if "not_found" in scrcpy_output:
                    ssh_manager.return_connection(ssh)
                    return JSONResponse(content={
                        'success': False,
                        'error': f'scrcpy未找到: {scrcpy_path}',
                        'instructions': '请检查配置文件中的 scrcpy_path 路径'
                    }, status_code=404)
            else:
                # Fallback to checking PATH
                scrcpy_check_cmd = "which scrcpy"
                scrcpy_output, _, scrcpy_code = ssh_manager.execute_command(ssh, scrcpy_check_cmd)

                if scrcpy_code != 0:
                    ssh_manager.return_connection(ssh)
                    return JSONResponse(content={
                        'success': False,
                        'error': 'scrcpy未安装',
                        'instructions': 'sudo apt-get install -y scrcpy'
                    }, status_code=404)
                scrcpy_path = "scrcpy"  # Use command from PATH

            # 启动scrcpy
            results = []
            vnc_sessions = []

            # 使用智能位置计算函数
            positions = calculate_window_positions(devices)

            for idx, device_id in enumerate(sorted(devices)):
                # 使用智能计算的窗口位置
                x_offset = positions['start_x'] + idx * (positions['window_width'] + positions['horizontal_gap'])
                y_offset = positions['start_y']
                window_width = positions['window_width']
                window_height = positions['window_height']

                # 使用nohup确保scrcpy在SSH连接关闭后继续运行
                cmd = (
                    f"export DISPLAY=:0 && "
                    f"if [ -f /run/user/1000/gdm/Xauthority ]; then "
                    f"export XAUTHORITY=/run/user/1000/gdm/Xauthority; "
                    f"else "
                    f"export XAUTHORITY=/home/{ubuntu_user}/.Xauthority; "
                    f"fi && "
                    f"(nohup {scrcpy_path} -s {device_id} "
                    f"--max-size 800 "
                    f"--stay-awake "
                    f"--window-title '{device_id}' "
                    f"--window-x {x_offset} "
                    f"--window-y {y_offset} "
                    f"--window-width {window_width} "
                    f"--window-height {window_height} "
                    f"> /tmp/scrcpy_{device_id}.log 2>&1 &)"
                )

                ssh_manager.execute_command(ssh, cmd, timeout=10)

                # 验证scrcpy是否成功启动
                await asyncio.sleep(0.3)
                check_cmd = f"pgrep -f 'scrcpy.*-s {device_id}' && echo 'RUNNING' || echo 'NOT_RUNNING'"
                check_output, _, _ = ssh_manager.execute_command(ssh, check_cmd, timeout=5)
                is_started = 'RUNNING' in check_output

                results.append({
                    'device': device_id,
                    'started': is_started,
                    'position': {'x': x_offset, 'y': y_offset, 'width': window_width, 'height': window_height}
                })

                vnc_sessions.append({
                    'device': device_id,
                    'url': f"http://{ubuntu_host}:6080/vnc.html?autoconnect=true" if vnc_available else None,
                    'message': 'VNC查看可用' if vnc_available else '仅本地显示'
                })

            ssh_manager.return_connection(ssh)

            # 构建详细消息
            newly_started = [r['device'] for r in results if r.get('started')]
            failed_devices = [r['device'] for r in results if not r.get('started')]

            message_parts = []
            if newly_started:
                message_parts.append(f"✅ 已启动{len(newly_started)}个投屏设备: {', '.join(newly_started)}")
            if failed_devices:
                message_parts.append(f"❌ {len(failed_devices)}个设备启动失败: {', '.join(failed_devices)}")

            message = '\n'.join(message_parts) if message_parts else '投屏启动完成'

            return JSONResponse(content={
                'success': len(failed_devices) == 0,
                'message': message,
                'results': results,
                'vnc_sessions': vnc_sessions,
                'desktop_url': '/desktop',
                'note': '点击"主机桌面"查看屏幕' if vnc_available else 'VNC未启动，屏幕仅在本地显示'
            })
        except Exception as e:
            ssh_manager.return_connection(ssh)
            raise

    except Exception as e:
        logger.error(f"Error showing device screens: {e}")
        return JSONResponse(
            content={"success": False, "error": str(e)},
            status_code=500
        )

# ==================== ADB转发 ====================
@app.post("/api/adb-forward/start")
async def start_adb_forward(req: ADBForwardStartRequest):
    """启动ADB转发"""
    try:
        result = adb_forward_manager.start_forward(req.device_host, req.device_password)
        if result.get('success'):
            return JSONResponse(content=result)
        else:
            raise HTTPException(status_code=500, detail=result.get('error', 'ADB转发启动失败'))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error starting ADB forward: {e}")
        raise HTTPException(
                status_code=500,
                detail=f"{str(e)}. 请检查配置和参数是否正确。"
            )

@app.post("/api/adb-forward/stop")
async def stop_adb_forward():
    """停止ADB转发"""
    try:
        client_id = 'test_client'
        result = adb_forward_manager.stop_forward(client_id)
        if result.get('success'):
            return JSONResponse(content=result)
        else:
            raise HTTPException(status_code=500, detail=result.get('error', 'ADB转发停止失败'))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error stopping ADB forward: {e}")
        raise HTTPException(
                status_code=500,
                detail=f"{str(e)}. 请检查配置和参数是否正确。"
            )

# ==================== USB/IP ====================
@app.get("/api/usbip/status")
async def get_usbip_status(request: Request):
    """
    获取 USB/IP 状态（与5000端口完全一致）

    通过检查多个维度来判断 USB/IP 连接状态：
    1. 检查当前客户端的连接状态记录
    2. 检查全局 USB/IP 设备来源记录（支持刷新页面后恢复状态）
    """
    client_id = get_client_id_from_request(request)

    # 方法1：检查当前客户端的连接状态
    with global_state.usbip_states_lock:
        state_info = global_state.usbip_states.get(client_id, {'connected': False, 'timestamp': 0})
        connected = state_info['connected']

    # 方法2：如果当前客户端没有记录，检查是否有全局 USB/IP 设备记录
    # 这样可以支持刷新页面后恢复按钮状态
    if not connected:
        with global_state.usbip_devices_source_lock:
            # 如果有任何 USB/IP 设备记录，说明有 USB/IP 连接
            has_usbip_devices = len(global_state.usbip_devices_source) > 0
            if has_usbip_devices:
                connected = True

    logger.info(f"[USB/IP Status] client_id={client_id}, connected={connected}, device_count={len(global_state.usbip_devices_source)}")
    return JSONResponse(content={'connected': connected})

@app.post("/api/usbip/start")
async def start_usbip(
    req: Optional[USBIPStartRequest] = Body(default=None),
    request: Request = None,
    help: bool = Query(False)
):
    """启动 USB/IP 转发（使用usbip_manager.start_usbip高级封装方法 - 与Flask版本一致）"""
    # 检查是否需要显示帮助
    if help:
        help_text = generate_per_api_help_text("POST", "/api/usbip/start")
        if help_text:
            return PlainTextResponse(
                content=help_text,
                headers={"Content-Type": "text/plain; charset=utf-8", "Cache-Control": "public, max-age=300"}
            )

    try:
        config = config_manager.load_config()
        client_id = get_client_id_from_request(request)

        # 从请求中获取参数
        request_data = req.model_dump() if req else {}

        # 获取device_host（优先级：请求参数 > 配置文件 > client_id）
        device_host = request_data.get('device_host') or config.get('usbip_device_host') or config.get('device_host')
        if not device_host:
            device_host = client_id

        logger.info(f"[USB/IP] Using device_host: {device_host}")

        # 保存原始 Windows 设备主机地址，用于记录设备来源
        windows_device_host = device_host

        # 获取密码
        device_password = request_data.get('device_password') or find_device_host_password(config, device_host) or config.get('device_pswd', '')

        if not device_password:
            return ApiResponse.error(
                f'未找到 {device_host} 的SSH凭据，请先在登录页面输入SSH密码',
                status_code=401
            )

        # 直接调用高级封装方法（简化实现，与Flask版本一致）
        result = usbip_manager.start_usbip(device_host, device_password)

        # 更新连接状态（使用线程锁 - 与Flask版本一致）
        if result.get('success'):
            with global_state.usbip_states_lock:
                global_state.usbip_states[client_id] = {'connected': True, 'timestamp': time.time()}
            logger.info(f"[USB/IP Start] Set connected=True for client_id={client_id}")

            # 记录设备来源（使用线程锁 - 与Flask版本一致）
            device_list = result.get('device_list', [])
            if device_list:
                with global_state.usbip_devices_source_lock:
                    for device_id in device_list:
                        global_state.usbip_devices_source[device_id] = {
                            'source': windows_device_host,
                            'timestamp': time.time()
                        }
                logger.info(f"[USB/IP Start] Recorded device source: {windows_device_host} for devices: {device_list}")

        return JSONResponse(content=result)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error starting USB/IP: {e}")
        return ApiResponse.error(str(e), status_code=500)

@app.post("/api/usbip/stop")
async def stop_usbip(request: Request):
    """停止 USB/IP 转发（与5000端口完全一致）"""
    config = config_manager.load_config()
    client_id = get_client_id_from_request(request)
    device_host = client_id
    config['device_host'] = device_host

    # 自动从 client_ssh_credentials 中查找密码
    device_password = find_device_host_password(config, device_host)
    if not device_password:
        device_password = config.get('device_pswd', '')

    if device_password:
        config['device_pswd'] = device_password

    win_ssh = create_device_ssh_connection(config)
    if not win_ssh:
        # 无法连接到 Windows，只清除连接状态
        # 注意：不清除设备来源记录，因为设备仍然在测试主机上
        with global_state.usbip_states_lock:
            global_state.usbip_states[client_id] = {'connected': False, 'timestamp': time.time()}
        logger.info(f"[USB/IP Stop] Connection cleared (device source preserved)")
        return JSONResponse(content={'success': True, 'message': '本地设备已断开'})

    try:
        ssh_manager.execute_command(win_ssh, 'usbipd unbind --all', timeout=10)
        ssh_manager.return_connection(win_ssh)
        await asyncio.sleep(2)

        # 只更新 USB/IP 连接状态，不清除设备来源记录
        # 设备仍然在测试主机上，来源信息应该保留
        with global_state.usbip_states_lock:
            global_state.usbip_states[client_id] = {'connected': False, 'timestamp': time.time()}
        logger.info(f"[USB/IP Stop] Connection cleared (device source preserved)")

        return JSONResponse(content={
            'success': True,
            'message': '本地设备已断开'
        })
    except Exception as e:
        ssh_manager.return_connection(win_ssh)
        # 即使失败也清除连接状态，但保留设备来源记录
        with global_state.usbip_states_lock:
            global_state.usbip_states[client_id] = {'connected': False, 'timestamp': time.time()}
        logger.info(f"[USB/IP Stop] Connection cleared on error (device source preserved)")
        return JSONResponse(content={'success': True, 'message': '本地设备已断开'})

@app.post("/api/usbip/auto-install")
async def auto_install_usbipd(request: Request):
    """自动安装 usbipd 到 Windows 主机"""
    try:
        config = config_manager.load_config()
        client_id = get_client_id_from_request(request)
        device_host = client_id
        config['device_host'] = device_host

        # 自动从 client_ssh_credentials 中查找密码
        device_password = find_device_host_password(config, device_host)
        if not device_password:
            device_password = config.get('device_pswd', '')

        if device_password:
            config['device_pswd'] = device_password

        # 连接到 Windows 主机
        win_ssh = create_device_ssh_connection(config)
        if not win_ssh:
            return JSONResponse(
                content={"success": False, "error": "无法连接到 Windows 主机"},
                status_code=500
            )

        try:
            # 使用 USBIPManager 的安装方法
            result = usbip_manager.install_usbipd(win_ssh, config)
            ssh_manager.return_connection(win_ssh)
            return JSONResponse(content=result)

        except Exception as e:
            ssh_manager.return_connection(win_ssh)
            raise

    except Exception as e:
        logger.error(f"Error auto-installing usbipd: {e}")
        return JSONResponse(
            content={"success": False, "error": str(e)},
            status_code=500
        )

# ==================== VPN管理 ====================
@app.get("/api/ssh/sshd-check")
async def check_ssh_sshd(request: Request):
    """检查VPN SSH服务状态

    通过SSH连接到Windows客户端检查SSHD服务状态。
    """
    def exec_ssh_cmd(ssh, cmd):
        """执行SSH命令并返回输出"""
        stdin, stdout, stderr = ssh.exec_command(cmd, timeout=10)
        return stdout.read().decode('utf-8', errors='ignore').strip()

    try:
        config = config_manager.load_config()
        device_host = get_client_id_from_request(request)
        config['device_host'] = device_host
        config['device_pswd'] = find_device_host_password(config, device_host) or config.get('device_pswd', '')

        ssh = create_device_ssh_connection(config)
        if not ssh:
            logger.warning(f"[SSHD Check] Cannot connect to {device_host}")
            return JSONResponse(content={
                "success": True,
                "installed": False,
                "running": False,
                "error": "无法连接到SSH服务，请检查网络连接和Windows客户端状态"
            })

        try:
            # 检查是否已安装（先找文件，再查服务）
            installed = bool(exec_ssh_cmd(ssh, "where sshd.exe 2>nul"))
            if not installed:
                installed = bool(exec_ssh_cmd(ssh, "sc query sshd 2>nul | findstr /C:\"RUNNING\" /C:\"STOPPED\""))

            # 检查是否运行中
            running = bool(exec_ssh_cmd(ssh, "sc query sshd | findstr /C:\"RUNNING\" 2>nul"))

            logger.info(f"[SSHD Check] {device_host}: installed={installed}, running={running}")

            ssh.close()
            return JSONResponse(content={
                'success': True,
                'installed': installed,
                'running': running,
                'install_guide': SSHD_INSTALL_GUIDE if not installed else None
            })

        except Exception as e:
            ssh.close()
            raise

    except Exception as e:
        logger.error(f"[SSHD Check] Error: {e}")
        return JSONResponse(
            content={"success": False, "error": str(e), "installed": False, "running": False},
            status_code=500
        )

@app.post("/api/ssh/sshd-install")
async def install_ssh_sshd():
    """获取SSHD安装说明

    SSHD需要在Windows客户端上手动安装,返回安装指南供用户参考。
    """
    return JSONResponse(content={
        'success': False,
        'error': 'SSHD 需要在 Windows 客户端上手动安装',
        'install_guide': SSHD_INSTALL_GUIDE,
        'manual_install': True
    })


@app.get("/api/ssh/route")
async def check_ssh_route(request: Request):
    """检查网络路由 - 检查测试主机和设备主机是否在同一网段"""
    try:
        config = config_manager.load_config()

        ubuntu_host = config.get("ubuntu_host", "")
        client_ip = get_client_ip_from_request_headers(request)

        if not ubuntu_host or client_ip == 'unknown':
            return JSONResponse(content={
                'success': False,
                'error': '无法获取主机IP地址'
            }, status_code=400)

        ubuntu_ip = extract_ip_from_host(ubuntu_host)
        device_ip = extract_ip_from_host(client_ip)

        same_network = are_same_network(ubuntu_ip, device_ip)
        need_route = not same_network

        if need_route:
            try:
                ubuntu_network_obj = ipaddress.IPv4Network(f"{ubuntu_ip}/24", strict=False)
                device_network_obj = ipaddress.IPv4Network(f"{device_ip}/24", strict=False)
                ubuntu_network = str(ubuntu_network_obj.network_address)
                device_network = str(device_network_obj.network_address)
            except (ipaddress.AddressValueError, ValueError):
                ubuntu_network = '.'.join(ubuntu_ip.split('.')[:3]) + '.0'
                device_network = '.'.join(device_ip.split('.')[:3]) + '.0'

            route_commands = {
                'windows': [
                    f"route add {ubuntu_network} mask 255.255.255.0 {device_ip}",
                    f"route add {device_network} mask 255.255.255.0 {ubuntu_ip}",
                    "# 检查路由表: route print",
                    f"# 删除路由表: route delete {ubuntu_network}",
                    f"# 删除路由表: route delete {device_network}"
                ],
                'linux': [
                    f"sudo ip route add {ubuntu_network}/24 via {device_ip}",
                    f"sudo ip route add {device_network}/24 via {ubuntu_ip}",
                    "# 检查路由表: ip route show",
                    f"# 删除路由表: sudo ip route del {ubuntu_network}/24",
                    f"# 删除路由表: sudo ip route del {device_network}/24"
                ]
            }

            return JSONResponse(content={
                'success': True,
                'same_network': False,
                'need_route': True,
                'message': f'⚠️ 网段不同: {ubuntu_ip} (网段: {ubuntu_network}/24) ↔ {device_ip} (网段: {device_network}/24)',
                'ubuntu_ip': ubuntu_ip,
                'device_ip': device_ip,
                'ubuntu_network': ubuntu_network,
                'device_network': device_network,
                'route_commands': route_commands,
                'warning': '测试主机和设备主机不在同一网段，可能影响网络通信，建议添加路由表'
            })
        else:
            try:
                ubuntu_network_obj = ipaddress.IPv4Network(f"{ubuntu_ip}/24", strict=False)
                ubuntu_network = str(ubuntu_network_obj.network_address)
            except (ipaddress.AddressValueError, ValueError):
                ubuntu_network = '.'.join(ubuntu_ip.split('.')[:3]) + '.0'

            return JSONResponse(content={
                'success': True,
                'same_network': True,
                'need_route': False,
                'message': f'✅ 网段相同: {ubuntu_ip} ↔ {device_ip}',
                'ubuntu_ip': ubuntu_ip,
                'device_ip': device_ip,
                'network': ubuntu_network
            })

    except Exception as e:
        logger.error(f"Error checking routing: {e}")
        return JSONResponse(
            content={"success": False, "error": str(e)},
            status_code=500
        )


def _validate_ip_address(ip: str) -> bool:
    """验证IPv4地址格式和范围"""
    try:
        ipaddress.IPv4Address(ip)
        return True
    except (ipaddress.AddressValueError, ValueError):
        return False


def _extract_network(ip: str) -> str:
    """从IP地址提取网络地址"""
    try:
        network = ipaddress.IPv4Network(f"{ip}/24", strict=False)
        return str(network.network_address)
    except (ipaddress.AddressValueError, ValueError):
        # Fallback to string manipulation for edge cases
        return '.'.join(ip.split('.')[:3]) + '.0'


def _parse_ping_output(ping_output: str, exit_status: int) -> tuple[bool, str]:
    """解析ping输出，返回(可达性, 延迟)"""
    if exit_status == 0:
        if "0% packet loss" in ping_output:
            # 无丢包
            time_match = _PING_RTT_PATTERN.search(ping_output)
            if time_match:
                return True, f"{time_match.group(1)}ms"
            else:
                avg_match = _PING_AVG_PATTERN.search(ping_output)
                if avg_match:
                    return True, f"{avg_match.group(1)}ms"
                else:
                    return True, '<10ms'
        elif "packet loss" in ping_output:
            # 部分丢包
            loss_match = _PING_LOSS_PATTERN.search(ping_output)
            if loss_match:
                loss_percent = int(loss_match.group(1))
                if loss_percent < 100:
                    return True, f'{loss_percent}% 丢包'
                else:
                    return False, 'N/A (100% 丢包)'
            else:
                return False, 'N/A'
        else:
            return True, 'N/A'
    else:
        # ping失败
        if "100% packet loss" in ping_output or "Network is unreachable" in ping_output:
            return False, 'N/A (不可达)'
        else:
            return False, 'N/A'


def _generate_route_commands(test_network: str, device_network: str, test_host_ip: str) -> dict:
    """生成路由命令

    网络拓扑说明：
    - 测试主机: 172.16.14.233 (运行GMS服务)
    - Android设备: 172.16.21.x (设备网段)
    - 测试主机网关: 172.16.14.1

    路由目的：让测试主机能够访问Android设备网段
    """
    # 推测网关地址（通常是网段的第一个IP）
    test_gateway = '.'.join(test_network.split('.')[:3]) + '.1'

    return {
        'windows': [
            f"# 在测试主机上执行以下命令:",
            f"# 添加到Android设备网段的路由（通过测试主机网关）",
            f"route add {device_network} mask 255.255.255.0 {test_gateway}",
            f"# 检查路由表: route print",
            f"# 删除路由: route delete {device_network}"
        ],
        'linux': [
            f"# 在测试主机上执行以下命令:",
            f"# 添加到Android设备网段的路由（通过测试主机网关）",
            f"sudo ip route add {device_network}/24 via {test_gateway}",
            f"# 检查路由表: ip route show",
            f"# 删除路由: sudo ip route del {device_network}/24"
        ]
    }


@app.post("/api/ssh/route/ping")
async def ping_route_test(request: Request):
    """测试测试主机和客户端的网络连通性"""
    try:
        # 获取请求数据
        data = await request.json()
        test_host_ip = data.get('test_host_ip', '').strip()
        client_ip = data.get('client_ip', '').strip()

        # 验证IP格式
        if not _validate_ip_address(test_host_ip) or not _validate_ip_address(client_ip):
            return JSONResponse(
                content={'success': False, 'error': 'IP地址格式不正确'},
                status_code=400
            )

        # 检查是否在同一网段
        test_network = _extract_network(test_host_ip)
        client_network = _extract_network(client_ip)
        same_network = (test_network == client_network)

        # 尝试真正的ping测试（从测试主机ping客户端）
        reachable = False
        latency = None
        ping_output = ""

        if same_network:
            # 同一网段，理论上可达
            reachable = True
            latency = '<1ms (同一网段)'
        else:
            # 不同网段，需要从测试主机执行ping来验证连通性
            try:
                config = config_manager.load_config()
                ssh = ssh_manager.get_connection(config)
                if ssh:
                    # 从测试主机ping客户端IP
                    ping_cmd = f"ping -c 3 -W 2 {client_ip}"
                    stdin, stdout, stderr = ssh.exec_command(ping_cmd, timeout=10)

                    # 读取ping输出（限制大小防止内存溢出）
                    ping_output = stdout.read(8192).decode('utf-8', errors='ignore')   # 8KB sufficient for ping
                    error_output = stderr.read(2048).decode('utf-8', errors='ignore')   # 2KB sufficient for errors
                    exit_status = stdout.channel.recv_exit_status()

                    ssh_manager.return_connection(ssh)

                    # 解析ping结果
                    reachable, latency = _parse_ping_output(ping_output, exit_status)

                    logger.info(f"Ping test from {test_host_ip} to {client_ip}: reachable={reachable}, latency={latency}")
                    if ping_output:
                        logger.debug(f"Ping output: {ping_output[:200]}")

            except Exception as e:
                logger.warning(f"Ping test failed: {e}")
                reachable = False
                latency = 'N/A'

        # 准备路由命令（检查测试主机是否需要添加路由到Android设备网段）
        route_commands = None
        device_network = '172.16.21.0'
        test_device_different = (test_network != device_network)

        if test_device_different:
            # 测试主机和Android设备不在同一网段，需要添加路由
            route_commands = _generate_route_commands(test_network, device_network, test_host_ip)

        return JSONResponse(content={
            'success': True,
            'reachable': reachable,
            'latency': latency,
            'same_network': same_network,
            'test_host_ip': test_host_ip,
            'client_ip': client_ip,
            'test_network': test_network,
            'client_network': client_network,
            'device_network': device_network,
            'test_device_different': test_device_different,
            'route_commands': route_commands
        })

    except Exception as e:
        logger.error(f"Error in ping route test: {e}")
        return JSONResponse(
            content={'success': False, 'error': str(e)},
            status_code=500
        )

@app.get("/api/vpn/status")
async def get_vpn_status():
    """获取VPN连接状态（多次ping提高可靠性）"""
    try:
        config = config_manager.load_config()
        ssh = ssh_manager.get_connection(config)
        if not ssh:
            return JSONResponse(
                content={"success": False, "error": "SSH连接失败"},
                status_code=500
            )

        try:
            vpn_target = config.get('vpn_target', 'www.google.com')
            if isinstance(vpn_target, list):
                vpn_target = vpn_target[0] if vpn_target else 'www.google.com'

            output, error, code = ssh_manager.execute_command(
                ssh,
                f"ping -c 1 -W 2 {vpn_target} 2>&1",
                timeout=3
            )

            ssh_manager.return_connection(ssh)

            if '1 packets transmitted, 1 received' in output or '1 received' in output or 'bytes from' in output:
                logger.info(f"[VPN Status] {vpn_target}: connected")
                return JSONResponse(content={"success": True, "connected": True})
            else:
                logger.info(f"[VPN Status] {vpn_target}: disconnected")
                return JSONResponse(content={"success": True, "connected": False})

        except Exception as e:
            ssh_manager.return_connection(ssh)
            raise

    except Exception as e:
        logger.error(f"Error getting VPN status: {e}")
        return JSONResponse(
            content={"success": False, "error": str(e)},
            status_code=500
        )

@app.post("/api/vpn/connect")
async def connect_vpn(
    req: Optional[VPNConnectRequest] = Body(default=None)
):
    """连接VPN（使用nmcli）

    请求体完全可选，兼容前端无参数调用
    """
    try:
        config = config_manager.load_config()
        ssh = ssh_manager.get_connection(config)
        if not ssh:
            return JSONResponse(
                content={"success": False, "error": "SSH连接失败"},
                status_code=500
            )

        try:
            # 使用nmcli连接VPN
            vpn_cmd = "sudo nmcli connection up hcq2"
            output, error, code = ssh_manager.execute_command(
                ssh,
                vpn_cmd,
                timeout=20
            )

            await asyncio.sleep(2)

            # 检查连接结果
            if code == 0:
                is_connected = True
                message = 'VPN 连接成功'
            elif 'already active' in (error or ''):
                is_connected = True
                message = 'VPN 已连接'
            elif 'unknown connection' in (error or ''):
                ssh_manager.return_connection(ssh)
                return JSONResponse(
                    content={
                        "success": False,
                        "error": "VPN 连接 hcq2 不存在，请先在 NetworkManager 中配置"
                    },
                    status_code=404
                )
            else:
                is_connected = False
                message = f'VPN 连接失败: {error or output}'

            ssh_manager.return_connection(ssh)
            return JSONResponse(content={
                "success": is_connected,
                "connected": is_connected,
                "message": message,
                "output": (output[:500] if output else '')
            })
        except Exception as e:
            ssh_manager.return_connection(ssh)
            raise

    except Exception as e:
        logger.error(f"Error connecting VPN: {e}")
        return JSONResponse(
            content={"success": False, "error": str(e)},
            status_code=500
        )

@app.post("/api/vpn/disconnect")
async def disconnect_vpn():
    """断开VPN（使用nmcli）"""
    try:
        config = config_manager.load_config()
        ssh = ssh_manager.get_connection(config)
        if not ssh:
            return JSONResponse(
                content={"success": False, "error": "SSH连接失败"},
                status_code=500
            )

        try:
            # 使用nmcli断开VPN
            disconnect_cmd = "sudo nmcli connection down hcq2"
            output, error, code = ssh_manager.execute_command(
                ssh,
                disconnect_cmd,
                timeout=10
            )

            ssh_manager.return_connection(ssh)
            return JSONResponse(content={
                "success": True,
                "message": "VPN 已断开"
            })
        except Exception as e:
            ssh_manager.return_connection(ssh)
            raise

    except Exception as e:
        logger.error(f"Error disconnecting VPN: {e}")
        return JSONResponse(
            content={"success": False, "error": str(e)},
            status_code=500
        )

# ==================== 文件上传 ====================

@app.post("/api/files/upload")
async def upload_file(
    file: UploadFile = File(...),
    path: str = Form("")
):
    """
    文件上传 - 与Flask版本一致，上传到远程服务器

    接收浏览器上传的文件，保存到临时目录，然后通过SFTP上传到远程测试主机
    """
    import tempfile

    try:
        # 检查文件
        if not file or file.filename == '':
            return JSONResponse(
                content={'success': False, 'error': 'No file selected'},
                status_code=400
            )

        config = config_manager.load_config()

        # 创建临时目录
        upload_dir = os.path.join(tempfile.gettempdir(), 'gms_uploads')
        os.makedirs(upload_dir, exist_ok=True)

        # 保存到临时文件
        temp_path = os.path.join(upload_dir, file.filename)
        with open(temp_path, 'wb') as f:
            content = await file.read()
            f.write(content)

        try:
            # 连接远程服务器
            ssh = ssh_manager.get_connection(config)
            if not ssh:
                os.remove(temp_path)
                return JSONResponse(
                    content={'success': False, 'error': 'SSH connection failed'},
                    status_code=500
                )

            # 上传到远程服务器
            remote_path = f"/home/{config['ubuntu_user']}/{file.filename}"
            with ssh.open_sftp() as sftp:
                sftp.put(temp_path, remote_path)
            ssh_manager.return_connection(ssh)

            # 清理临时文件
            os.remove(temp_path)

            return JSONResponse(content={
                'success': True,
                'remote_path': remote_path,
                'message': f'文件已上传到 {remote_path}'
            })
        except Exception as e:
            # 清理临时文件
            if os.path.exists(temp_path):
                os.remove(temp_path)
            if 'ssh' in locals():
                ssh_manager.return_connection(ssh)
            raise e

    except Exception as e:
        logger.error(f"Error uploading file: {e}")
        raise HTTPException(
                status_code=500,
                detail=str(e)
            )

@app.post("/api/files/install")
async def upload_files(files: List[UploadFile] = File(...), file_path: str = Form(None)):
    """
    文件上传并安装 - 支持两种模式
    1. 多文件上传：接收文件对象列表
    2. 本地路径上传：通过file_path参数指定本地文件路径
    """
    try:
        config = config_manager.load_config()

        # 模式1：从本地路径上传（与Flask版本一致）
        if file_path:
            if not file_path or not os.path.exists(file_path):
                return JSONResponse(
                    content={'success': False, 'error': 'No file path provided or file not found'},
                    status_code=400
                )

            # 连接远程服务器
            ssh = ssh_manager.get_connection(config)
            if not ssh:
                return JSONResponse(
                    content={'success': False, 'error': 'SSH connection failed'},
                    status_code=500
                )

            try:
                filename = os.path.basename(file_path)
                remote_path = f"/home/{config['ubuntu_user']}/{filename}"

                # 使用SFTP上传
                with ssh.open_sftp() as sftp:
                    sftp.put(file_path, remote_path)
                ssh_manager.return_connection(ssh)

                return JSONResponse(content={
                    'success': True,
                    'remote_path': remote_path,
                    'message': f'文件已上传到 {remote_path}'
                })
            except Exception as e:
                if 'ssh' in locals():
                    ssh_manager.return_connection(ssh)
                raise e

        # 模式2：多文件上传（保存到本地）
        upload_dir = '/tmp/uploads'
        os.makedirs(upload_dir, exist_ok=True)

        uploaded_files = []
        for file in files:
            file_path = os.path.join(upload_dir, file.filename)
            with open(file_path, 'wb') as f:
                content = await file.read()
                f.write(content)

            uploaded_files.append({
                'filename': file.filename,
                'path': file_path,
                'size': len(content)
            })

        return JSONResponse(content={
            "success": True,
            "files": uploaded_files,
            "count": len(uploaded_files)
        })
    except Exception as e:
        logger.error(f"Error uploading files: {e}")
        raise HTTPException(
                status_code=500,
                detail=str(e)
            )

@app.get("/api/files/progress")
async def get_upload_progress(upload_id: Optional[str] = None):
    """获取上传进度"""
    try:
        # 返回上传进度（这里需要实现实际的进度跟踪）
        return JSONResponse(content={
            "success": True,
            "data": {
                "upload_id": upload_id,
                "progress": 100,
                "status": "completed"
            }
        })
    except Exception as e:
        logger.error(f"Error getting upload progress: {e}")
        raise HTTPException(
                status_code=500,
                detail=f"{str(e)}. 请检查配置和参数是否正确。"
            )

# 向后兼容别名
@app.post("/api/upload/file")
async def upload_file_legacy(file: UploadFile = File(...), path: str = Form("")):
    """文件上传（向后兼容别名）"""
    return await upload_file(file, path)

@app.post("/api/upload")
async def upload_files_legacy(files: List[UploadFile] = File(...), file_path: str = Form(None)):
    """文件上传并安装（向后兼容别名）"""
    return await upload_files(files, file_path)

@app.post("/api/upload/progress")
async def get_upload_progress_legacy(req: dict):
    """获取上传进度（向后兼容别名）"""
    upload_id = req.get('upload_id')
    return await get_upload_progress(upload_id)

# ==================== 固件管理 ====================
@app.post("/api/burn/firmware")
async def burn_firmware(
    request: Request,
    h: Optional[str] = Query(None),
    help: bool = Query(False)
):
    """
    固件烧写 - 支持文件上传

    使用 upgrade_tool 烧写固件到选定的设备
    """
    # 检查是否需要显示帮助（支持 ?h 或 ?help）
    if help:
        help_text = generate_per_api_help_text("POST", "/api/burn/firmware")
        if help_text:
            return PlainTextResponse(
                content=help_text,
                headers={
                    "Content-Type": "text/plain; charset=utf-8",
                    "Cache-Control": "public, max-age=300"
                }
            )

    try:
        # 获取客户端ID
        client_id = get_client_id_from_request(request)

        # 从URL参数获取设备列表（优先，这样可以在文件上传前先锁定）
        devices_param = request.query_params.get('devices')
        if devices_param:
            devices = devices_param.split(',')
            logger.info(f"[Firmware Burn] 设备列表从URL参数获取: {devices}")
        else:
            # 兼容旧版本：从FormData获取
            form = await request.form()
            devices_str = form.get('devices')
            devices = devices_str.split(',') if devices_str else []
            logger.info(f"[Firmware Burn] 设备列表从FormData获取: {devices}")

        # 检查设备
        if not devices:
            return JSONResponse(
                content={'success': False, 'error': 'No devices selected'}
            )

        # 获取用户名并立即锁定设备（在等待FormData之前）
        config = config_manager.load_config()
        username = config.get('client_username', 'unknown')

        # 锁定设备
        locked_devices = []
        failed_devices = []
        for device_id in devices:
            success, message = device_lock_manager.lock_device(device_id, client_id, username)
            if success:
                locked_devices.append(device_id)
            else:
                failed_devices.append({'device_id': device_id, 'error': message})

        logger.info(f"[Device Lock] 锁定完成: 成功 {len(locked_devices)} 台, 失败 {len(failed_devices)} 台")
        logger.info(f"[Device Lock] 锁定的设备: {locked_devices}")

        # 如果有设备锁定失败，释放已锁定的设备并返回错误
        if failed_devices:
            await release_device_locks(client_id, locked_devices, broadcast=False)

            error_msg = "以下设备已被其他用户占用：\n"
            for fail in failed_devices:
                error_msg += f"- {fail['device_id']} ({fail['error']})\n"

            return JSONResponse(
                content={
                    'success': False,
                    'error': error_msg.strip(),
                    'failed_devices': failed_devices
                },
                status_code=409
            )

        # 广播设备锁定状态（立即显示）
        logger.info(f"[Device Lock] 开始广播锁定状态到 {len(global_state.websocket_connections)} 个客户端")
        await broadcast_device_lock_update(locked_devices)
        logger.info(f"[Device Lock] 锁定状态广播完成")

        # 现在才开始等待FormData（此时设备已经锁定并显示）
        form = await request.form()
        firmware_file = form.get('firmware_file')
        firmware_path = form.get('firmware_path', '').strip()

        # 检查固件来源
        if not firmware_file and not firmware_path:
            await release_device_locks(client_id, locked_devices)

            return JSONResponse(
                content={'success': False, 'error': 'Please upload a firmware file or provide a firmware path'}
            )

        ssh = ssh_manager.get_connection(config)
        if not ssh:
            await release_device_locks(client_id, locked_devices)
            return JSONResponse(
                content={'success': False, 'error': 'SSH connection failed'}
            )

        try:
            # 1. 上传 upgrade_tool 到测试主机
            logger.info("[Firmware Burn] Uploading upgrade_tool...")
            local_tool = os.path.join(os.path.dirname(__file__), "tools", "upgrade_tool")
            remote_tool = f"/home/{config['ubuntu_user']}/GMS-Suite/upgrade_tool"

            if not os.path.exists(local_tool):
                logger.error(f"[Firmware Burn] upgrade_tool not found: {local_tool}")
                ssh_manager.return_connection(ssh)
                return JSONResponse(
                    content={'success': False, 'error': f'upgrade_tool not found: {local_tool}'}
                )

            # 使用 SCP 上传 upgrade_tool
            import scp
            scp_client = scp.SCPClient(ssh.get_transport())
            scp_client.put(local_tool, remote_tool)
            scp_client.close()
            logger.info("[Firmware Burn] upgrade_tool uploaded successfully")

            # 2. 处理固件文件
            import tempfile

            if firmware_file:
                # 用户上传了文件 - 直接在内存中处理，不写磁盘
                logger.info(f"[Firmware Burn] Processing uploaded file: {firmware_file.filename}")

                # 使用BytesIO在内存中处理文件，避免写磁盘
                import io
                firmware_content = await firmware_file.read()
                firmware_size = len(firmware_content)
                firmware_bytes = io.BytesIO(firmware_content)

                logger.info(f"[Firmware Burn] File loaded into memory: {firmware_size} bytes")

                # 直接上传到测试主机
                firmware_name = firmware_file.filename
                remote_firmware = f"/home/{config['ubuntu_user']}/GMS-Suite/{firmware_name}"

                logger.info(f"[Firmware Burn] Directly uploading to test host: {remote_firmware}")

                # 用于存储上传进度的全局变量（供回调访问）
                upload_progress_data = {'current_percentage': 0}

                # 自定义SCP进度回调
                def upload_progress(filename, size, sent):
                    percentage = (sent / size) * 100 if size > 0 else 0
                    upload_progress_data['current_percentage'] = percentage
                    logger.info(f"[Firmware Burn] Upload progress: {percentage:.2f}%")

                # 使用线程执行SCP上传
                import threading
                upload_complete = threading.Event()
                upload_error = [None]

                def upload_file_thread():
                    try:
                        # 使用SCP的putfo方法直接从内存上传
                        scp_client = scp.SCPClient(ssh.get_transport(), progress=upload_progress)
                        scp_client.putfo(firmware_bytes, remote_firmware)
                        scp_client.close()
                        logger.info(f"[Firmware Burn] Firmware uploaded to: {remote_firmware}")
                    except Exception as e:
                        logger.error(f"[Firmware Burn] Upload error: {e}")
                        upload_error[0] = str(e)
                    finally:
                        upload_complete.set()

                # 发送上传开始消息
                if client_id in global_state.websocket_connections:
                    try:
                        await global_state.websocket_connections[client_id].send_json({
                            'type': 'file_upload_progress',
                            'filename': firmware_name,
                            'percentage': 0,
                            'total_size': firmware_size,
                            'uploaded_size': 0
                        })
                    except (WebSocketDisconnect, ConnectionError):
                        pass  # WebSocket disconnected, continue with upload

                # 启动上传线程
                upload_thread = threading.Thread(target=upload_file_thread)
                upload_thread.start()

                # 定期更新进度到前端
                last_percentage = 0
                while not upload_complete.is_set():
                    await asyncio.sleep(0.5)
                    current_percentage = upload_progress_data.get('current_percentage', 0)

                    # 只有当百分比变化时才发送更新
                    if abs(current_percentage - last_percentage) > 0.1:
                        if client_id in global_state.websocket_connections:
                            try:
                                sent_size = int((current_percentage / 100) * firmware_size)
                                await global_state.websocket_connections[client_id].send_json({
                                    'type': 'file_upload_progress',
                                    'filename': firmware_name,
                                    'percentage': round(current_percentage, 2),
                                    'total_size': firmware_size,
                                    'uploaded_size': sent_size
                                })
                            except (WebSocketDisconnect, ConnectionError, KeyError):
                                pass
                        last_percentage = current_percentage

                # 等待线程完成
                upload_thread.join(timeout=300)  # 5分钟超时

                # 检查上传是否成功
                if upload_error[0]:
                    ssh_manager.return_connection(ssh)
                    return JSONResponse(
                        content={'success': False, 'error': f'Upload failed: {upload_error[0]}'}
                    )

                # 发送上传完成消息
                if client_id in global_state.websocket_connections:
                    try:
                        await global_state.websocket_connections[client_id].send_json({
                            'type': 'file_upload_progress',
                            'filename': firmware_name,
                            'percentage': 100,
                            'total_size': firmware_size,
                            'uploaded_size': firmware_size
                        })
                        await global_state.websocket_connections[client_id].send_json({
                            'type': 'log_update',
                            'log': '✅ 固件文件上传完成',
                            'log_type': 'success'
                        })
                    except (WebSocketDisconnect, ConnectionError, KeyError):
                        pass

                logger.info(f"[Firmware Burn] Firmware uploaded successfully, skipping local file check")

            # 如果没有上传文件，处理其他情况（远程文件或本地文件）
            else:
                # 现在处理固件文件（远程路径或本地文件）
                logger.info(f"[Firmware Burn] Processing firmware: {firmware_path}")
                firmware_name = os.path.basename(firmware_path)
                remote_firmware = f"/home/{config['ubuntu_user']}/GMS-Suite/{firmware_name}"

                # 判断是本地文件还是远程文件
                if os.path.exists(firmware_path):
                    # 本地文件，需要上传
                    file_size = os.path.getsize(firmware_path)
                    logger.info(f"[Firmware Burn] Uploading local file: {firmware_path} ({file_size} bytes)")

                    # 用于存储上传进度的全局变量（供回调访问）
                    upload_progress_data = {'current_percentage': 0}

                    # 自定义SCP进度回调
                    def upload_progress(filename, size, sent):
                        percentage = (sent / size) * 100 if size > 0 else 0
                        upload_progress_data['current_percentage'] = percentage
                        logger.info(f"[Firmware Burn] Upload progress: {percentage:.2f}%")

                    # 使用线程执行SCP上传
                    import threading
                    upload_complete = threading.Event()
                    upload_error = [None]

                    def upload_file_thread():
                        try:
                            scp_client = scp.SCPClient(ssh.get_transport(), progress=upload_progress)
                            scp_client.put(firmware_path, remote_firmware)
                            scp_client.close()
                            logger.info(f"[Firmware Burn] Firmware uploaded to: {remote_firmware}")
                        except Exception as e:
                            logger.error(f"[Firmware Burn] Upload error: {e}")
                            upload_error[0] = str(e)
                        finally:
                            upload_complete.set()

                    # 发送上传开始消息
                    if client_id in global_state.websocket_connections:
                        try:
                            await global_state.websocket_connections[client_id].send_json({
                                'type': 'file_upload_progress',
                                'filename': firmware_name,
                                'percentage': 0,
                                'total_size': file_size,
                                'uploaded_size': 0
                            })
                        except (WebSocketDisconnect, ConnectionError, KeyError):
                            pass

                    # 启动上传线程
                    upload_thread = threading.Thread(target=upload_file_thread)
                    upload_thread.start()

                    # 定期更新进度到前端
                    last_percentage = 0
                    while not upload_complete.is_set():
                        await asyncio.sleep(0.5)
                        current_percentage = upload_progress_data.get('current_percentage', 0)

                        # 只有当百分比变化时才发送更新
                        if abs(current_percentage - last_percentage) > 0.1:
                            if client_id in global_state.websocket_connections:
                                try:
                                    sent_size = int((current_percentage / 100) * file_size)
                                    await global_state.websocket_connections[client_id].send_json({
                                        'type': 'file_upload_progress',
                                        'filename': firmware_name,
                                        'percentage': round(current_percentage, 2),
                                        'total_size': file_size,
                                        'uploaded_size': sent_size
                                    })
                                except (WebSocketDisconnect, ConnectionError, KeyError):
                                    pass
                            last_percentage = current_percentage

                    # 等待线程完成
                    upload_thread.join(timeout=300)

                    # 检查上传是否成功
                    if upload_error[0]:
                        ssh_manager.return_connection(ssh)
                        return JSONResponse(
                            content={'success': False, 'error': f'Upload failed: {upload_error[0]}'}
                        )

                    # 发送上传完成消息
                    if client_id in global_state.websocket_connections:
                        try:
                            await global_state.websocket_connections[client_id].send_json({
                                'type': 'file_upload_progress',
                                'filename': firmware_name,
                                'percentage': 100,
                                'total_size': file_size,
                                'uploaded_size': file_size
                            })
                            await global_state.websocket_connections[client_id].send_json({
                                'type': 'log_update',
                                'log': '✅ 固件文件上传完成',
                                'log_type': 'success'
                            })
                        except (WebSocketDisconnect, ConnectionError, KeyError):
                            pass

                    logger.info(f"[Firmware Burn] Firmware uploaded to: {remote_firmware}")
                elif firmware_path.startswith('/') or firmware_path.startswith('./'):
                    # 远程文件路径
                    logger.info(f"[Firmware Burn] Using remote file: {firmware_path}")
                    remote_firmware = firmware_path
                else:
                    # 可能只是文件名，尝试在 GMS-Suite 目录中查找
                    logger.info(f"[Firmware Burn] Searching for file in GMS-Suite: {firmware_path}")
                    check_cmd = f"ls /home/{config['ubuntu_user']}/GMS-Suite/{firmware_path} 2>/dev/null && echo 'found' || echo 'not_found'"
                    output, _, _ = ssh_manager.execute_command(ssh, check_cmd, timeout=5)

                    if 'found' in output:
                        remote_firmware = f"/home/{config['ubuntu_user']}/GMS-Suite/{firmware_path}"
                        logger.info(f"[Firmware Burn] File found: {remote_firmware}")
                    else:
                        ssh_manager.return_connection(ssh)
                        return JSONResponse(
                            content={'success': False, 'error': f'Firmware file not found: {firmware_path}. Please use a full path or upload the file first.'}
                        )

            # 3. 让设备进入 Loader 模式
            logger.info("[Firmware Burn] Entering Loader mode...")
            for device in devices:
                cmd = f"adb -s {device} reboot loader"
                ssh_manager.execute_command(ssh, cmd, timeout=5)
                logger.info(f"[Firmware Burn] Device {device} sent to Loader mode")

            logger.info("[Firmware Burn] Waiting for devices to enter Loader mode...")
            await asyncio.sleep(8)

            # 4. 检查 Loader 设备
            gms_suite_dir = f"/home/{config['ubuntu_user']}/GMS-Suite"
            check_cmd = f"cd {gms_suite_dir} && ./upgrade_tool ld"
            output, _, _ = ssh_manager.execute_command(ssh, check_cmd, timeout=5)

            if "List of rockusb connected" not in output:
                ssh_manager.return_connection(ssh)
                return JSONResponse(
                    content={'success': False, 'error': 'No Loader devices detected'}
                )

            logger.info(f"[Firmware Burn] Loader devices detected:\n{output}")

            # 5. 烧写固件（upgrade_tool 会自动处理所有设备）
            logger.info("[Firmware Burn] Starting firmware burning...")
            burn_cmd = f"cd {gms_suite_dir} && ./upgrade_tool uf {shlex.quote(firmware_name)}"

            # 发送开始消息
            if client_id in global_state.websocket_connections:
                try:
                    await global_state.websocket_connections[client_id].send_json({
                        'type': 'log_update',
                        'log': '🔥 开始烧写固件...',
                        'log_type': 'info'
                    })
                except (WebSocketDisconnect, ConnectionError, KeyError):
                    pass

            # 执行烧写并获取实时输出
            stdin, stdout, stderr = ssh.exec_command(burn_cmd, get_pty=True, timeout=300)

            # 实时读取输出并发送到前端
            import select
            output_buffer = []

            # 进度条状态
            firmware_burn_start = False
            current_progress = 0
            last_progress_time = 0

            while not stdout.channel.exit_status_ready():
                current_time = asyncio.get_event_loop().time()

                # 如果有数据可读，读取并处理
                if stdout.channel.recv_ready():
                    chunk = stdout.channel.recv(1024).decode('utf-8', errors='ignore')
                    output_buffer.append(chunk)

                    # 清理ANSI转义码
                    clean_chunk = strip_ansi_codes(chunk)

                    # 检测烧写状态开始
                    if 'Download Firmware Start' in clean_chunk and not firmware_burn_start:
                        firmware_burn_start = True
                        current_progress = 0
                        last_progress_time = current_time
                        logger.info("[Firmware Burn] 检测到固件烧写开始，启动进度条")

                    # 发送所有非空输出到前端
                    if client_id in global_state.websocket_connections:
                        try:
                            for line in clean_chunk.split('\n'):
                                line = line.strip()
                                if line:
                                    # 固件烧写期间不显示日志（保持日志区域干净）
                                    # 除非是错误信息
                                    if firmware_burn_start:
                                        # 只显示错误信息
                                        if any(keyword in line.lower() for keyword in ['error', 'failed', 'fail', '错误', '失败']):
                                            await global_state.websocket_connections[client_id].send_json({
                                                'type': 'log_update',
                                                'log': line,
                                                'log_type': 'error'
                                            })
                                        continue

                                    # 其他正常日志
                                    await global_state.websocket_connections[client_id].send_json({
                                        'type': 'log_update',
                                        'log': line,
                                        'log_type': 'info'
                                    })
                        except Exception as e:
                            logger.error(f"[Firmware Burn] 发送日志失败: {e}")

                # 如果固件烧写开始，每0.5秒更新一次进度
                if firmware_burn_start and (current_time - last_progress_time > 0.5):
                    # 进度条从0%到95%，每0.5秒增加5%
                    current_progress = min(current_progress + 5, 95)
                    last_progress_time = current_time

                    # 发送进度更新到前端（只更新进度条，不显示在日志）
                    if client_id in global_state.websocket_connections:
                        try:
                            await global_state.websocket_connections[client_id].send_json({
                                'type': 'firmware_progress',
                                'percentage': current_progress
                            })
                        except Exception as e:
                            logger.error(f"[Firmware Burn] 发送进度失败: {e}")

                # 短暂休眠避免CPU占用过高
                await asyncio.sleep(0.1)

            # 获取最终输出
            final_output = ''.join(output_buffer)
            exit_status = stdout.channel.recv_exit_status()

            if exit_status == 0:
                logger.info(f"[Firmware Burn] Success:\n{final_output}")
                ssh_manager.return_connection(ssh)

                # 发送100%完成进度
                if client_id in global_state.websocket_connections:
                    try:
                        await global_state.websocket_connections[client_id].send_json({
                            'type': 'firmware_progress',
                            'percentage': 100
                        })
                    except (WebSocketDisconnect, ConnectionError, KeyError):
                        pass

                # 发送完成消息
                if client_id in global_state.websocket_connections:
                    try:
                        await global_state.websocket_connections[client_id].send_json({
                            'type': 'log_update',
                            'log': '✅ 固件烧写完成！',
                            'log_type': 'success'
                        })
                    except (WebSocketDisconnect, ConnectionError, KeyError):
                        pass

                # 释放设备锁
                logger.info(f"[Device Lock] 开始解锁设备: {locked_devices}")
                await release_device_locks(client_id, locked_devices)
                logger.info(f"[Device Lock] 设备解锁完成")

                return JSONResponse(
                    content={'success': True, 'message': 'Firmware burn completed successfully'}
                )
            else:
                logger.error(f"[Firmware Burn] Failed with exit code {exit_status}")

                # 从输出缓冲区获取错误信息
                final_output = ''.join(output_buffer)
                error_output = final_output or stderr.read().decode('utf-8', errors='ignore')
                ssh_manager.return_connection(ssh)

                # 发送失败消息（显示详细错误）
                if client_id in global_state.websocket_connections:
                    try:
                        await global_state.websocket_connections[client_id].send_json({
                            'type': 'log_update',
                            'log': f'❌ 固件烧写失败 (exit code: {exit_status})',
                            'log_type': 'error'
                        })
                        # 如果有详细错误信息，也发送
                        if error_output and len(error_output) < 500:  # 限制长度
                            await global_state.websocket_connections[client_id].send_json({
                                'type': 'log_update',
                                'log': f'错误详情: {error_output[:200]}',
                                'log_type': 'error'
                            })
                    except (WebSocketDisconnect, ConnectionError, KeyError):
                        pass

                # 释放设备锁
                logger.info(f"[Device Lock] 开始解锁设备: {locked_devices}")
                await release_device_locks(client_id, locked_devices)
                logger.info(f"[Device Lock] 设备解锁完成")

                return JSONResponse(
                    content={'success': False, 'error': error_output or 'Firmware burn failed'}
                )

        except Exception as e:
            ssh_manager.return_connection(ssh)
            logger.error(f"[Firmware Burn] Error: {e}")

            # 释放设备锁
            await release_device_locks(client_id, locked_devices)

            return JSONResponse(
                content={'success': False, 'error': str(e)}
            )

    except Exception as e:
        logger.error(f"Error in burn_firmware: {e}")
        return JSONResponse(
            content={'success': False, 'error': str(e)},
            status_code=500
        )

@app.post("/api/burn/gsi")
async def burn_gsi(request: Request):
    """
    GSI 烧写 - 按照GUI版本实现

    使用 run_GSI_Burn.sh 脚本烧写GSI镜像到选定的设备
    """
    try:
        # 获取客户端ID
        client_id = get_client_id_from_request(request)

        # 解析请求体
        req_data = await request.json()
        devices = req_data.get('devices', [])
        script_path = req_data.get('script_path', '').strip()
        system_img = req_data.get('system_img', '').strip()
        vendor_img = req_data.get('vendor_img', '').strip()

        # 检查设备
        if not devices:
            return JSONResponse(
                content={'success': False, 'error': 'No devices selected'}
            )

        # 检查脚本路径
        if not script_path:
            return JSONResponse(
                content={'success': False, 'error': 'Script path is required'}
            )

        # 检查system镜像
        if not system_img:
            return JSONResponse(
                content={'success': False, 'error': 'System image path is required'}
            )

        # 获取用户名
        config = config_manager.load_config()
        username = config.get('client_username', 'unknown')

        # 锁定设备
        locked_devices = []
        failed_devices = []
        for device_id in devices:
            success, message = device_lock_manager.lock_device(device_id, client_id, username)
            if success:
                locked_devices.append(device_id)
            else:
                failed_devices.append({'device_id': device_id, 'error': message})

        logger.info(f"[Device Lock] 锁定完成: 成功 {len(locked_devices)} 台, 失败 {len(failed_devices)} 台")
        logger.info(f"[Device Lock] 锁定的设备: {locked_devices}")

        # 如果有设备锁定失败，释放已锁定的设备并返回错误
        if failed_devices:
            await release_device_locks(client_id, locked_devices, broadcast=False)

            error_msg = "以下设备已被其他用户占用：\n"
            for fail in failed_devices:
                error_msg += f"- {fail['device_id']} ({fail['error']})\n"

            return JSONResponse(
                content={
                    'success': False,
                    'error': error_msg.strip(),
                    'failed_devices': failed_devices
                },
                status_code=409
            )

        # 广播设备锁定状态（立即显示）
        logger.info(f"[Device Lock] 开始广播锁定状态到 {len(global_state.websocket_connections)} 个客户端")
        await broadcast_device_lock_update(locked_devices)
        logger.info(f"[Device Lock] 锁定状态广播完成")

        config = config_manager.load_config()
        ssh = ssh_manager.get_connection(config)
        if not ssh:
            await release_device_locks(client_id, locked_devices)
            return JSONResponse(
                content={'success': False, 'error': 'SSH connection failed'}
            )

        try:
            import scp

            # 1. 上传必要文件到测试主机
            logger.info("[GSI Burn] Uploading necessary files...")

            # 上传脚本（从tools目录）
            local_script = os.path.join(os.path.dirname(__file__), "tools", "run_GSI_Burn.sh")
            remote_script = f"/home/{config['ubuntu_user']}/GMS-Suite/run_GSI_Burn.sh"

            if os.path.exists(local_script):
                logger.info(f"[GSI Burn] Uploading script from: {local_script}")
                scp_client = scp.SCPClient(ssh.get_transport())
                scp_client.put(local_script, remote_script)
                scp_client.close()
                # 设置可执行权限
                ssh_manager.execute_command(ssh, f"chmod +x {remote_script}")
                logger.info(f"[GSI Burn] Script uploaded to: {remote_script}")
            else:
                logger.error(f"[GSI Burn] Script not found: {local_script}")
                ssh_manager.return_connection(ssh)
                return JSONResponse(
                    content={'success': False, 'error': f'GSI burn script not found: {local_script}'}
                )

            # 上传 misc.img（从tools目录）
            local_misc = os.path.join(os.path.dirname(__file__), "tools", "misc.img")
            remote_misc = f"/home/{config['ubuntu_user']}/GMS-Suite/misc.img"

            if os.path.exists(local_misc):
                logger.info(f"[GSI Burn] Uploading misc.img from: {local_misc}")
                scp_client = scp.SCPClient(ssh.get_transport())
                scp_client.put(local_misc, remote_misc)
                scp_client.close()
                logger.info(f"[GSI Burn] misc.img uploaded to: {remote_misc}")
            else:
                logger.warning(f"[GSI Burn] misc.img not found: {local_misc}, skipping...")

            # 2. 处理 vendor 镜像（如果提供）
            remote_vendor = ""
            if vendor_img:
                if os.path.exists(vendor_img):
                    # 本地文件，需要上传
                    vendor_name = os.path.basename(vendor_img)
                    remote_vendor = f"/home/{config['ubuntu_user']}/GMS-Suite/{vendor_name}"
                    scp_client = scp.SCPClient(ssh.get_transport())
                    scp_client.put(vendor_img, remote_vendor)
                    scp_client.close()
                else:
                    # 远程文件，直接使用路径
                    remote_vendor = vendor_img

            # 3. 对每个设备执行烧写
            logger.info("[GSI Burn] Starting GSI burning...")
            results = []

            # 发送开始消息
            if client_id in global_state.websocket_connections:
                try:
                    await global_state.websocket_connections[client_id].send_json({
                        'type': 'log_update',
                        'log': f'🔥 开始烧写GSI镜像到 {len(devices)} 台设备...',
                        'log_type': 'info'
                    })
                except (WebSocketDisconnect, ConnectionError, KeyError):
                    pass

            for device in devices:
                # 构建烧写命令
                img_args = f"--system {system_img}"
                if remote_vendor:
                    img_args += f" --vendor {remote_vendor}"

                burn_cmd = f"{remote_script} {device} {img_args}"

                logger.info(f"[GSI Burn] Executing for {device}: {burn_cmd}")

                # 发送设备开始消息
                if client_id in global_state.websocket_connections:
                    try:
                        await global_state.websocket_connections[client_id].send_json({
                            'type': 'log_update',
                            'log': f'📱 正在烧写设备: {device}',
                            'log_type': 'info'
                        })
                    except (WebSocketDisconnect, ConnectionError, KeyError):
                        pass

                # 执行命令并实时读取输出
                stdin, stdout, stderr = ssh.exec_command(burn_cmd, get_pty=True, timeout=600)

                # 实时读取输出并发送到前端
                output_buffer = []

                while not stdout.channel.exit_status_ready():
                    if stdout.channel.recv_ready():
                        chunk = stdout.channel.recv(1024).decode('utf-8', errors='ignore')
                        output_buffer.append(chunk)

                        # 清理ANSI转义码
                        clean_chunk = strip_ansi_codes(chunk)

                        # 发送进度更新到前端（逐行显示）
                        if client_id in global_state.websocket_connections:
                            try:
                                for line in clean_chunk.split('\n'):
                                    line = line.strip()
                                    if line and len(line) > 0:
                                        await global_state.websocket_connections[client_id].send_json({
                                            'type': 'log_update',
                                            'log': line,
                                            'log_type': 'info'
                                        })
                            except (WebSocketDisconnect, ConnectionError, KeyError):
                                pass
                    else:
                        await asyncio.sleep(0.5)

                # 获取最终输出
                final_output = ''.join(output_buffer)
                exit_status = stdout.channel.recv_exit_status()

                # 读取stderr
                error_output = ''
                if stderr.channel.recv_ready():
                    error_output = stderr.read().decode('utf-8', errors='ignore')

                logger.info(f"[GSI Burn] Device {device} - Exit code: {exit_status}")
                logger.info(f"[GSI Burn] Device {device} - Output: {final_output[:500] if final_output else 'Empty'}")
                if error_output:
                    logger.error(f"[GSI Burn] Device {device} - Error: {error_output[:500]}")

                if exit_status == 0:
                    logger.info(f"[GSI Burn] Success for {device}")
                    results.append({
                        'device': device,
                        'success': True,
                        'output': final_output
                    })

                    # 发送成功消息
                    if client_id in global_state.websocket_connections:
                        try:
                            await global_state.websocket_connections[client_id].send_json({
                                'type': 'log_update',
                                'log': f'✅ 设备 {device} GSI烧写完成',
                                'log_type': 'success'
                            })
                        except (WebSocketDisconnect, ConnectionError, KeyError):
                            pass
                else:
                    logger.error(f"[GSI Burn] Failed for {device}: {error_output}")
                    results.append({
                        'device': device,
                        'success': False,
                        'error': error_output,
                        'output': final_output
                    })

                    # 发送失败消息（显示详细错误）
                    if client_id in global_state.websocket_connections:
                        try:
                            error_msg = error_output or "未知错误"
                            # 如果输出中有错误信息，显示最后几行
                            if final_output:
                                lines = final_output.strip().split('\n')
                                if len(lines) > 0:
                                    last_lines = lines[-3:]  # 取最后3行
                                    error_detail = ' '.join(last_lines)
                                    if error_detail and len(error_detail) < 200:
                                        error_msg = error_detail

                            await global_state.websocket_connections[client_id].send_json({
                                'type': 'log_update',
                                'log': f'❌ 设备 {device} GSI烧写失败: {error_msg}',
                                'log_type': 'error'
                            })
                        except (WebSocketDisconnect, ConnectionError, KeyError):
                            pass

            ssh_manager.return_connection(ssh)

            # 释放所有设备锁
            await release_device_locks(client_id, locked_devices)

            # 检查是否全部成功
            all_success = all(r['success'] for r in results)
            if all_success:
                return JSONResponse(
                    content={'success': True, 'message': 'GSI burn completed successfully', 'results': results}
                )
            else:
                return JSONResponse(
                    content={'success': False, 'error': 'Some devices failed', 'results': results}
                )

        except Exception as e:
            ssh_manager.return_connection(ssh)
            logger.error(f"[GSI Burn] Error: {e}")

            # 释放所有设备锁
            await release_device_locks(client_id, locked_devices)

            return JSONResponse(
                content={'success': False, 'error': str(e)}
            )

    except Exception as e:
        logger.error(f"Error in burn_gsi: {e}")
        return JSONResponse(
            content={'success': False, 'error': str(e)},
            status_code=500
        )

@app.post("/api/burn/serial")
async def burn_sn(req: SNBurnRequest):
    """
    SN烧录 - 与Flask版本一致

    烧写序列号到选定的设备
    注意：SN烧写通常需要在loader模式下使用upgrade_tool
    当前实现是占位符，需要特定工具支持
    """
    try:
        devices = req.devices
        sn_code = req.sn_code

        # 检查设备
        if not devices:
            return JSONResponse(
                content={'success': False, 'error': 'No devices selected'},
                status_code=400
            )

        # 检查SN码
        if not sn_code:
            return JSONResponse(
                content={'success': False, 'error': 'SN code is required'},
                status_code=400
            )

        config = config_manager.load_config()
        ssh = ssh_manager.get_connection(config)
        if not ssh:
            return JSONResponse(
                content={'success': False, 'error': 'SSH connection failed'},
                status_code=500
            )

        try:
            results = []

            for device_id in devices:
                # SN烧写通常需要在loader模式下使用upgrade_tool
                # 当前实现是占位符
                results.append({
                    'device': device_id,
                    'success': False,
                    'error': 'SN burning requires device in loader mode. This feature needs to be implemented with specific tool support.'
                })

            ssh_manager.return_connection(ssh)
            return JSONResponse(content={'success': True, 'results': results})
        except Exception as e:
            ssh_manager.return_connection(ssh)
            raise e
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error burning SN: {e}")
        raise HTTPException(
                status_code=500,
                detail=str(e)
            )

# ==================== 其他功能 ====================

@app.post("/api/files/list")
async def list_files(req: dict):
    """文件列表 - 通过SSH连接到远程主机"""
    try:
        path = req.get('path', '')
        config = config_manager.load_config()

        if not path:
            # Default to user home directory
            path = f"/home/{config.get('ubuntu_user', 'hcq')}"

        ssh = ssh_manager.get_connection(config)
        if not ssh:
            return JSONResponse(content={'success': False, 'error': 'SSH connection failed'}, status_code=500)

        try:
            # Check if path exists
            check_cmd = f"test -e '{path}' && echo 'exists' || echo 'not_found'"
            output, _, _ = ssh_manager.execute_command(ssh, check_cmd)

            if 'not_found' in output:
                ssh_manager.return_connection(ssh)
                return JSONResponse(content={'success': False, 'error': f'Path not found: {path}'}, status_code=404)

            # List files with details (name, type, size, modified time)
            # Using ls -la to get detailed information
            list_cmd = f"ls -la '{path}' 2>/dev/null || echo 'ERROR'"
            output, error, code = ssh_manager.execute_command(ssh, list_cmd)

            if 'ERROR' in output or code != 0:
                ssh_manager.return_connection(ssh)
                return JSONResponse(content={'success': False, 'error': 'Failed to list directory'}, status_code=500)

            files = []
            for line in output.split('\n'):
                if line.startswith('total') or not line.strip():
                    continue

                # Parse ls -la output
                parts = line.split()
                if len(parts) >= 9:
                    permissions = parts[0]
                    name = ' '.join(parts[8:])
                    is_dir = permissions.startswith('d')
                    size = parts[4] if not is_dir else '0'

                    # Skip . and ..
                    if name in ['.', '..']:
                        continue

                    files.append({
                        'name': name,
                        'type': 'directory' if is_dir else 'file',
                        'size': int(size),
                        'permissions': permissions
                    })

            # Sort: directories first, then files, alphabetically
            files.sort(key=lambda x: (x['type'] != 'directory', x['name'].lower()))

            ssh_manager.return_connection(ssh)
            return JSONResponse(content={
                'success': True,
                'path': path,
                'files': files
            })
        except Exception as e:
            ssh_manager.return_connection(ssh)
            raise
    except Exception as e:
        logger.error(f"Error listing files: {e}")
        return JSONResponse(
            content={'success': False, 'error': str(e)},
            status_code=500
        )

# ==================== USB/IP辅助函数（与5000端口一致）====================

def is_windows_host(ssh):
    """检查SSH主机是否为Windows"""
    try:
        stdin, stdout, stderr = ssh.exec_command('ver 2>&1', timeout=3)
        output = stdout.read().decode('utf-8', errors='ignore').lower()
        return 'microsoft' in output or 'windows' in output
    except (paramiko.SSHException, AttributeError):
        return False


def find_device_host_password(config, device_host):
    """从 client_ssh_credentials 中查找对应 device_host 的密码"""
    if '@' not in device_host:
        return None

    username, hostname = device_host.split('@', 1)

    # 从 client_ssh_credentials 中查找匹配的凭据
    for cred in config.get('client_ssh_credentials', []):
        if cred.get('username') == username:
            logger.info(f"[USB/IP] Found SSH credential for username={username}")
            return cred.get('password')

    logger.info(f"[USB/IP] No SSH credential found for {device_host}")
    return None


def create_device_ssh_connection(config):
    """创建设备主机的SSH连接（Windows）"""
    device_host = config.get('device_host', '')
    if not device_host:
        return None

    if '@' not in device_host:
        logger.error("[USB/IP] Device host format should be user@host")
        return None

    username, hostname = device_host.split('@', 1)
    password = config.get('device_pswd', '')

    if not password:
        logger.error("[USB/IP] No SSH password configured for device host")
        return None

    try:
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(hostname=hostname, username=username, password=password, timeout=10)
        return ssh
    except Exception as e:
        logger.error(f"[USB/IP] Failed to connect to device host: {e}")
        return None

# ==================== WebSocket ====================

@app.websocket("/api/system/websocket/{client_id}")
async def websocket_endpoint(websocket: WebSocket, client_id: str):
    """WebSocket连接端点"""
    await websocket.accept()
    with global_state.websocket_connections_lock:
        global_state.websocket_connections[client_id] = websocket
    logger.info(f"WebSocket client connected: {client_id}")

    try:
        while True:
            # 接收消息
            data = await websocket.receive_json()
            message_type = data.get('type')

            if message_type == 'ping':
                await websocket.send_json({
                    'type': 'pong',
                    'timestamp': datetime.now().isoformat()
                })

            elif message_type == 'refresh_devices':
                await refresh_devices_websocket(client_id, websocket)

            elif message_type == 'terminal_connect':
                await handle_terminal_connect(client_id, websocket, data)

            elif message_type == 'terminal_input':
                await handle_terminal_input(client_id, websocket, data)

            elif message_type == 'terminal_resize':
                await handle_terminal_resize(client_id, websocket, data)

    except WebSocketDisconnect:
        logger.info(f"WebSocket client disconnected: {client_id}")
    except Exception as e:
        logger.error(f"WebSocket error for {client_id}: {e}")
    finally:
        # 清理WebSocket连接
        with global_state.websocket_connections_lock:
            if client_id in global_state.websocket_connections:
                del global_state.websocket_connections[client_id]

        # 清理终端SSH会话（如果存在）
        with global_state.terminal_lock:
            if client_id in global_state.terminal_ssh_sessions:
                session_info = global_state.terminal_ssh_sessions[client_id]
                try:
                    # 只有SSH模式才关闭SSH连接,ADB模式使用的是共享连接
                    if session_info.get('mode') != 'adb':
                        session_info['ssh'].close()
                        logger.info(f"[TERMINAL] Closed SSH connection for {client_id}")
                    else:
                        # ADB模式:只关闭channel,不关闭SSH连接
                        try:
                            session_info['channel'].close()
                            logger.info(f"[TERMINAL] Closed ADB channel for {client_id}")
                        except (WebSocketDisconnect, ConnectionError, KeyError):
                            pass
                        # 归还SSH连接到连接池
                        ssh_manager.return_connection(session_info['ssh'])
                except Exception as e:
                    logger.error(f"[TERMINAL] Error closing session for {client_id}: {e}")
                del global_state.terminal_ssh_sessions[client_id]

async def refresh_devices_websocket(client_id: str, websocket: WebSocket):
    """WebSocket刷新设备列表"""
    try:
        config = config_manager.load_config()
        ssh = ssh_manager.get_connection(config)

        if ssh:
            try:
                stdout, stderr, code = ssh_manager.execute_command(
                    ssh, "adb devices", timeout=5
                )
                if code == 0:
                    lines = stdout.strip().split('\n')[1:]
                    devices_info = []
                    for line in lines:
                        if line.strip():
                            parts = line.split('\t')
                            if len(parts) >= 2:
                                device_id = parts[0]
                                status = parts[1]
                                device_data = {
                                    'id': device_id,
                                    'status': status
                                }

                                # 添加锁定状态
                                lock_status = device_lock_manager.get_lock_status(device_id)
                                if lock_status:
                                    device_data['locked'] = True
                                    device_data['locked_by'] = lock_status['locked_by']

                                devices_info.append(device_data)

                    await websocket.send_json({
                        'type': 'devices_updated',
                        'devices': devices_info
                    })
            except Exception as e:
                logger.error(f"Error refreshing devices: {e}")
            finally:
                ssh_manager.return_connection(ssh)
    except Exception as e:
        logger.error(f"Error in refresh_devices_websocket: {e}")
        await websocket.send_json({
            'type': 'error',
            'message': str(e)
        })

async def handle_adb_shell_connect(client_id: str, websocket: WebSocket, serial_no: str, config: dict):
    """处理ADB Shell连接 - 通过SSH执行adb shell命令"""
    try:
        # 先连接到SSH
        ssh = ssh_manager.get_connection(config)
        if not ssh:
            await websocket.send_json({
                'type': 'terminal_error',
                'error': 'SSH连接失败'
            })
            return

        # 创建shell通道
        channel = ssh.invoke_shell(term='xterm-256color')
        channel.setblocking(0)

        # 设置初始终端大小
        channel.resize_pty(width=120, height=30)

        # 发送清屏和adb shell命令
        # 使用多个换行和clear命令来清除banner
        channel.send('\n\n\n')  # 发送换行跳过banner
        channel.send('clear\n')  # 清屏
        channel.send(f'adb -s {serial_no} shell\n')  # 执行 adb shell

        # 保存会话
        loop = asyncio.get_event_loop()
        session_id = client_id

        with global_state.terminal_lock:
            # 关闭旧连接（如果存在）
            if session_id in global_state.terminal_ssh_sessions:
                try:
                    old_session = global_state.terminal_ssh_sessions[session_id]
                    # 只有SSH模式才关闭SSH连接
                    if old_session.get('mode') != 'adb':
                        old_session['ssh'].close()
                    else:
                        # ADB模式:只关闭channel
                        try:
                            old_session['channel'].close()
                        except (WebSocketDisconnect, ConnectionError, KeyError):
                            pass
                        ssh_manager.return_connection(old_session['ssh'])
                except (WebSocketDisconnect, ConnectionError, KeyError):
                    pass

            global_state.terminal_ssh_sessions[session_id] = {
                'ssh': ssh,  # 这里保存的是SSH连接对象
                'channel': channel,
                'host': config.get('ubuntu_host'),
                'user': config.get('ubuntu_user'),
                'mode': 'adb',
                'serial_no': serial_no,
                'connected_at': time.time(),
                'websocket': websocket,
                'event_loop': loop
            }

        logger.info(f"[TERMINAL] ADB Shell session created for device {serial_no}")
        await websocket.send_json({
            'type': 'terminal_connected',
            'mode': 'adb',
            'serial_no': serial_no
        })

        # 启动后台读取线程
        def read_output():
            """后台线程持续读取终端输出"""
            try:
                while True:
                    # 检查会话是否仍然存在
                    if session_id not in global_state.terminal_ssh_sessions:
                        logger.info(f"[TERMINAL] ADB Session {session_id} no longer exists")
                        break

                    try:
                        # 读取数据
                        data_chunk = global_state.terminal_ssh_sessions[session_id]['channel'].recv(1024)
                        if not data_chunk:
                            logger.info(f"[TERMINAL] No data received, ADB connection closed")
                            break

                        # 解码并发送
                        try:
                            text = data_chunk.decode('utf-8')
                        except UnicodeDecodeError:
                            text = data_chunk.decode('utf-8', errors='ignore')

                        # 使用保存的事件循环通过WebSocket发送
                        try:
                            future = asyncio.run_coroutine_threadsafe(
                                websocket.send_json({
                                    'type': 'terminal_data',
                                    'data': text
                                }),
                                loop
                            )
                            # 等待发送完成
                            future.result(timeout=5)
                        except Exception as e:
                            logger.error(f"[TERMINAL] Error sending ADB data: {e}")
                            break

                    except socket.timeout:
                        continue
                    except Exception as e:
                        logger.error(f"[TERMINAL] ADB read error: {e}")
                        break

                    time.sleep(0.01)  # 防止CPU占用过高（线程函数中使用time.sleep）

            except Exception as e:
                logger.error(f"[TERMINAL] ADB read thread error: {e}")
            finally:
                # 清理连接
                logger.info(f"[TERMINAL] ADB read thread exiting for {session_id}")

        # 在后台线程中启动读取
        thread = threading.Thread(target=read_output, daemon=True)
        thread.start()

        logger.info(f"[TERMINAL] ADB Shell connected for device {serial_no}")

    except Exception as e:
        logger.error(f"[TERMINAL] ADB Shell connection error: {e}")
        await websocket.send_json({
            'type': 'terminal_error',
            'error': f'ADB Shell连接失败: {str(e)}'
        })

async def handle_terminal_connect(client_id: str, websocket: WebSocket, data: dict):
    """处理终端SSH连接"""
    try:
        config = config_manager.load_config()
        host = data.get('host', config.get('ubuntu_host'))
        user = data.get('user', config.get('ubuntu_user'))
        password = data.get('password', config.get('ubuntu_pswd', ''))
        mode = data.get('mode', 'ssh')  # 'ssh' 或 'adb'
        serial_no = data.get('serial_no', '')

        # 使用client_id作为会话ID（每个WebSocket连接独立）
        session_id = client_id

        # ADB Shell 模式
        if mode == 'adb':
            logger.info(f"[TERMINAL] ADB Shell connection request for device {serial_no}")
            await handle_adb_shell_connect(client_id, websocket, serial_no, config)
            return

        logger.info(f"[TERMINAL] SSH Connection request from {session_id} to {user}@{host}")

        # 使用 ssh_manager 创建SSH连接
        ssh_config = {
            'hostname': host,
            'username': user,
            'password': password,
            'timeout': 5,
            'use_key_auth': config.get('use_key_auth', False),
            'private_key_path': config.get('private_key_path', '~/.ssh/id_rsa')
        }

        ssh = ssh_manager.create_connection(ssh_config)
        if not ssh:
            error_msg = 'SSH连接失败：请检查用户名、密码或密钥配置'
            await websocket.send_json({
                'type': 'terminal_error',
                'error': error_msg
            })
            return

        # 创建shell通道
        channel = ssh.invoke_shell(term='xterm-256color')
        channel.setblocking(0)

        # 设置初始终端大小
        channel.resize_pty(width=120, height=30)

        # 保存SSH会话和当前事件循环
        loop = asyncio.get_event_loop()
        with global_state.terminal_lock:
            # 关闭旧连接（如果存在）
            if session_id in global_state.terminal_ssh_sessions:
                try:
                    global_state.terminal_ssh_sessions[session_id]['ssh'].close()
                except (WebSocketDisconnect, ConnectionError, KeyError):
                    pass

            global_state.terminal_ssh_sessions[session_id] = {
                'ssh': ssh,
                'channel': channel,
                'host': host,
                'user': user,
                'connected_at': time.time(),
                'websocket': websocket,
                'event_loop': loop  # 保存事件循环引用
            }

        logger.info(f"[TERMINAL] Terminal session created for {session_id}")
        await websocket.send_json({
            'type': 'terminal_connected'
        })

        # 启动后台读取线程
        def read_output():
            """后台线程持续读取终端输出"""
            try:
                while True:
                    # 检查会话是否仍然存在
                    if session_id not in global_state.terminal_ssh_sessions:
                        logger.info(f"[TERMINAL] Session {session_id} no longer exists")
                        break

                    try:
                        # 读取数据
                        data_chunk = global_state.terminal_ssh_sessions[session_id]['channel'].recv(1024)
                        if not data_chunk:
                            logger.info(f"[TERMINAL] No data received, connection closed")
                            break

                        # 解码并发送
                        try:
                            text = data_chunk.decode('utf-8')
                        except UnicodeDecodeError:
                            text = data_chunk.decode('utf-8', errors='ignore')

                        # 使用保存的事件循环通过WebSocket发送
                        try:
                            future = asyncio.run_coroutine_threadsafe(
                                websocket.send_json({
                                    'type': 'terminal_data',
                                    'data': text
                                }),
                                loop
                            )
                            # 等待发送完成
                            future.result(timeout=5)
                        except Exception as e:
                            logger.error(f"[TERMINAL] Error sending data: {e}")
                            break

                    except socket.timeout:
                        continue
                    except Exception as e:
                        logger.error(f"[TERMINAL] Read error: {e}")
                        break

                    import time
                    time.sleep(0.01)  # 防止CPU占用过高（线程函数中）

            except Exception as e:
                logger.error(f"[TERMINAL] Read thread error: {e}")
            finally:
                # 清理连接
                with global_state.terminal_lock:
                    if session_id in global_state.terminal_ssh_sessions:
                        try:
                            global_state.terminal_ssh_sessions[session_id]['ssh'].close()
                        except (WebSocketDisconnect, ConnectionError, KeyError):
                            pass
                        del global_state.terminal_ssh_sessions[session_id]
                        logger.info(f"[TERMINAL] Cleaned up session {session_id}")

                # 通知客户端断开连接
                try:
                    asyncio.run_coroutine_threadsafe(
                        websocket.send_json({
                            'type': 'terminal_error',
                            'error': '连接已断开'
                        }),
                        loop
                    )
                except (WebSocketDisconnect, ConnectionError, KeyError):
                    pass

        # 启动读取线程
        thread = threading.Thread(target=read_output, daemon=True, name=f"terminal_read_{session_id}")
        thread.start()

    except paramiko.AuthenticationException:
        await websocket.send_json({
            'type': 'terminal_error',
            'error': 'SSH认证失败：用户名或密码错误'
        })
    except paramiko.SSHException as e:
        await websocket.send_json({
            'type': 'terminal_error',
            'error': f'SSH连接错误：{str(e)}'
        })
    except Exception as e:
        logger.error(f"[TERMINAL] Connection error: {e}")
        await websocket.send_json({
            'type': 'terminal_error',
            'error': f'连接失败：{str(e)}'
        })

async def handle_terminal_input(client_id: str, websocket: WebSocket, data: dict):
    """处理终端输入"""
    session_id = client_id

    with global_state.terminal_lock:
        if session_id in global_state.terminal_ssh_sessions:
            try:
                input_data = data.get('input', data.get('data', ''))
                global_state.terminal_ssh_sessions[session_id]['channel'].send(input_data)
            except Exception as e:
                logger.error(f"[TERMINAL] Input error for {session_id}: {e}")
                await websocket.send_json({
                    'type': 'terminal_error',
                    'error': f'发送数据失败：{str(e)}'
                })

async def handle_terminal_resize(client_id: str, websocket: WebSocket, data: dict):
    """处理终端大小调整"""
    session_id = client_id

    with global_state.terminal_lock:
        if session_id in global_state.terminal_ssh_sessions:
            try:
                cols = data.get('cols', 120)
                rows = data.get('rows', 30)
                global_state.terminal_ssh_sessions[session_id]['channel'].resize_pty(width=cols, height=rows)
                logger.info(f"[TERMINAL] Terminal resized for session {session_id}: {cols}x{rows}")
            except Exception as e:
                logger.error(f"[TERMINAL] Resize error for session {session_id}: {e}")

# ==================== API文档 ====================

# 预定义API文档列表（模块级常量，只初始化一次）
API_DOCS_LIST = [
    # ==================== 基础接口 ====================
    {
        "method": "GET",
        "path": "/",
        "description": "获取首页（Web界面）",
        "params": []
    },
    {
        "method": "GET",
        "path": "/api/system/health",
        "description": "系统管理",
        "params": [],
        "category": "health"
    },

    # ==================== 配置管理 ====================
    {
        "method": "GET",
        "path": "/api/config/validate",
        "description": "验证配置文件正确性（检查必要字段和路径）",
        "params": [],
        "category": "config"
    },
    {
        "method": "GET",
        "path": "/api/config/values",
        "description": "获取前端配置（仅返回前端需要的字段，不含敏感信息）",
        "params": [],
        "category": "config"
    },
    {
        "method": "GET",
        "path": "/api/config/read",
        "description": "获取完整配置（读取当前系统配置）",
        "params": [],
        "category": "config"
    },
    {
        "method": "POST",
        "path": "/api/config/update",
        "description": "更新配置（修改动态配置字段，保存在config_dynamic.json）",
        "params": [
            {"name": "ubuntu_user", "type": "string", "required": False, "desc": "Ubuntu用户名"},
            {"name": "ubuntu_host", "type": "string", "required": False, "desc": "Ubuntu主机地址"},
            {"name": "ubuntu_pswd", "type": "string", "required": False, "desc": "Ubuntu密码"},
            {"name": "device_host", "type": "string", "required": False, "desc": "设备主机地址"},
            {"name": "device_pswd", "type": "string", "required": False, "desc": "设备密码"},
            {"name": "local_server", "type": "string", "required": False, "desc": "本地服务器地址"},
            {"name": "suites_path", "type": "string", "required": False, "desc": "测试套件路径"},
            {"name": "usbip_vid_pid", "type": "string", "required": False, "desc": "USB/IP的VID:PID"}
        ],
        "category": "config"
    },

    # ==================== 用户管理 ====================
    {
        "method": "GET",
        "path": "/api/users/current",
        "description": "获取客户端IP信息",
        "params": [],
        "category": "users"
    },
    {
        "method": "POST",
        "path": "/api/users/detect",
        "description": "自动检测客户端用户名（通过SSH）",
        "params": [{"name": "ip", "type": "string", "required": False}, {"name": "username", "type": "string", "required": False}, {"name": "password", "type": "string", "required": False}],
        "category": "users"
    },
    {
        "method": "POST",
        "path": "/api/users/set-username",
        "description": "手动设置客户端用户名（无需SSH密码）",
        "params": [{"name": "username", "type": "string", "required": True}],
        "category": "users"
    },
    {
        "method": "GET",
        "path": "/api/users/list",
        "description": "获取所有在线用户列表",
        "params": [],
        "category": "users"
    },

    # ==================== 设备管理 ====================
    {
        "method": "GET",
        "path": "/api/devices/list",
        "description": "获取设备列表",
        "params": [],
        "category": "device"
    },
    {
        "method": "POST",
        "path": "/api/devices/bootloader-lock",
        "description": "锁定设备Bootloader(使用run_Device_Lock.sh脚本)",
        "params": [{"name": "devices", "type": "array", "required": True}],
        "category": "device"
    },
    {
        "method": "POST",
        "path": "/api/devices/bootloader-unlock",
        "description": "解锁设备Bootloader",
        "params": [{"name": "devices", "type": "array", "required": True}],
        "category": "device"
    },
    {
        "method": "POST",
        "path": "/api/devices/bootloader-status",
        "description": "检查设备Bootloader锁状态(GREEN=锁定, ORANGE=未锁定)",
        "params": [{"name": "devices", "type": "array", "required": True}],
        "category": "device"
    },
    {
        "method": "GET",
        "path": "/api/devices/user-locked",
        "description": "列出所有用户锁定设备(多用户环境下的设备占用状态)",
        "params": [],
        "category": "device"
    },
    {
        "method": "POST",
        "path": "/api/devices/reboot",
        "description": "重启设备",
        "params": [{"name": "device_id", "type": "string", "required": True}],
        "category": "device"
    },
    {
        "method": "POST",
        "path": "/api/devices/remount",
        "description": "重新挂载设备为读写模式",
        "params": [{"name": "device_id", "type": "string", "required": True}],
        "category": "device"
    },
    {
        "method": "POST",
        "path": "/api/devices/connect-wifi",
        "description": "连接设备WiFi",
        "params": [{"name": "device_id", "type": "string", "required": True}, {"name": "ssid", "type": "string", "required": True}, {"name": "password", "type": "string", "required": True}],
        "category": "device"
    },
    {
        "method": "POST",
        "path": "/api/devices/shell",
        "description": "执行Shell命令",
        "params": [{"name": "device_id", "type": "string", "required": True}, {"name": "command", "type": "string", "required": True}],
        "category": "device"
    },
    {
        "method": "POST",
        "path": "/api/devices/screen",
        "description": "显示设备屏幕",
        "params": [{"name": "devices", "type": "array", "required": True}],
        "category": "screen"
    },
    {
        "method": "POST",
        "path": "/api/terminal/push",
        "description": "终端推送命令",
        "params": [{"name": "command", "type": "string", "required": True}],
        "category": "device"
    },
    {
        "method": "POST",
        "path": "/api/opengrok/search",
        "description": "OpenGrok代码搜索",
        "params": [{"name": "query", "type": "string", "required": True}, {"name": "full", "type": "boolean", "required": False}],
        "category": "device"
    },

    # ==================== 测试执行 ====================
    {
        "method": "POST",
        "path": "/api/test/start",
        "description": "启动测试 ⭐核心接口",
        "params": [{"name": "devices", "type": "array", "required": True}, {"name": "test_type", "type": "string", "required": True}, {"name": "test_module", "type": "string", "required": True}],
        "category": "test"
    },
    {
        "method": "POST",
        "path": "/api/test/stop",
        "description": "停止测试",
        "params": [],
        "category": "test"
    },
    {
        "method": "POST",
        "path": "/api/test/clean",
        "description": "清理测试环境",
        "params": [],
        "category": "test"
    },
    {
        "method": "POST",
        "path": "/api/reports/analyze-source",
        "description": "分析测试源码",
        "params": [{"name": "test_name", "type": "string", "required": True}, {"name": "error_message", "type": "string", "required": False}],
        "category": "reports"
    },
    {
        "method": "GET",
        "path": "/api/test/logs/current",
        "description": "下载当前日志",
        "params": [],
        "category": "test"
    },
    {
        "method": "POST",
        "path": "/api/test/logs/batch",
        "description": "批量下载日志（ZIP压缩包）",
        "params": [{"name": "files", "type": "array", "required": True, "desc": "日志文件路径数组"}],
        "category": "test"
    },
    {
        "method": "POST",
        "path": "/api/test/logs/save-current",
        "description": "保存当前日志",
        "params": [],
        "category": "test"
    },
    {
        "method": "GET",
        "path": "/api/test/logs/list",
        "description": "获取日志列表",
        "params": [],
        "category": "test"
    },
    {
        "method": "GET",
        "path": "/api/test/logs/stream",
        "description": "流式输出测试日志（实时）⭐",
        "params": [],
        "category": "test",
        "usage": "curl -N http://server:5001/api/test/logs/stream",
        "note": "返回纯文本流，适合CLI工具和脚本"
    },

    # ==================== 报告管理 ====================
    {
        "method": "GET",
        "path": "/api/test/status",
        "description": "获取测试状态",
        "params": [],
        "category": "test"
    },
    {
        "method": "GET",
        "path": "/api/reports/list",
        "description": "获取报告列表",
        "params": [],
        "category": "report"
    },
    {
        "method": "GET",
        "path": "/api/reports/files/{report_timestamp}",
        "description": "获取报告文件",
        "params": [{"name": "report_timestamp", "type": "string", "required": True}],
        "category": "report"
    },
    {
        "method": "GET",
        "path": "/api/reports/analyze/{report_timestamp}",
        "description": "分析报告",
        "params": [{"name": "report_timestamp", "type": "string", "required": True}],
        "category": "report"
    },
    {
        "method": "GET",
        "path": "/api/reports/view",
        "description": "查看报告",
        "params": [{"name": "report_timestamp", "type": "string", "required": True}],
        "category": "report"
    },
    {
        "method": "GET",
        "path": "/api/reports/download/{report_timestamp}",
        "description": "下载报告",
        "params": [{"name": "report_timestamp", "type": "string", "required": True}],
        "category": "report"
    },
    {
        "method": "DELETE",
        "path": "/api/reports/delete",
        "description": "删除报告",
        "params": [{"name": "report_timestamp", "type": "string", "required": True}],
        "category": "report"
    },
    {
        "method": "POST",
        "path": "/api/reports/analyze",
        "description": "AI分析报告",
        "params": [{"name": "report_timestamp", "type": "string", "required": True}, {"name": "use_ai", "type": "boolean", "required": False}],
        "category": "report"
    },
    {
        "method": "POST",
        "path": "/api/reports/analyze-ai",
        "description": "AI深度分析",
        "params": [{"name": "report_timestamp", "type": "string", "required": True}],
        "category": "report"
    },

    # ==================== 主机桌面 ====================
    {
        "method": "GET",
        "path": "/api/desktop/vnc/status",
        "description": "查询Ubuntu主机桌面VNC服务状态",
        "params": [],
        "category": "desktop"
    },
    {
        "method": "POST",
        "path": "/api/desktop/vnc/start",
        "description": "启动Ubuntu主机桌面VNC服务",
        "params": [{"name": "host", "type": "string", "required": False}, {"name": "password", "type": "string", "required": False}, {"name": "vnc_password", "type": "string", "required": False}],
        "category": "desktop"
    },
    {
        "method": "POST",
        "path": "/api/desktop/vnc/stop",
        "description": "停止Ubuntu主机桌面VNC服务",
        "params": [],
        "category": "desktop"
    },
    {
        "method": "POST",
        "path": "/api/desktop/validate",
        "description": "验证Ubuntu主机SSH连接并检查VNC服务可用性（host格式：user@ip）",
        "params": [{"name": "host", "type": "string", "required": True}, {"name": "password", "type": "string", "required": False}],
        "category": "desktop"
    },

    # ==================== USB/IP管理 ====================
    {
        "method": "POST",
        "path": "/api/adb-forward/start",
        "description": "启动ADB端口转发",
        "params": [{"name": "device_host", "type": "string", "required": True}, {"name": "device_password", "type": "string", "required": True}],
        "category": "usbip"
    },
    {
        "method": "POST",
        "path": "/api/adb-forward/stop",
        "description": "停止ADB端口转发",
        "params": [{"name": "device_host", "type": "string", "required": True}],
        "category": "usbip"
    },
    {
        "method": "GET",
        "path": "/api/usbip/status",
        "description": "获取USB/IP状态",
        "params": [],
        "category": "usbip"
    },
    {
        "method": "POST",
        "path": "/api/usbip/start",
        "description": "启动USB/IP",
        "params": [{"name": "device_id", "type": "string", "required": True}],
        "category": "usbip"
    },
    {
        "method": "POST",
        "path": "/api/usbip/stop",
        "description": "停止USB/IP",
        "params": [],
        "category": "usbip"
    },
    {
        "method": "POST",
        "path": "/api/usbip/auto-install",
        "description": "自动安装USB/IP",
        "params": [],
        "category": "usbip"
    },

    # ==================== SSH管理 ====================
    {
        "method": "GET",
        "path": "/api/ssh/sshd-check",
        "description": "检查SSHD状态",
        "params": [],
        "category": "ssh"
    },
    {
        "method": "POST",
        "path": "/api/ssh/sshd-install",
        "description": "安装SSHD服务",
        "params": [],
        "usage": "curl -sX POST \"http://server:5001/api/ssh/sshd-install\" | jq -r '.install_guide'",
        "note": "💡 提示：返回JSON包含install_guide字段，使用jq -r查看换行内容",
        "category": "ssh"
    },
    {
        "method": "GET",
        "path": "/api/ssh/route",
        "description": "检查路由状态",
        "params": [],
        "category": "ssh"
    },
    {
        "method": "GET",
        "path": "/api/vpn/status",
        "description": "获取VPN状态",
        "params": [],
        "category": "vpn"
    },
    {
        "method": "POST",
        "path": "/api/vpn/connect",
        "description": "连接VPN（无需参数，使用默认配置）",
        "params": [],
        "category": "vpn"
    },
    {
        "method": "POST",
        "path": "/api/vpn/disconnect",
        "description": "断开VPN",
        "params": [],
        "category": "vpn"
    },

    # ==================== 文件上传 ====================
    {
        "method": "POST",
        "path": "/api/files/upload",
        "description": "上传文件到服务器",
        "params": [{"name": "file", "type": "file", "required": True}, {"name": "path", "type": "string", "required": False, "desc": "目标路径"}],
        "category": "file"
    },
    {
        "method": "POST",
        "path": "/api/files/install",
        "description": "上传APK并安装到设备",
        "params": [{"name": "file", "type": "file", "required": True}, {"name": "device_id", "type": "string", "required": True}],
        "category": "file"
    },
    {
        "method": "GET",
        "path": "/api/files/progress",
        "description": "获取文件上传进度",
        "params": [{"name": "upload_id", "type": "string", "required": False, "desc": "上传任务ID"}],
        "category": "file"
    },

    # ==================== 刷机功能 ====================
    {
        "method": "POST",
        "path": "/api/burn/firmware",
        "description": "刷入固件（上传固件文件并刷入设备）",
        "params": [
            {"name": "firmware_file", "type": "file", "required": True, "desc": "固件文件（.img格式）"},
            {"name": "devices", "type": "string", "required": True, "desc": "设备序列号（多个用逗号分隔）"},
            {"name": "wipe_data", "type": "boolean", "required": False, "desc": "是否清除数据（默认true）"}
        ],
        "usage": "curl -X POST \"http://server:5001/api/burn/firmware\" -F \"firmware_file=@/path/to/firmware.img\" -F \"devices=rk3572cai\" -F \"wipe_data=true\"",
        "note": "⚠️ 危险操作：刷入固件会重启设备并清除数据",
        "category": "burn"
    },
    {
        "method": "POST",
        "path": "/api/burn/gsi",
        "description": "刷入GSI镜像",
        "params": [
            {"name": "gsi_image", "type": "file", "required": True, "desc": "GSI镜像文件（.img格式）"},
            {"name": "devices", "type": "string", "required": True, "desc": "设备序列号（多个用逗号分隔）"},
            {"name": "wipe_data", "type": "boolean", "required": False, "desc": "是否清除数据（默认true）"}
        ],
        "category": "burn"
    },
    {
        "method": "POST",
        "path": "/api/burn/serial",
        "description": "修改序列号",
        "params": [{"name": "device_id", "type": "string", "required": True}, {"name": "new_serial", "type": "string", "required": True}],
        "category": "burn"
    },

    # ==================== 文件管理 ====================
    {
        "method": "POST",
        "path": "/api/files/list",
        "description": "列出文件",
        "params": [{"name": "path", "type": "string", "required": False}],
        "category": "file"
    },

    # ==================== WebSocket ====================
    {
        "method": "WebSocket",
        "path": "/api/system/websocket/{client_id}",
        "description": "WebSocket实时通信",
        "params": [{"name": "client_id", "type": "string", "required": True}],
        "category": "health"
    },

    # ==================== API文档 ====================
    {
        "method": "GET",
        "path": "/api/docs",
        "description": "获取API文档列表",
        "params": [],
        "category": "config"
    }
]

@app.get("/api/docs")
async def get_api_docs():
    """获取所有API文档（使用预定义列表，性能优化）"""
    try:
        # 直接返回预定义的API列表，避免每次请求重新构建
        return JSONResponse(
            content={
                "success": True,
                "apis": API_DOCS_LIST,
                "total": len(API_DOCS_LIST)
            },
            headers={
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "X-Content-Type-Options": "nosniff"
            }
        )
    except Exception as e:
        logger.error(f"Error getting API docs: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/help")
async def get_api_help():
    """获取API列表（纯文本格式，只显示方法名和路径）"""
    try:
        # 按方法类型和路径排序
        sorted_apis = sorted(API_DOCS_LIST, key=lambda x: (x['method'], x['path']))

        # 生成纯文本API列表
        api_list = []
        for api in sorted_apis:
            # 格式：METHOD    PATH
            api_list.append(f"{api['method']:<10} {api['path']}")

        # 直接返回纯文本（每个API一行）
        text_content = "GMS Auto Test API List\n"
        text_content += "=" * 60 + "\n\n"
        text_content += f"Total: {len(api_list)} APIs\n"
        text_content += f"Updated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        text_content += "=" * 60 + "\n\n"
        text_content += "\n".join(api_list) + "\n"  # 确保最后也有换行

        # 添加使用示例
        text_content += "\n" + "=" * 60 + "\n"
        text_content += "Usage Examples:\n"
        text_content += '  curl -s "http://172.16.14.233:5001/api/devices?help=1"           \n'
        text_content += '  curl -s "http://172.16.14.233:5001/api/test/status?help=1"       \n'
        text_content += '  curl -sX POST "http://172.16.14.233:5001/api/test/start?help=1"  \n'

        return PlainTextResponse(
            content=text_content,
            headers={
                "Cache-Control": "public, max-age=300",
                "Content-Type": "text/plain; charset=utf-8"
            }
        )
    except Exception as e:
        logger.error(f"Error getting API help: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/help/{api_path:path}")
async def get_api_help_detail(api_path: str, help: Optional[str] = None):
    """获取单个API的详细帮助信息（通过GET请求）"""
    try:
        # 查找匹配的API
        api_doc = None
        for api in API_DOCS_LIST:
            # 移除开头的斜杠进行匹配
            if api['path'].lstrip('/') == api_path:
                api_doc = api
                break

        if not api_doc:
            raise HTTPException(status_code=404, detail=f"API not found: /{api_path}")

        # 生成帮助文本
        help_text = generate_per_api_help_text(api_doc['method'], api_doc['path'])

        if not help_text:
            raise HTTPException(status_code=404, detail=f"Help not available for: /{api_path}")

        return PlainTextResponse(
            content=help_text,
            headers={
                "Content-Type": "text/plain; charset=utf-8",
                "Cache-Control": "public, max-age=300"
            }
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting API help detail: {e}")
        raise HTTPException(status_code=500, detail=str(e))


def generate_per_api_help_text(method: str, path: str) -> Optional[str]:
    """为指定API生成详细帮助文本（优化格式）

    Args:
        method: HTTP方法 (GET/POST/DELETE等)
        path: API路径

    Returns:
        格式化的帮助文本，如果API不存在则返回None
    """

    def get_display_width(text):
        """计算字符串的显示宽度（中文算2个字符）"""
        width = 0
        for char in text:
            if ord(char) > 127:  # 非ASCII字符（中文等）
                width += 2
            else:
                width += 1
        return width

    def pad_string(text, target_width, align='left'):
        """填充字符串到目标显示宽度，考虑中文"""
        current_width = get_display_width(text)
        padding = target_width - current_width

        if align == 'center':
            left_pad = padding // 2
            right_pad = padding - left_pad
            return ' ' * left_pad + text + ' ' * right_pad
        elif align == 'right':
            return ' ' * padding + text
        else:  # left
            return text + ' ' * padding

    base_url = "http://172.16.14.233:5001"

    # 详细的API参数映射（与前端保持一致）
    API_DETAILS_MAP = {
        '/api/test/start': {
            'title': '启动测试',
            'description': '启动兼容性测试(CTS/VTS/GTS等)',
            'params': [
                {'name': 'devices', 'type': 'array', 'required': True, 'desc': '设备序列号数组'},
                {'name': 'test_type', 'type': 'string', 'required': True, 'desc': '测试类型: CTS|VTS|STS|GTS|CTS_VERIFIER'},
                {'name': 'test_module', 'type': 'string', 'required': True, 'desc': '测试模块名称'},
                {'name': 'test_case', 'type': 'string', 'required': False, 'desc': '具体测试用例(可选)'},
                {'name': 'retry_dir', 'type': 'string', 'required': False, 'desc': '重试目录(可选)'},
                {'name': 'test_suite', 'type': 'string', 'required': False, 'desc': '测试套件路径(可选)'}
            ],
            'response': '{"success": true, "message": "测试已启动"}',
            'usage': '⭐核心接口'
        },
        '/api/test/stop': {
            'title': '停止测试',
            'description': '停止当前正在运行的测试',
            'params': [],
            'response': '{"success": true, "message": "测试已停止"}',
            'usage': ''
        },
        '/api/devices': {
            'title': '获取设备列表',
            'description': '获取所有已连接的设备列表',
            'params': [],
            'response': '{"success": true, "devices": [...]}',
            'usage': ''
        },
        '/api/devices/lock': {
            'title': '锁定设备',
            'description': '锁定/解锁设备',
            'params': [
                {'name': 'device_id', 'type': 'string', 'required': True, 'desc': '设备序列号'},
                {'name': 'client_id', 'type': 'string', 'required': True, 'desc': '客户端ID'},
                {'name': 'username', 'type': 'string', 'required': True, 'desc': '用户名'}
            ],
            'response': '{"success": true, "message": "设备已锁定"}',
            'usage': ''
        },
        '/api/burn/firmware': {
            'title': '刷入固件',
            'description': '上传固件文件并刷入设备',
            'params': [
                {'name': 'firmware_file', 'type': 'file', 'required': True, 'desc': '固件文件（.img格式）'},
                {'name': 'devices', 'type': 'string', 'required': True, 'desc': '设备序列号（多个用逗号分隔）'},
                {'name': 'wipe_data', 'type': 'boolean', 'required': False, 'desc': '是否清除数据（默认true）'}
            ],
            'response': '{"success": true, "message": "固件刷入完成"}',
            'usage': ''
        },
        '/api/test/status': {
            'title': '获取测试状态',
            'description': '获取当前测试运行状态',
            'params': [],
            'response': '{"running": false, "devices": []}',
            'usage': ''
        },
        '/api/usbip/start': {
            'title': '启动 USB/IP 连接',
            'description': '通过 USB/IP 连接到远程设备',
            'params': [
                {'name': 'device_host', 'type': 'string', 'required': True, 'desc': 'Windows 主机地址 (user@ip)'},
                {'name': 'device_password', 'type': 'string', 'required': True, 'desc': 'SSH 密码'}
            ],
            'response': '{"success": true, "devices": [...]}',
            'usage': ''
        },
        '/api/desktop/vnc/status': {
            'title': '查询桌面VNC状态',
            'description': '查询Ubuntu桌面VNC服务状态（运行中/已停止）和远程访问地址',
            'params': [],
            'response': '{"success": true, "running": true, "url": "http://xxx:6080/vnc.html"}',
            'usage': '检查Ubuntu桌面VNC服务是否正在运行，获取远程访问URL'
        },
        '/api/desktop/vnc/start': {
            'title': '启动桌面VNC',
            'description': '启动Ubuntu桌面VNC服务，返回VNC访问URL用于远程桌面连接',
            'params': [
                {'name': 'host', 'type': 'string', 'required': False, 'desc': '桌面主机地址，格式：user@ip（可选，使用配置默认值）'},
                {'name': 'password', 'type': 'string', 'required': False, 'desc': 'SSH登录密码（可选）'},
                {'name': 'vnc_password', 'type': 'string', 'required': False, 'desc': 'VNC访问密码（可选）'}
            ],
            'response': '{"success": true, "url": "http://xxx:6080/vnc.html"}',
            'usage': '启动Ubuntu桌面的VNC服务，通过浏览器远程访问图形化桌面'
        },
        '/api/desktop/vnc/stop': {
            'title': '停止桌面VNC',
            'description': '停止Ubuntu桌面VNC服务，断开所有远程桌面连接',
            'params': [],
            'response': '{"success": true, "message": "桌面VNC已停止"}',
            'usage': '停止Ubuntu桌面VNC服务，释放系统资源'
        },
        '/api/desktop/validate': {
            'title': '验证桌面主机',
            'description': '验证Ubuntu主机SSH连接并检查VNC服务可用性（host格式：user@ip）',
            'params': [
                {'name': 'host', 'type': 'string', 'required': True, 'desc': '主机地址（格式：user@ip，如hcq@172.16.14.233）'},
                {'name': 'password', 'type': 'string', 'required': False, 'desc': 'SSH登录密码（可选）'}
            ],
            'response': '{"success": true, "message": "SSH连接成功，VNC服务可用"}',
            'usage': '连接Ubuntu桌面主机前验证SSH连接和VNC服务状态'
        }
    }

    # 查找API详情
    api_details = API_DETAILS_MAP.get(path)
    if not api_details:
        return None

    params = api_details.get('params', [])

    # 构建帮助文本（优化格式）
    help_text = ""

    # 固定的边框线（70个字符宽，包含左右边框）
    border_line = "╔════════════════════════════════════════════════════════════════════╗"
    mid_line = "╠════════════════════════════════════════════════════════════════════╣"
    bottom_line = "╚════════════════════════════════════════════════════════════════════╝"

    help_text += f"{border_line}\n"

    # 第一行：方法 + 路径
    method_part = f"  {method}  "
    # 目标：让字符串长度与边框线一致（70个字符）
    # 内容区：70 - 2(左右║) = 68个字符
    content_length = 68
    method_length = len(method_part)
    path_length = len(path)
    needed_padding = content_length - method_length - path_length
    path_part = path + ' ' * needed_padding

    help_text += f"║{method_part}{path_part}║\n"

    help_text += f"{mid_line}\n"

    # 第二行：emoji + 描述
    description = api_details['description']
    desc_prefix = "  📋 "
    prefix_length = len(desc_prefix)
    desc_length = len(description)

    # 对于包含中文的行，需要调整填充以确保视觉对齐
    # 计算中文字符数量
    chinese_chars = len([c for c in description + desc_prefix if ord(c) > 127])
    # 每个中文字符的显示宽度比字符长度多1，所以需要减少相应数量的空格
    # 但不能减少太多，否则字符串长度会不够
    # 这里我们减少一半的差值作为平衡
    visual_adjustment = chinese_chars // 2
    needed_padding = content_length - prefix_length - desc_length + visual_adjustment

    desc_part = description + ' ' * needed_padding

    help_text += f"║{desc_prefix}{desc_part}║\n"

    help_text += f"{bottom_line}\n\n"

    # 完整curl命令
    if method == 'GET':
        help_text += f'curl -s "{base_url}{path}"\n\n'
    elif method == 'POST':
        has_file = any(p.get('type') == 'file' for p in params)
        if has_file:
            # FormData格式
            curl_cmd = f'curl -sX POST "{base_url}{path}"'
            for p in params:
                if p.get('type') == 'file':
                    curl_cmd += f' \\\n  -F "{p["name"]}=@VALUE"'
                elif p.get('type') == 'boolean':
                    curl_cmd += f' \\\n  -F "{p["name"]}=true"'
                else:
                    curl_cmd += f' \\\n  -F "{p["name"]}=VALUE"'
            help_text += curl_cmd + "\n\n"
        else:
            # JSON格式
            curl_cmd = f'curl -sX POST "{base_url}{path}"'
            if params:
                curl_cmd += ' \\\n  -H "Content-Type: application/json" \\\n  -d \''
                body_lines = ['{']
                for i, p in enumerate(params):
                    comma = "," if i < len(params) - 1 else ""
                    value = '["Serial"]' if p.get('type') == 'array' else '"VALUE"'
                    body_lines.append(f'    "{p["name"]}": {value}{comma}')
                body_lines.append('  }')
                curl_cmd += '\n'.join(body_lines) + '\''
            help_text += curl_cmd + "\n\n"
    elif method == 'DELETE':
        help_text += f'curl -X DELETE "{base_url}{path}"\n\n'

    # 标题
    usage = api_details.get('usage', '')
    if usage:
        help_text += f"### {api_details['title']} {usage}\n\n"
    else:
        help_text += f"### {api_details['title']}\n\n"

    # HTTP信息
    help_text += f"{method} {path}\n"
    if method == 'POST':
        has_file = any(p.get('type') == 'file' for p in params)
        if not has_file:
            help_text += f"Content-Type: application/json\n"
    help_text += "\n"

    # 参数说明（表格格式）
    if params:
        help_text += "📋 API 参数对照表\n\n"

        # 计算列宽（使用显示宽度，但确保最小宽度）
        name_width = max(get_display_width('API 参数'), max((get_display_width(p['name']) for p in params), default=get_display_width('API 参数')))
        desc_width = max(get_display_width('说明'), max(((get_display_width(p['desc'].split('(')[0]) + 6) for p in params), default=get_display_width('说明')))

        # 表格字符定义
        border_char = '─'
        corner_tl = '┌'
        corner_tr = '┐'
        corner_bl = '└'
        corner_br = '┘'
        tee_top = '┬'
        tee_bottom = '┴'
        tee_cross = '┼'  # 用于行分隔线的十字连接符
        bar = '│'

        # 列宽定义（固定）
        col1_width = name_width + 2      # API 参数列（含左右空格）
        col2_width = 6                    # 类型列（固定 6 字符，确保对齐）
        col3_width = desc_width + 10      # 说明列（含标记）
        col4_width = 14                   # 默认值列（固定 14 字符）

        # 构建表格行（使用显示宽度计算表头）
        top_border     = f"{corner_tl}{border_char * col1_width}{tee_top}{border_char * col2_width}{tee_top}{border_char * col3_width}{tee_top}{border_char * col4_width}{corner_tr}\n"
        header_row     = f"{bar}{pad_string('API 参数', col1_width, 'center')}{bar}{pad_string('类型', col2_width, 'center')}{bar}{pad_string('说明', col3_width, 'center')}{bar}{pad_string('默认值', col4_width, 'center')}{bar}\n"
        header_border  = f"{bar}{border_char * col1_width}{tee_top}{border_char * col2_width}{tee_top}{border_char * col3_width}{tee_top}{border_char * col4_width}{bar}\n"

        # 创建一个函数来生成正确长度的分隔线
        def create_separator():
            # 生成一个示例数据行来获取实际长度
            sample_row = f"{bar}{pad_string('sample', col1_width, 'center')}{bar}{pad_string('str', col2_width, 'center')}{bar}{pad_string('sample text', col3_width, 'left')}{bar}{pad_string('', col4_width, 'center')}{bar}"
            # 获取每一节的实际长度
            sections = []
            current_section = ""
            in_section = False
            for char in sample_row:
                if char == bar:
                    if in_section:
                        sections.append(current_section)
                        current_section = ""
                    in_section = True
                elif in_section:
                    current_section += char
            if current_section:
                sections.append(current_section)

            # 使用实际的字符串长度来构建分隔线
            if len(sections) >= 4:
                return f"{bar}{border_char * len(sections[0])}{tee_cross}{border_char * len(sections[1])}{tee_cross}{border_char * len(sections[2])}{tee_cross}{border_char * len(sections[3])}{bar}\n"
            else:
                # 备用方案
                return f"{bar}{border_char * col1_width}{tee_cross}{border_char * col2_width}{tee_cross}{border_char * col3_width}{tee_cross}{border_char * col4_width}{bar}\n"

        row_separator  = create_separator()
        bottom_border  = f"{corner_bl}{border_char * col1_width}{tee_bottom}{border_char * col2_width}{tee_bottom}{border_char * col3_width}{tee_bottom}{border_char * col4_width}{corner_br}\n"

        # 添加表头部分
        help_text += f"  {top_border}"
        help_text += f"  {header_row}"
        help_text += f"  {header_border}"

        # 参数行
        for i, param in enumerate(params):
            name = param['name']
            ptype = param.get('type', 'string')
            # 统一类型缩写，确保对齐
            type_map = {
                'array': 'arr',
                'string': 'str',
                'number': 'num',
                'integer': 'int',
                'boolean': 'bool',
                'object': 'obj'
            }
            ptype = type_map.get(ptype.lower(), ptype[:3])
            desc = param['desc'].split('(')[0].strip()  # 去掉 (可选) 等后缀
            default_val = param.get('default', '')
            required = param.get('required', False)

            # 在说明中添加必需/可选标记
            if required:
                desc_with_mark = f"{desc} ⭐"
            else:
                desc_with_mark = f"{desc} (可选)"

            # 使用新的填充函数格式化每个单元格
            name_formatted = pad_string(name, col1_width, 'center')
            ptype_formatted = pad_string(ptype, col2_width, 'center')
            desc_formatted = pad_string(desc_with_mark, col3_width, 'left')
            default_formatted = pad_string(default_val, col4_width, 'center')

            row = f"{bar}{name_formatted}{bar}{ptype_formatted}{bar}{desc_formatted}{bar}{default_formatted}{bar}\n"
            help_text += f"  {row}"

            # 在每一行后面添加分隔线（除了最后一行）
            if i < len(params) - 1:
                help_text += f"  {row_separator}"

        # 表尾
        help_text += f"  {bottom_border}"
        help_text += "\n"

    # 响应示例
    help_text += "📤 响应示例:\n"
    response_str = api_details.get('response', '{"success": true}')
    try:
        response_obj = json.loads(response_str)
        help_text += json.dumps(response_obj, ensure_ascii=False, indent=2)
    except:
        help_text += response_str

    # 添加结尾换行符（两个换行，视觉上更明显）
    help_text += "\n\n"

    return help_text


def generate_curl_example(api):
    """生成API的curl示例命令"""
    method = api['method']
    path = api['path']
    params = api.get('params', [])
    base_url = "http://172.16.14.233:5001"

    if method == 'GET':
        if params:
            # 有参数的GET请求
            param = params[0]
            return f'curl -s "{base_url}{path}?{param["name"]}=VALUE"'
        else:
            return f'curl -s "{base_url}{path}"'

    elif method == 'POST':
        if params:
            # 检查是否有file类型参数
            has_file = any(p.get('type') == 'file' for p in params)
            if has_file:
                # FormData格式
                file_params = [p for p in params if p.get('type') == 'file']
                other_params = [p for p in params if p.get('type') != 'file']

                parts = []
                for p in file_params:
                    parts.append(f'-F "{p["name"]}=@VALUE"')
                for p in other_params[:2]:  # 最多显示2个参数
                    parts.append(f'-F "{p["name"]}=VALUE"')

                cmd = f'curl -sX POST "{base_url}{path}"'
                if parts:
                    cmd += ' \\\n  ' + ' \\\n  '.join(parts)
                return cmd
            else:
                # JSON格式
                json_body = "{"
                for i, p in enumerate(params[:2]):  # 最多显示2个参数
                    comma = "," if i < min(len(params), 2) - 1 else ""
                    json_body += f'\\n    "{p["name"]}": "VALUE"{comma}'
                json_body += "\\n  }"

                return f'curl -sX POST "{base_url}{path}" \\\n  -H "Content-Type: application/json" \\\n  -d \'{json_body}\''
        else:
            return f'curl -sX POST "{base_url}{path}"'

    elif method == 'DELETE':
        if params:
            param = params[0]
            return f'curl -X DELETE "{base_url}{path}" \\\n  -G \\\n  -d "{param["name"]}=VALUE"'
        else:
            return f'curl -X DELETE "{base_url}{path}"'

    else:
        return f'curl -X {method} "{base_url}{path}"'


def generate_api_example(api):
    """生成API使用示例"""
    method = api['method']
    path = api['path']
    params = api.get('params', [])

    base_url = "http://172.16.14.233:5001"

    if method == 'GET':
        if params:
            # 有参数的GET请求
            param_str = "&".join([f"{p['name']}=VALUE" for p in params[:2]])
            return f'curl -s "{base_url}{path}?{param_str}"'
        else:
            return f'curl -s "{base_url}{path}"'

    elif method == 'POST':
        if params:
            # 检查是否有file类型参数
            has_file = any(p.get('type') == 'file' for p in params)
            if has_file:
                # FormData格式
                param_str = " \\\n  ".join([
                    f'-F "{p["name"]}=@{p.get("desc", "path/to/file")}"' if p.get('type') == 'file' else f'-F "{p["name"]}=VALUE"'
                    for p in params[:3]
                ])
                return f'curl -sX POST "{base_url}{path}" \\\n  {param_str}'
            else:
                # JSON格式
                body = "{"
                for i, p in enumerate(params[:3]):
                    comma = "," if i < len(params) - 1 else ""
                    body += f'\n    "{p["name"]}": "VALUE"{comma}'
                body += "\n  }"
                return f'curl -sX POST "{base_url}{path}" \\\n  -H "Content-Type: application/json" \\\n  -d \'{body}\''
        else:
            return f'curl -sX POST "{base_url}{path}"'

    elif method == 'DELETE':
        if params:
            param = params[0]['name']
            return f'curl -X DELETE "{base_url}{path}" \\\n  -G \\\n  -d "{param}=VALUE"'
        else:
            return f'curl -X DELETE "{base_url}{path}"'

    else:
        return f'curl -X {method} "{base_url}{path}"'

# ==================== Claude报告分析API ====================

@app.get("/api/reports/claude-analyze/{report_timestamp}")
async def claude_analyze_report(
    report_timestamp: str,
    use_claude_api: bool = Query(False, description="是否使用Claude API进行深度分析"),
    claude_api_key: Optional[str] = Query(None, description="Claude API密钥（如果使用Claude API）")
):
    """
    使用Claude分析测试报告

    Args:
        report_timestamp: 报告时间戳
        use_claude_api: 是否使用Claude API（需要API密钥）
        claude_api_key: Claude API密钥

    Returns:
        {
            "success": true,
            "basic_analysis": {...},  # 基础分析（总是返回）
            "claude_analysis": {...}  # Claude深度分析（如果use_claude_api=true）
        }
    """
    try:
        # 从数据库获取报告信息
        report = test_report_db.get_report_by_timestamp(report_timestamp)

        if not report:
            return JSONResponse(
                content={'success': False, 'error': '报告不存在'},
                status_code=404
            )

        # 获取路径信息
        result_dir = report.get('result_dir')
        log_file = report.get('log_file')

        if not result_dir or not os.path.exists(result_dir):
            return JSONResponse(
                content={'success': False, 'error': '报告目录不存在'},
                status_code=404
            )

        # 创建Claude分析器
        analyzer = ClaudeReportAnalyzer()

        # 1. 基础分析（解析日志摘要）
        basic_analysis = analyzer.analyze_report_file(result_dir, log_file)

        if not basic_analysis.get('success'):
            return JSONResponse(
                content={'success': False, 'error': '基础分析失败'},
                status_code=500
            )

        result = {
            'success': True,
            'basic_analysis': basic_analysis
        }

        # 2. Claude API深度分析（可选）
        if use_claude_api and claude_api_key:
            logger.info(f"[Claude] 使用Claude API深度分析报告: {report_timestamp}")
            claude_analysis = analyzer.analyze_with_claude_api(basic_analysis, claude_api_key)
            result['claude_analysis'] = claude_analysis

            if claude_analysis.get('success'):
                logger.info("[Claude] Claude API分析成功")
            else:
                logger.warning(f"[Claude] Claude API分析失败: {claude_analysis.get('error')}")

        return JSONResponse(content=result)

    except Exception as e:
        logger.error(f"Claude分析失败: {e}", exc_info=True)
        return JSONResponse(
            content={'success': False, 'error': str(e)},
            status_code=500
        )


@app.post("/api/reports/claude-analyze-upload")
async def claude_analyze_uploaded_report(
    file: UploadFile = File(..., description="测试日志文件或XML报告"),
    use_claude_api: bool = Form(False, description="是否使用Claude API"),
    claude_api_key: Optional[str] = Form(None, description="Claude API密钥")
):
    """
    分析上传的测试报告文件

    Args:
        file: 上传的文件（支持.log、.xml、.zip、.tar.gz）
        use_claude_api: 是否使用Claude API
        claude_api_key: Claude API密钥

    Returns:
        分析结果
    """
    import tempfile
    import shutil

    try:
        # 保存上传的文件到临时目录
        temp_dir = tempfile.mkdtemp(prefix='claude_analyze_')
        file_path = os.path.join(temp_dir, file.filename)

        with open(file_path, 'wb') as f:
            content = await file.read()
            f.write(content)

        # 创建分析器
        analyzer = ClaudeReportAnalyzer()

        # 基础分析
        basic_analysis = analyzer.analyze_report_file(temp_dir, file_path)

        result = {
            'success': True,
            'basic_analysis': basic_analysis
        }

        # Claude API分析
        if use_claude_api and claude_api_key:
            claude_analysis = analyzer.analyze_with_claude_api(basic_analysis, claude_api_key)
            result['claude_analysis'] = claude_analysis

        # 清理临时文件
        shutil.rmtree(temp_dir, ignore_errors=True)

        return JSONResponse(content=result)

    except Exception as e:
        logger.error(f"分析上传文件失败: {e}", exc_info=True)
        return JSONResponse(
            content={'success': False, 'error': str(e)},
            status_code=500
        )


# ==================== 主程序 ====================

if __name__ == "__main__":
    logger.info("Starting GMS Auto Test FastAPI Server on port 5001...")
    logger.info("=" * 60)
    logger.info("  GMS Auto Test - FastAPI Server (Port 5001)")
    logger.info("  Framework: FastAPI (Pure)")
    logger.info("  Version: 4.0.0")
    logger.info("  Complete Migration from Flask")
    logger.info("=" * 60)
    logger.info("")

    # 运行FastAPI应用
    uvicorn.run(
        app,
        host='0.0.0.0',
        port=5001,
        log_level='info'
    )
